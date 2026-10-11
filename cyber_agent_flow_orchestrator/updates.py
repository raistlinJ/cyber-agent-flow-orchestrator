"""Host source packaging and PVE-scoped, offline application maintenance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess
import threading
import os
import time
from urllib.parse import urlsplit

from cyber_agent_flow_eval import integration as ev, reporting
from .access import AccessDenied
from .monitor import definitions, now
from .workspaces import Workspace, private_directory
from .diagnostics import Trace

DEFAULTS = {
    'group': 'caf-maintainers',
    'participant': {'url': 'https://github.com/raistlinJ/cyber-agent-flow.git', 'ref': 'main'},
    'scenarioforge': {'url': 'https://github.com/raistlinJ/scenarioforge.git', 'ref': 'main'},
}


class UpdateError(ValueError):
    pass


def ref_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,199}', value)
            or '..' in value or value.endswith('/') or '//' in value):
        raise UpdateError('Use a branch, tag or commit from the configured repository')
    return value


def settings(value):
    if value is False:
        return None
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('updates accepts group, participant and scenarioforge, or false')
    result = deepcopy(DEFAULTS)
    result.update(value)
    if not isinstance(result['group'], str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_.-]{0,63}', result['group']):
        raise ValueError('updates.group must be a PVE maintenance group')
    for role in ('participant', 'scenarioforge'):
        source = result[role]
        if not isinstance(source, dict) or set(source) - {'url', 'ref'}:
            raise ValueError('Update sources accept url and ref')
        source = result[role] = dict(DEFAULTS[role], **source)
        parsed = urlsplit(source['url'])
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or re.search(r'[\s\x00-\x1f]', source['url'])):
            raise ValueError('Update repository must be an HTTPS URL without credentials')
        ref_name(source['ref'])
    return result


def require_maintenance(access, group):
    record = access.current()
    users = access.provider.request('GET', '/access/users?full=1', ticket=record['credential'])
    if not isinstance(users, list) or not any(
            isinstance(user, dict) and user.get('userid') == access.username
            and isinstance(user.get('groups'), str) and group in user['groups'].split(',') for user in users):
        raise AccessDenied('Application updates require the ' + group + ' PVE group')
    access.current()


def package(source, ref, base, destination, *, trace=None, allow_same=False):
    """Download on the host; send only missing Git objects when base is an ancestor."""
    repository = destination / 'source.git'
    def git(*args, timeout=300, check=True):
        argv = ['git', '-c', 'core.hooksPath=/dev/null', *args]
        if trace:
            from .guest_monitor import redact
            trace.emit('command', redact(argv), force=True)
        start = time.monotonic()
        try:
            process = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                     env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        except Exception as exc:
            if trace:
                trace.emit('error', f'Host Git command failed after {time.monotonic()-start:.2f}s ({type(exc).__name__})', force=True)
            raise
        if trace:
            trace.emit('response', f'Host Git exit {process.returncode} after {time.monotonic()-start:.2f}s\n'
                       + process.stdout.decode(errors='replace') + process.stderr.decode(errors='replace'), force=True)
        if check and process.returncode:
            raise UpdateError('Host Git download/package failed: ' + process.stderr.decode(errors='replace')[-1200:])
        return process
    git('init', '--bare', str(repository))
    git('-C', str(repository), 'fetch', '--no-tags', source['url'], ref_name(ref))
    revision = git('-C', str(repository), 'rev-parse', 'FETCH_HEAD^{commit}').stdout.decode().strip()
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise UpdateError('Unsupported source revision')
    git('-C', str(repository), 'update-ref', 'refs/heads/caf-release', revision)
    excludes = []
    if re.fullmatch('[0-9a-f]{40}', base) and git('-C', str(repository), 'merge-base', '--is-ancestor', base, revision, check=False).returncode == 0:
        excludes = ['^' + base]
    if revision == base and allow_same:
        return revision, None
    if revision == base:
        raise UpdateError('The selected revision is already installed')
    bundle = destination / 'source.bundle'
    git('-C', str(repository), 'bundle', 'create', str(bundle), 'refs/heads/caf-release', *excludes)
    return revision, bundle


class UpdateManager:
    def __init__(self, cfg, runtime, root, config=None):
        self.cfg, self.runtime, self.root = cfg, runtime, Path(root)
        self.config = settings(config)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='application-update')
        self.lock, self.jobs, self.closed = threading.Lock(), {}, False

    @property
    def desktop(self):
        return self.runtime['backend']['type'] == 'fusion'

    def require_update_access(self, access, vmid=None):
        """Authorize maintenance for PVE groups or the owned desktop inventory."""
        if self.desktop:
            access.current()
            if vmid is not None:
                access.require_vm(vmid)
            return
        require_maintenance(access, self.config['group'])

    def close(self):
        with self.lock:
            self.closed = True
        # Allow a started activation to finish and record its outcome.
        self.pool.shutdown(wait=False)

    def view(self, access, workspace, roles):
        if self.config is None:
            return None
        try:
            self.require_update_access(access)
            permitted = True
        except AccessDenied:
            permitted = False
        rows = []
        folder = workspace.path / 'updates'
        if folder.is_dir():
            for item in sorted(folder.glob('*/job.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                row = ev.read_json(item)
                if row['status'] == 'running' and not reporting.active(item.parent / '.lock'):
                    row['status'] = 'interrupted'
                if row['status'] == 'queued' and (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds() > 15:
                    row['status'] = 'interrupted'
                console = item.parent / 'console.json'
                if len(rows) < 3 and console.is_file() and not console.is_symlink() and console.stat().st_size <= 1024 * 1024:
                    try:
                        row['console'] = ev.read_json(console)
                    except (OSError, ValueError):
                        pass  # A damaged optional trace must not hide the job result.
                rows.append(row)
        return dict(can_update=permitted, group=self.config['group'], desktop=self.desktop, jobs=rows,
                    applications=[dict(role=role, vmid=roles.get(role), **self.config[role]) for role in ('participant', 'scenarioforge')])

    def submit(self, access, role, action, ref, request_id, process_confirmation=None, force_confirmation=None):
        if self.config is None or role not in ('participant', 'scenarioforge') or action not in ('inspect', 'update', 'rollback'):
            raise UpdateError('This application maintenance action is not enabled')
        ref_name(ref)
        if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id):
            raise UpdateError('Invalid maintenance request ID')
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()[role]
        if vmid is None:
            raise UpdateError('Save a VM selection for this application first')
        access.require_vm(vmid)
        if action != 'inspect':
            self.require_update_access(access, vmid)
        definition = next(item for item in definitions(self.cfg, self.runtime) if item['role'] == role)
        root = definition['root']
        approved = None
        if process_confirmation is not None:
            if action != 'update' or not isinstance(process_confirmation, str) or not re.fullmatch('[0-9a-f]{32}', process_confirmation):
                raise UpdateError('Process confirmation requires an update and a valid inspection ID')
            prior_path = workspace.path / 'updates' / process_confirmation / 'job.json'
            if not prior_path.is_file():
                raise UpdateError('Check version and confirm its process list first')
            prior = ev.read_json(prior_path)
            approved = prior.get('installed', {}).get('processes')
            if (prior.get('status') not in ('completed', 'failed') or
                    any(prior.get(k) != v for k, v in dict(role=role, vmid=vmid, root=root).items()) or
                    not isinstance(approved, list) or not 1 <= len(approved) <= 12 or
                    any(type(p.get('pid')) is not int or p['pid'] <= 1 or
                        not re.fullmatch('[0-9a-f]{64}', p.get('identity', '')) for p in approved)):
                raise UpdateError('Check version and confirm a fresh process list for this application')
        force_expected = None
        if force_confirmation is not None:
            if action != 'update' or not isinstance(force_confirmation, str) or not re.fullmatch('[0-9a-f]{32}', force_confirmation):
                raise UpdateError('Force confirmation requires an update and a valid inspection ID')
            prior_path = workspace.path / 'updates' / force_confirmation / 'job.json'
            if not prior_path.is_file():
                raise UpdateError('Check version before forcing an update')
            prior = ev.read_json(prior_path)
            info = prior.get('installed', {})
            if (prior.get('action') != 'inspect' or prior.get('status') != 'completed'
                    or any(prior.get(k) != v for k, v in dict(role=role, vmid=vmid, root=root).items())
                    or not info.get('modified') or not re.fullmatch('[0-9a-f]{40}', info.get('revision', ''))
                    or type(info.get('modified_file_count')) is not int
                    or not isinstance(info.get('modified_files'), list)):
                raise UpdateError('Check version again before forcing an update')
            force_expected = {key: info[key] for key in ('revision', 'modified_file_count', 'modified_files')}
        python = self.runtime['engine']['python'] if role == 'participant' else self.cfg['scenarioforge'].get('python', root + '/.venv/bin/python')
        job = dict(id=request_id, role=role, action=action, ref=ref, vmid=vmid, root=root,
                   source=self.config[role]['url'],
                   status='queued', created_at=now(), message='Waiting for maintenance worker')
        if approved:
            job.update(process_confirmation=process_confirmation, stop_processes=approved)
        if force_expected:
            job.update(force_confirmation=force_confirmation, force_expected=force_expected)
        directory = workspace.path / 'updates' / request_id
        with self.lock:
            if directory.exists():
                old = ev.read_json(directory / 'job.json')
                if any(old[k] != job[k] for k in ('role', 'action', 'ref', 'vmid')):
                    raise UpdateError('Request ID already belongs to another operation')
                if old.get('process_confirmation') != process_confirmation:
                    raise UpdateError('Request ID already belongs to another process confirmation')
                if old.get('force_confirmation') != force_confirmation:
                    raise UpdateError('Request ID already belongs to another force confirmation')
                return {'id': request_id}
            self.jobs = {key: future for key, future in self.jobs.items() if not future.done()}
            if self.closed or access.username in self.jobs or len(self.jobs) >= 2:
                raise UpdateError('An application maintenance worker is already busy; try again later')
            private_directory(directory)
            ev.write_json(directory / 'job.json', job)
            self.jobs[access.username] = self.pool.submit(self._run, access, directory, job, python, definition.get('unit'))
        return {'id': request_id}

    def _run(self, access, directory, job, python, service):
        trace = Trace(directory)
        def save(message):
            job['message'] = message
            ev.write_json(directory / 'job.json', job)
            trace.emit('status', message, force=True)
        def authorize(args):
            start = time.monotonic()
            label = 'Desktop inventory authorization' if self.desktop else 'PVE authorization'
            trace.emit('authorization', f'Checking current {label}', force=True)
            try:
                access.qm(args, required_group=(self.config['group'] if job['action'] != 'inspect' and not self.desktop else None))
            except Exception:
                trace.emit('error', f'{label} failed after {time.monotonic()-start:.2f}s', force=True)
                raise
            trace.emit('authorization', f'{label} passed in {time.monotonic()-start:.2f}s')
        agent = trace.instrument(ev.GuestAgent(self.runtime['backend'], authorize=authorize))
        agent.script = Path(__file__).with_name('update_guest.py').read_text()
        vmid = job['vmid']
        args = dict(role=job['role'], root=job['root'])
        with ev.lease(directory / '.lock'):
            try:
                job['status'] = 'running'
                save('Reading the installed application revision')
                installed = agent.call(vmid, 'app_inspect', **args)
                job['installed'] = installed
                if job['action'] == 'inspect':
                    job['status'] = 'completed'
                    save('Installed revision and compatibility checked')
                    return
                replace_tools = job['action'] == 'update' and installed.get('tools_config_replaceable')
                force_expected = job.get('force_expected')
                if force_expected and any(installed.get(key) != value for key, value in force_expected.items()):
                    raise UpdateError('Tracked local edits changed after confirmation; check version again before forcing the update.')
                if installed.get('modified') and not replace_tools and not force_expected:
                    raise UpdateError('Tracked local edits exist; review the changed files under Check version and preserve/commit them before retrying. No source bundle downloaded or transferred; application files unchanged.')
                approved = job.get('stop_processes')
                if approved:
                    expected = {p['pid']: p['identity'] for p in approved}
                    if any(expected.get(p['pid']) != p.get('identity') for p in installed.get('processes', [])):
                        raise UpdateError('Application processes changed; check version and confirm the new process list. No source bundle downloaded or transferred.')
                if installed.get('processes') and not service and not approved:
                    processes = '; '.join(f"PID {p['pid']} ({p['name']}: {p['reason']})" for p in installed['processes'])
                    raise UpdateError('Processes reference the application checkout: ' + processes +
                                      '. No managed service is configured. Stop the application/workers or move idle shells out of the checkout, then retry. No source bundle downloaded or transferred.')
                with ev.TargetReservation(self.runtime['execution']['target_lock']), ev.lease(f'/var/lock/cyber-agent-flow-eval-vm-{vmid}.lock'):
                    if self.closed:
                        raise UpdateError('Server stopped before maintenance began')
                    self.require_update_access(access, vmid)
                    update = dict(args, token=job['id'], expected_revision=installed['revision'], python=python, service=service)
                    if force_expected:
                        update['force'] = True
                    if approved:
                        update['stop_processes'] = approved
                    if job['action'] == 'update':
                        save('Downloading the selected source revision on the host')
                        revision, bundle = package(self.config[job['role']], job['ref'], installed['revision'], directory,
                                                   trace=trace, allow_same=bool(force_expected))
                        job['revision'] = revision
                        update['revision'] = revision
                        if bundle is not None:
                            if bundle.stat().st_size > self.runtime['backend']['max_transfer_bytes']:
                                raise UpdateError('Source bundle exceeds the configured transfer limit')
                            save('Transferring source through the guest agent')
                            upload = agent.call(vmid, 'app_stage', **args, token=job['id'])
                            # Standard transfer RPC uses its own helper, with the same authorization callback.
                            transfer = trace.instrument(ev.GuestAgent(self.runtime['backend'], authorize=authorize))
                            content = bundle.read_bytes()
                            job['bundle_sha256'] = hashlib.sha256(content).hexdigest()
                            save('Transferring verified source bundle through the guest agent')
                            transfer.put(vmid, upload['path'], content)
                            update['sha256'] = job['bundle_sha256']
                    save('Validating and activating application source; preserving configuration and data')
                    if self.closed:
                        raise UpdateError('Server stopped before activation; application source was not changed')
                    result = agent.call(vmid, 'app_' + job['action'], timeout=360, **update)
                    job.update(status='completed', installed=result)
                    save('Application revision activated' if job['action'] == 'update' else 'Previous revision restored')
            except Exception as exc:
                job.update(status='failed', error=str(exc))
                save('Maintenance stopped; inspect details before retrying')
            finally:
                job['ended_at'] = now()
                ev.write_json(directory / 'job.json', job)
                trace.flush(force=True)
