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
import signal
import stat
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
    # Inspection must not refresh/write an index left by provisioning or a
    # service account. Required locks for fetch/reset remain enforced by Git.
    return run(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                '-c', 'core.fsmonitor=false', '-C', str(root), *args], user=user, **kwargs)


def value(root, *args):
    return git(root, *args).stdout.decode().strip()


def normalize_repository_metadata(root):
    """Repair root-owned Git metadata left by privileged provisioning.

    The application checkout itself defines the intended owner. Only root-owned
    entries are reassigned; metadata owned by any third account fails closed.
    Symlinks are never followed.
    """
    owner = root.stat()
    metadata = root / '.git'
    if os.geteuid() != 0 or owner.st_uid == 0 or not metadata.exists():
        return
    if metadata.is_symlink():
        raise ValueError('Git metadata cannot be a symlink')
    paths = [metadata]
    if metadata.is_dir():
        for directory, names, files in os.walk(metadata, followlinks=False):
            paths.extend(Path(directory) / name for name in [*names, *files])
    for path in paths:
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode):
            continue
        if current.st_uid not in (0, owner.st_uid):
            raise ValueError('Git metadata is owned by an unexpected account')
        if current.st_uid == 0:
            os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)


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


def preserve_identical_untracked(root, target, backup, record_move):
    """Preserve earlier hotfix copies only when identical to incoming Git blobs."""
    incoming = {}
    for entry in git(root, 'ls-tree', '-r', '-z', target).stdout.split(b'\0'):
        if entry:
            metadata, name = entry.split(b'\t', 1)
            mode, kind, digest = metadata.split()
            if kind == b'blob' and mode in (b'100644', b'100755'):
                incoming[os.fsdecode(name)] = digest.decode()
    for name in git(root, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split(b'\0'):
        if not name:
            continue
        relative = Path(os.fsdecode(name))
        if str(relative) not in incoming:
            continue
        source = root / relative
        if (relative.is_absolute() or '..' in relative.parts
                or any(parent.is_symlink() for parent in [source, *source.parents] if parent != root)
                or not source.is_file()):
            raise ValueError('Untracked source collision needs manual review: ' + str(relative))
        if value(root, 'hash-object', '--no-filters', '--', str(relative)) != incoming[str(relative)]:
            raise ValueError('Untracked file differs from the incoming revision; preserve or move it before updating: ' + str(relative))
        destination = backup / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record_move(str(relative))
        source.rename(destination)


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
                missing_controls=controls, tools_config_replaceable=role == 'participant' and legacy_tools_edit(root))


def legacy_tools_edit(root):
    """Only the legacy, unstaged runtime catalog qualifies for replacement."""
    return (git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=no').stdout == b' M kali_tools.json\0'
            and (root / 'kali_tools.json').is_file() and not (root / 'kali_tools.json').is_symlink())


def active_jobs():
    result = run(['systemctl', 'list-units', '--all', '--state=active,activating', '--no-legend', '--plain',
                  'caf-eval-*', 'caf-orchestrator-*', 'caf-sample-*'])
    if result.stdout.strip():
        raise ValueError('An experiment or workflow is still running inside this VM')


def application_processes(root, proc_root='/proc'):
    # Fail closed on unreadable processes. Ignore only this helper and its ancestors.
    proc_root = Path(proc_root)
    boot = (proc_root / 'sys/kernel/random/boot_id').read_bytes().strip()
    ignored, pid = {os.getpid()}, os.getppid()
    while pid > 1 and pid not in ignored:
        ignored.add(pid)
        try:
            pid = int((proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except FileNotFoundError:
            break
    found = []
    for proc in proc_root.iterdir():
        if not proc.name.isdecimal() or int(proc.name) in ignored:
            continue
        try:
            started = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            raw_command = (proc / 'cmdline').read_bytes()
            argv = raw_command.split(b'\0')
            command = b' '.join(argv).decode(errors='replace')
            cwd = os.readlink(proc / 'cwd')
            name = (proc / 'comm').read_text().strip()
            executable = os.readlink(proc / 'exe')
            uid = proc.stat().st_uid
            if started != (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]:
                continue  # PID changed while reading; the final idle check rechecks.
        except FileNotFoundError:
            continue
        if len(argv) > 2 and argv[1] == b'-c' and argv[2].startswith(b'"""Read-only, stdlib-only Linux guest probe, sent through QEMU Guest Agent.'):
            continue
        reasons = []
        if re.search(re.escape(str(root)) + r'''(?=/|$|[\s'"\x00])''', command):
            reasons.append('command references checkout')
        if cwd == str(root) or cwd.startswith(str(root) + '/'):
            reasons.append('working directory is in checkout')
        if reasons:
            # Names and reasons identify blockers without exposing command arguments,
            # which can contain API keys, inline scripts or other credentials.
            found.append(dict(pid=int(proc.name), name=re.sub(r'[^A-Za-z0-9_. -]', '?', name)[:60],
                              reason='; '.join(reasons),
                              identity=hashlib.sha256(b'\0'.join([boot, started.encode(), raw_command,
                                                                cwd.encode(), executable.encode(), str(uid).encode()])).hexdigest()))
            if len(found) == 12:
                break
    return found


def active_processes(root):
    found = application_processes(root)
    if found:
        labels = '; '.join(f"PID {p['pid']} ({p['name']}: {p['reason']})" for p in found)
        raise ValueError('Processes still reference the application checkout: ' + labels +
                         '. Stop the application/workers or move idle shells out of the checkout, then retry.')


def stop_confirmed_processes(root, approved, record_signal, timeout=15):
    """Signal only confirmed process instances; pidfds prevent PID-reuse races."""
    if (not isinstance(approved, list) or not 1 <= len(approved) <= 12 or
            any(not isinstance(p, dict) or type(p.get('pid')) is not int or p['pid'] <= 1 or
                not isinstance(p.get('identity'), str) or not re.fullmatch('[0-9a-f]{64}', p['identity']) for p in approved)):
        raise ValueError('A fresh process inspection and confirmation are required')
    expected = {p['pid']: p['identity'] for p in approved}
    if len(expected) != len(approved):
        raise ValueError('Duplicate process confirmation')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('Guest requires Linux pidfd support for confirmed process stopping')
    handles = {}
    def checked():
        found = application_processes(root)
        if any(expected.get(p['pid']) != p['identity'] for p in found):
            raise ValueError('Application processes changed; check version and confirm the new process list')
        return found
    try:
        for process in checked():
            try:
                handles[process['pid']] = os.pidfd_open(process['pid'])
            except ProcessLookupError:
                pass
        # Recheck after acquiring handles, before sending any signals.
        live = {p['pid'] for p in checked()}
        for pid, handle in handles.items():
            if pid not in live:
                continue
            current = {p['pid']: p['identity'] for p in checked()}
            if current.get(pid) != expected[pid]:
                continue  # The confirmed process already exited or left the checkout.
            try:
                signal.pidfd_send_signal(handle, signal.SIGTERM)
                record_signal(pid)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while application_processes(root) and time.monotonic() < deadline:
            time.sleep(0.25)
        active_processes(root)
    finally:
        for handle in handles.values():
            os.close(handle)


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
    normalize_repository_metadata(root)
    if value(root, 'rev-parse', '--show-toplevel') != str(root):
        raise ValueError('Application path must be a Git checkout root')
    directory = state_dir(root)
    journal = directory / 'state.json'
    op = data['op']
    force = data.get('force', False)
    if type(force) is not bool or (force and op != 'app_update'):
        raise ValueError('Force is supported only for application updates')
    if 'stop_processes' in data and op != 'app_update':
        raise ValueError('Confirmed process stopping is only supported for updates')
    if op == 'app_inspect':
        return dict(identity(root, role), root=str(root), processes=application_processes(root),
                    update=read(journal), rollback=read(directory / 'last-success.json'))
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
        replace_tools = op == 'app_update' and current['tools_config_replaceable']
        if current['modified'] and not replace_tools and not force:
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
            if before != target:
                bundle = directory / (token + '.bundle')
                if not bundle.is_file() or bundle.is_symlink():
                    raise ValueError('Source bundle is missing')
                if hashlib.sha256(bundle.read_bytes()).hexdigest() != data.get('sha256'):
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
            if not force:
                raise ValueError('This revision is already installed')
        # Validate the incoming checkout before changing the live application.
        staging = directory / ('stage-' + token)
        staging.mkdir(mode=0o700)
        os.chown(staging, root.stat().st_uid, root.stat().st_gid)
        stage = staging / 'checkout'
        record = dict(status='validating', token=token, role=role, previous_revision=before, revision=target,
                      service=service, python=str(python), service_was_active=False, started_at=time.time())
        if force:
            record.update(forced=True, discarded_modified_files=current['modified_files'],
                          discarded_modified_file_count=current['modified_file_count'])
        save(journal, record)
        switched = False
        try:
            git(root, 'worktree', 'add', '--detach', str(stage), target)
            if before != target:
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
            if data.get('stop_processes'):
                def record_signal(pid):
                    record.setdefault('signaled_pids', []).append(pid)
                    save(journal, record)
                    save(PENDING, dict(record, root=str(root)))
                stop_confirmed_processes(root, data['stop_processes'], record_signal)
            active_processes(root)
            if replace_tools:
                if value(stage, 'ls-files', '--', 'kali_tools.json') or not (stage / 'kali_tools.default.json').is_file():
                    raise ValueError('Target still tracks the runtime catalog; select a CAF revision with kali_tools.default.json')
                if not legacy_tools_edit(root):
                    raise ValueError('Tracked edits changed during validation; inspect again')
                backup = directory / (token + '-kali_tools.json.backup')
                with backup.open('xb') as stream:
                    os.chmod(backup, 0o600)
                    stream.write((root / 'kali_tools.json').read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                record['tools_config_backup'] = str(backup)
                save(journal, record)
                save(PENDING, dict(record, root=str(root)))
                git(root, 'restore', '--worktree', '--', 'kali_tools.json')
            backup = directory / (token + '-untracked-backup')
            def record_move(relative):
                record['untracked_backup'] = str(backup)
                record.setdefault('preserved_untracked_files', []).append(relative)
                save(journal, record)
                save(PENDING, dict(record, root=str(root)))
            preserve_identical_untracked(root, target, backup, record_move)
            if force:
                # The user explicitly confirmed this irreversible step.
                git(root, 'reset', '--hard', 'HEAD')
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
            for relative in record.get('preserved_untracked_files', []):
                backup_file = Path(record['untracked_backup']) / relative
                if backup_file.exists():
                    backup_file.rename(root / relative)
            if record.get('tools_config_backup'):
                (root / 'kali_tools.json').write_bytes(Path(record['tools_config_backup']).read_bytes())
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
