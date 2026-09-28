"""Fixed offline Git update operations, sent through QGA; no shell commands."""
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

STATE_ROOT = Path('/var/lib/caf-application-updates')
LOCK_PATH = Path('/run/caf-application-maintenance.lock')
PENDING = Path('/var/lib/caf-application-maintenance.pending')
DEPENDENCIES = ('pyproject.toml', 'uv.lock', 'requirements.txt', 'requirements-core.txt', 'requirements-llm.txt', 'setup.py', 'setup.cfg')


def run(argv, *, cwd=None, user=None, check=True, timeout=60, input=None):
    if user and user != 'root':
        argv = ['runuser', '-u', user, '--', *argv]
    result = subprocess.run(argv, cwd=cwd, input=input, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[-1500:] or 'Guest command failed')
    return result


def git(root, *args, **kwargs):
    user = pwd.getpwuid(root.stat().st_uid).pw_name
    return run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-C', str(root), *args], user=user, **kwargs)


def value(root, *args):
    return git(root, *args).stdout.decode().strip()


def state_dir(root):
    STATE_ROOT.mkdir(mode=0o711, parents=True, exist_ok=True)
    directory = STATE_ROOT / hashlib.sha256(str(root).encode()).hexdigest()
    directory.mkdir(mode=0o711, exist_ok=True)
    if STATE_ROOT.is_symlink() or directory.is_symlink():
        raise ValueError('Update state directory cannot be a symlink')
    return directory


def save(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read(path):
    return json.loads(path.read_text()) if path.is_file() else None


@contextmanager
def maintenance():
    with LOCK_PATH.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A guest update or experiment launch is active') from None
        yield


def tracked_changes(root):
    # NUL framing preserves spaces/newlines and the second path of a rename.
    records = iter(git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=no').stdout.split(b'\0'))
    files, count, output_bytes = [], 0, 0
    for record in records:
        if not record:
            continue
        status = record[:2].decode('ascii', errors='replace')
        path = record[3:].decode(errors='replace')
        original = next(records, b'').decode(errors='replace') if 'R' in status or 'C' in status else None
        count += 1
        if len(files) < 50:
            label = status + ' ' + json.dumps(path[:300])
            if original is not None:
                label += ' <- ' + json.dumps(original[:300])
            size = len(json.dumps(label).encode()) + 2
            if output_bytes + size <= 8000:
                files.append(label)
                output_bytes += size
    return files, count


def identity(root, role):
    head = value(root, 'rev-parse', 'HEAD')
    controls = None
    if role == 'participant':
        try:
            tree = ast.parse((root / 'mcp_client.py').read_text())
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MCPSession')
            init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
            controls = sorted({'allowed_tools', 'guidance_text', 'reveal_network_policy'} -
                              {a.arg for a in [*init.args.args, *init.args.kwonlyargs]})
        except (OSError, SyntaxError, StopIteration):
            controls = ['compatible MCPSession']
    files, count = tracked_changes(root)
    return dict(revision=head, branch=value(root, 'branch', '--show-current'),
                modified=bool(count), modified_files=files, modified_file_count=count,
                missing_controls=controls)


def active_jobs():
    result = run(['systemctl', 'list-units', '--all', '--state=active,activating', '--no-legend', '--plain',
                  'caf-eval-*', 'caf-orchestrator-*', 'caf-sample-*'])
    if result.stdout.strip():
        raise ValueError('An experiment or workflow is still running inside this VM')


def active_processes(root):
    # Fail closed on unreadable processes. Ignore only this helper and its ancestors.
    ignored, pid = {os.getpid()}, os.getppid()
    while pid > 1 and pid not in ignored:
        ignored.add(pid)
        try:
            pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
        except FileNotFoundError:
            break
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal() or int(proc.name) in ignored:
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            command = b' '.join(argv).decode(errors='replace')
            cwd = os.readlink(proc / 'cwd')
        except FileNotFoundError:
            continue
        if len(argv) > 2 and argv[1] == b'-c' and argv[2].startswith(b'"""Read-only, stdlib-only Linux guest probe, sent through QEMU Guest Agent.'):
            continue
        if (str(root) in command or cwd == str(root) or cwd.startswith(str(root) + '/')):
            raise ValueError('Application processes are still running; stop them before maintenance')


def validate_checkout(stage, original, python, role, *, rollback=False):
    # Dependency installation is deliberately separate: offline updates must not
    # mutate a shared environment or download packages behind the user's back.
    manifests = set(DEPENDENCIES) | {p.name for base in (original, stage) for p in base.glob('requirements*.txt')}
    for name in sorted(manifests):
        old, new = original / name, stage / name
        if (old.read_bytes() if old.is_file() else None) != (new.read_bytes() if new.is_file() else None):
            raise ValueError(f'Dependency manifest changed: {name}. Prepare dependencies through provisioning before this source update.')
    if role == 'participant':
        missing = identity(stage, role)['missing_controls']
        if missing and not rollback:
            raise ValueError('Target CAF lacks evaluation controls: ' + ', '.join(missing))
        script = 'import mcp_client, mcp_kali, session_logger'
    else:
        if not (stage / 'scenarioforge/cli.py').is_file() or not (stage / 'webapp/app_backend.py').is_file():
            raise ValueError('Target is not a ScenarioForge application checkout')
        script = 'import scenarioforge.cli; import flask, lxml, yaml, psutil'
    user = pwd.getpwuid(original.stat().st_uid).pw_name
    run([str(python), '-c', script], cwd=stage, user=user, timeout=45)
    # uv-created environments may not include pip; imports above are always checked.
    check = run([str(python), '-c', 'import importlib.util; print(bool(importlib.util.find_spec("pip")))'], user=user)
    if check.stdout.strip() == b'True':
        run([str(python), '-m', 'pip', 'check'], user=user, timeout=45)


def dispatch(data):
    role = data.get('role')
    if role not in ('participant', 'scenarioforge'):
        raise ValueError('Unknown application role')
    raw = Path(data['root'])
    if not raw.is_absolute() or raw.is_symlink() or '..' in raw.parts or not raw.is_dir():
        raise ValueError('Application root must be an existing non-symlink absolute checkout')
    root = raw.resolve()
    if value(root, 'rev-parse', '--show-toplevel') != str(root):
        raise ValueError('Application path must be a Git checkout root')
    directory = state_dir(root)
    journal = directory / 'state.json'
    op = data['op']
    if op == 'app_inspect':
        return dict(identity(root, role), root=str(root), update=read(journal), rollback=read(directory / 'last-success.json'))
    token = data.get('token', '')
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('Invalid maintenance request ID')
    if op == 'app_stage':
        # Exclusive upload destination; retries cannot overwrite another request.
        upload = directory / (token + '.bundle')
        if upload.exists() or upload.is_symlink():
            raise ValueError('Upload already exists; start a fresh update request')
        return {'path': str(upload)}
    if op not in ('app_update', 'app_rollback'):
        raise ValueError('Unknown maintenance operation')
    service = data.get('service')
    if service and not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', service):
        raise ValueError('Invalid application service')
    python = Path(data['python'])
    if not python.is_absolute() or not python.is_file():
        raise ValueError('Configured guest Python is unavailable')
    with maintenance():
        active_jobs()
        previous = read(journal)
        pending = read(PENDING)
        current = identity(root, role)
        if data.get('expected_revision') != current['revision']:
            raise ValueError('Installed revision changed; inspect the application again')
        if previous and previous['status'] in ('activating', 'restoring') and op != 'app_rollback':
            raise ValueError('Previous maintenance was interrupted; inspect guest state before another update')
        if PENDING.exists() and (op != 'app_rollback' or read(PENDING).get('root') != str(root)):
            raise ValueError('Interrupted maintenance needs rollback before another update')
        before = current['revision']
        if current['modified']:
            raise ValueError('Tracked local edits exist; preserve/commit them before updating. No files changed.')
        if op == 'app_rollback':
            previous = pending if pending else read(directory / 'last-success.json')
            if not previous or before not in (previous.get('revision'), previous.get('previous_revision')):
                raise ValueError('No matching completed update is available to roll back')
            target = previous['previous_revision']
            service = previous.get('service')
            python = Path(previous.get('python', str(python)))
        else:
            target = data['revision']
            if not re.fullmatch('[0-9a-f]{40}', target):
                raise ValueError('Invalid target revision')
            bundle = directory / (token + '.bundle')
            if not bundle.is_file() or bundle.is_symlink():
                raise ValueError('Source bundle is missing')
            if hashlib.sha256(bundle.read_bytes()).hexdigest() != data['sha256']:
                raise ValueError('Source bundle checksum mismatch')
            git(root, 'bundle', 'verify', str(bundle))
            git(root, 'fetch', '--no-tags', str(bundle), 'refs/heads/caf-release', timeout=120)
            if value(root, 'rev-parse', 'FETCH_HEAD') != target:
                raise ValueError('Fetched revision does not match the approved source')
        if before == target:
            if pending and op == 'app_rollback':
                if previous.get('service_was_active') and service:
                    run(['systemctl', 'start', service])
                    run(['systemctl', 'is-active', '--quiet', service])
                PENDING.unlink()
                previous.update(status='recovered', ended_at=time.time())
                save(journal, previous)
                return dict(identity(root, role), root=str(root), update=previous)
            raise ValueError('This revision is already installed')
        # Local edits remain untouched. Git checkout also refuses untracked-file collisions.
        staging = directory / ('stage-' + token)
        staging.mkdir(mode=0o700)
        os.chown(staging, root.stat().st_uid, root.stat().st_gid)
        stage = staging / 'checkout'
        record = dict(status='validating', token=token, role=role, previous_revision=before, revision=target,
                      service=service, python=str(python), service_was_active=False, started_at=time.time())
        save(journal, record)
        switched = False
        try:
            git(root, 'worktree', 'add', '--detach', str(stage), target)
            validate_checkout(stage, root, python, role, rollback=op == 'app_rollback')
            # Keep both revisions reachable even after normal Git reflog expiry/GC.
            for revision in (before, target):
                git(root, 'update-ref', 'refs/caf-orchestrator/releases/' + revision, revision)
            if not pending:
                save(PENDING, dict(record, root=str(root)))
            if service:
                state = run(['systemctl', 'is-active', service], check=False).stdout.strip()
                if state in (b'active', b'activating', b'reloading') or (pending and previous.get('service_was_active')):
                    record['service_was_active'] = True
                    save(journal, record)
                    if not pending:
                        save(PENDING, dict(record, root=str(root)))
                    run(['systemctl', 'stop', service], timeout=60)
            active_jobs()
            active_processes(root)
            record['status'] = 'activating'
            save(journal, record)
            git(root, 'checkout', '--detach', target)
            switched = True
            if record['service_was_active']:
                run(['systemctl', 'start', service], timeout=60)
                run(['systemctl', 'is-active', '--quiet', service])
            record['status'] = 'completed'
            save(directory / 'last-success.json', record)
            PENDING.unlink(missing_ok=True)
        except Exception as exc:
            record.update(status='restoring', error=str(exc))
            save(journal, record)
            if switched:
                if record['service_was_active']:
                    run(['systemctl', 'stop', service], timeout=60)
                git(root, 'checkout', '--detach', before)
            if record['service_was_active']:
                run(['systemctl', 'start', service], timeout=60)
                run(['systemctl', 'is-active', '--quiet', service])
            record['status'] = 'failed'
            if not pending:
                PENDING.unlink(missing_ok=True)
            raise
        finally:
            record['ended_at'] = time.time()
            save(journal, record)
            if stage.exists():
                git(root, 'worktree', 'remove', '--force', str(stage), check=False)
        return dict(identity(root, role), root=str(root), update=record)


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        raise SystemExit(1)
