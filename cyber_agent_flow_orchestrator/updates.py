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


def package(source, ref, base, destination, *, trace=None):
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

    def close(self):
        with self.lock:
            self.closed = True
        # Allow a started activation to finish and record its outcome.
        self.pool.shutdown(wait=False)

    def view(self, access, workspace, roles):
        if self.config is None:
            return None
        try:
            require_maintenance(access, self.config['group'])
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
        return dict(can_update=permitted, group=self.config['group'], jobs=rows,
                    applications=[dict(role=role, vmid=roles.get(role), **self.config[role]) for role in ('participant', 'scenarioforge')])

    def submit(self, access, role, action, ref, request_id):
        if self.config is None or role not in ('participant', 'scenarioforge') or action not in ('inspect', 'update', 'rollback'):
            raise UpdateError('This application maintenance action is not enabled')
        ref_name(ref)
        if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id):
            raise UpdateError('Invalid maintenance request ID')
        if action != 'inspect':
            require_maintenance(access, self.config['group'])
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()[role]
        if vmid is None:
            raise UpdateError('Save a VM selection for this application first')
        access.require_vm(vmid)
        definition = next(item for item in definitions(self.cfg, self.runtime) if item['role'] == role)
        root = definition['root']
        python = self.runtime['engine']['python'] if role == 'participant' else self.cfg['scenarioforge'].get('python', root + '/.venv/bin/python')
        job = dict(id=request_id, role=role, action=action, ref=ref, vmid=vmid, root=root,
                   source=self.config[role]['url'],
                   status='queued', created_at=now(), message='Waiting for maintenance worker')
        directory = workspace.path / 'updates' / request_id
        with self.lock:
            if directory.exists():
                old = ev.read_json(directory / 'job.json')
                if any(old[k] != job[k] for k in ('role', 'action', 'ref', 'vmid')):
                    raise UpdateError('Request ID already belongs to another operation')
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
            trace.emit('authorization', 'Checking current PVE group and VM permissions', force=True)
            try:
                if job['action'] != 'inspect':
                    require_maintenance(access, self.config['group'])
                access.qm(args)
            except Exception:
                trace.emit('error', f'PVE authorization failed after {time.monotonic()-start:.2f}s', force=True)
                raise
            trace.emit('authorization', f'PVE authorization passed in {time.monotonic()-start:.2f}s')
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
                with ev.TargetReservation(self.runtime['execution']['target_lock']), ev.lease(f'/var/lock/cyber-agent-flow-eval-vm-{vmid}.lock'):
                    if self.closed:
                        raise UpdateError('Server stopped before maintenance began')
                    require_maintenance(access, self.config['group'])
                    update = dict(args, token=job['id'], expected_revision=installed['revision'], python=python, service=service)
                    if job['action'] == 'update':
                        save('Downloading the selected source revision on the host')
                        revision, bundle = package(self.config[job['role']], job['ref'], installed['revision'], directory, trace=trace)
                        if bundle.stat().st_size > self.runtime['backend']['max_transfer_bytes']:
                            raise UpdateError('Source bundle exceeds the configured transfer limit')
                        job['revision'] = revision
                        save('Transferring source through the guest agent')
                        upload = agent.call(vmid, 'app_stage', **args, token=job['id'])
                        # Standard transfer RPC uses its own helper, with the same authorization callback.
                        transfer = trace.instrument(ev.GuestAgent(self.runtime['backend'], authorize=authorize))
                        content = bundle.read_bytes()
                        job['bundle_sha256'] = hashlib.sha256(content).hexdigest()
                        save('Transferring verified source bundle through the guest agent')
                        transfer.put(vmid, upload['path'], content)
                        update.update(revision=revision, sha256=job['bundle_sha256'])
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
