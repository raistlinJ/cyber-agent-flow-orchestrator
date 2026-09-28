import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import update_guest as guest, updates
from cyber_agent_flow_orchestrator.access import AccessDenied
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.workspaces import Workspace
from cyber_agent_flow_orchestrator.diagnostics import Trace
from test_pve_auth import pve, make_auth
from test_user_access import access, vm, Probe
from test_workflow import lab
from test_web import request
from https_fixture import secure_server, PASSWORD
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from sample_fixture import install_backend


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture(params=['participant', 'scenarioforge'])
def installation(tmp_path, monkeypatch, request):
    role = request.param
    source = tmp_path / 'upstream'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    git(source, 'config', 'user.email', 'test@example.invalid')
    git(source, 'config', 'user.name', 'Test')
    if role == 'participant':
        (source / 'mcp_client.py').write_text('class MCPSession:\n def __init__(self): pass\n')
        (source / 'mcp_kali.py').write_text('')
        (source / 'session_logger.py').write_text('')
        (source / 'kali_tools.json').write_text('{"tools": [{"name": "base"}]}')
    else:
        for name in ('scenarioforge', 'webapp'):
            (source / name).mkdir()
            (source / name / '__init__.py').write_text('')
        (source / 'scenarioforge/cli.py').write_text('VERSION = 1\n')
        (source / 'webapp/app_backend.py').write_text('')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Original installation')
    original = git(source, 'rev-parse', 'HEAD')
    installed = tmp_path / 'installed app'
    subprocess.run(['git', 'clone', str(source), str(installed)], check=True, capture_output=True)
    (installed / 'config.local.yaml').write_text('keep: my settings\n')
    (installed / 'generated').mkdir()
    (installed / 'generated/tool.json').write_text('keep my tool')
    if role == 'participant':
        (source / 'mcp_client.py').write_text('class MCPSession:\n def __init__(self, *, allowed_tools=None, guidance_text=None, reveal_network_policy=True): pass\n')
        git(source, 'mv', 'kali_tools.json', 'kali_tools.default.json')
    else:
        (source / 'scenarioforge/cli.py').write_text('VERSION = 2\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Application update')
    monkeypatch.setattr(guest, 'STATE_ROOT', tmp_path / 'guest-state')
    monkeypatch.setattr(guest, 'LOCK_PATH', tmp_path / 'guest-maintenance.lock')
    monkeypatch.setattr(guest, 'PENDING', tmp_path / 'guest-maintenance.pending')
    monkeypatch.setattr(guest, 'active_processes', lambda root: None)
    original_run = guest.run
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'systemctl':
            return subprocess.CompletedProcess(argv, 0, b'active\n' if argv[1] == 'is-active' else b'', b'')
        kwargs.pop('user', None)  # Real Git/Python; simulate only OS user switching.
        if len(argv) == 3 and argv[1] == '-c' and argv[2] == 'import scenarioforge.cli; import flask, lxml, yaml, psutil':
            # The tiny fixture has no Flask runtime. Still import the real staged
            # fixture package; production checks the installed SF dependencies.
            argv = [argv[0], '-c', 'import scenarioforge.cli']
        return original_run(argv, **kwargs)
    monkeypatch.setattr(guest, 'run', run)
    return role, source, installed, original, calls


def stage(installation, tmp_path, token='a' * 32):
    role, source, root, original, _ = installation
    output = tmp_path / token
    output.mkdir()
    target, bundle = updates.package({'url': str(source)}, 'main', original, output)
    args = dict(role=role, root=str(root), token=token)
    upload = guest.dispatch(dict(args, op='app_stage'))
    Path(upload['path']).write_bytes(bundle.read_bytes())
    return dict(args, python=sys.executable, expected_revision=original, revision=target,
                sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(), service='demo.service')


def test_real_git_package_offline_update_and_rollback_preserve_data(installation, tmp_path):
    role, _, root, original, calls = installation
    args = stage(installation, tmp_path)
    result = guest.dispatch(dict(args, op='app_update'))
    assert result['revision'] == args['revision']
    assert result['update']['status'] == 'completed'
    if role == 'participant': assert result['missing_controls'] == []
    assert (root / 'config.local.yaml').read_text() == 'keep: my settings\n'
    (root / 'generated/after-update.json').write_text('new data survives rollback')
    result = guest.dispatch(dict(args, op='app_rollback', expected_revision=result['revision'], token='b' * 32))
    assert result['revision'] == original
    assert (root / 'generated/after-update.json').read_text() == 'new data survives rollback'
    assert (root / 'generated/tool.json').read_text() == 'keep my tool'
    assert not guest.PENDING.exists()
    assert ['systemctl', 'stop', 'demo.service'] in calls
    assert ['systemctl', 'start', 'demo.service'] in calls


def test_dirty_sources_dependency_changes_and_checksums_fail_without_activation(installation, tmp_path):
    _, source, root, original, calls = installation
    args = stage(installation, tmp_path)
    with pytest.raises(ValueError, match='checksum'):
        guest.dispatch(dict(args, op='app_update', sha256='0' * 64))
    tracked = root / ('mcp_client.py' if installation[0] == 'participant' else 'scenarioforge/cli.py')
    previous = tracked.read_text()
    tracked.write_text(previous + '# local edit\n')
    with pytest.raises(ValueError, match='Tracked local edits'):
        guest.dispatch(dict(args, op='app_update'))
    assert '# local edit' in tracked.read_text()
    tracked.write_text(previous)
    (source / 'requirements.txt').write_text('unavailable-package==99\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Dependency change')
    args = stage(installation, tmp_path, 'c' * 32)
    with pytest.raises(ValueError, match='Dependency manifest changed'):
        guest.dispatch(dict(args, op='app_update'))
    assert git(root, 'rev-parse', 'HEAD') == original
    assert ['systemctl', 'stop', 'demo.service'] not in calls
    assert not guest.PENDING.exists()


def test_failed_service_start_restores_source_and_restarts_previous_version(installation, tmp_path, monkeypatch):
    args = stage(installation, tmp_path)
    original_run = guest.run
    starts = []
    def run(argv, **kwargs):
        if argv[:2] == ['systemctl', 'start']:
            starts.append(argv)
            if len(starts) == 1: raise ValueError('Service failed after update')
        return original_run(argv, **kwargs)
    monkeypatch.setattr(guest, 'run', run)
    with pytest.raises(ValueError, match='Service failed'):
        guest.dispatch(dict(args, op='app_update'))
    assert git(installation[2], 'rev-parse', 'HEAD') == installation[3]
    assert len(starts) == 2 and not guest.PENDING.exists()


def test_active_experiment_refuses_service_stop_or_update(installation, tmp_path, monkeypatch):
    args = stage(installation, tmp_path)
    def active(): raise ValueError('An experiment is running')
    monkeypatch.setattr(guest, 'active_jobs', active)
    with pytest.raises(ValueError, match='experiment'):
        guest.dispatch(dict(args, op='app_update'))
    assert git(installation[2], 'rev-parse', 'HEAD') == installation[3]
    assert ['systemctl', 'stop', 'demo.service'] not in installation[4]


def test_settings_reject_arbitrary_urls_refs_and_disable():
    assert updates.settings(False) is None
    assert updates.settings(None)['group'] == 'caf-maintainers'
    for url in ('file:///tmp/repo', 'https://user:password@example.com/repo', 'http://example.com/repo'):
        with pytest.raises(ValueError): updates.settings({'participant': {'url': url}})
    for ref in ('--upload-pack=evil', '../main', 'branch:destination', 'main; echo bad'):
        with pytest.raises(updates.UpdateError): updates.ref_name(ref)


def test_maintenance_group_and_vm_permission_rechecked(pve):
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    user = access(pve)
    with pytest.raises(AccessDenied): updates.require_maintenance(user, 'caf-maintainers')
    pve[0]['groups'] += ',caf-maintainers'
    updates.require_maintenance(user, 'caf-maintainers')
    pve[0]['groups'] = 'caf-orchestration'
    with pytest.raises(AccessDenied): updates.require_maintenance(user, 'caf-maintainers')


def test_https_maintenance_whitelist_csrf_group_and_scoped_inspection(pve, lab, tmp_path, monkeypatch):
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    calls, agent, _ = install_backend(monkeypatch, tmp_path)
    original = agent.call
    def call(self, vmid, op, **data):
        original(self, vmid, op, **data)
        return {'revision': 'a' * 40, 'missing_controls': [], 'modified': False}
    monkeypatch.setattr(agent, 'call', call)
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        with secure_server(dash, tmp_path / 'web', auth=make_auth(pve)) as server:
            _, headers, _ = request(server, '/api/login', {'username': 'operator@pve', 'password': PASSWORD})
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
            headers = {'X-CSRF-Token': csrf}
            request(server, '/api/roles', dict(scenarioforge=None, participant=9403, core=None), cookie=cookie, headers=headers)
            data = dict(role='participant', action='update', ref='main', request_id='a' * 32)
            assert request(server, '/api/applications', data)[0] == 401
            assert request(server, '/api/applications', data, cookie=cookie)[0] == 403
            assert request(server, '/api/applications', data, cookie=cookie, headers=headers)[0] == 403
            assert request(server, '/api/applications', dict(data, vmid=999), cookie=cookie, headers=headers)[0] == 400
            confirmed = dict(data, process_confirmation='b' * 32)
            assert request(server, '/api/applications', confirmed, cookie=cookie)[0] == 403
            assert request(server, '/api/applications', confirmed, cookie=cookie, headers=headers)[0] == 403
            assert request(server, '/api/applications', dict(data, stop_processes=[42]), cookie=cookie, headers=headers)[0] == 400
            data['action'] = 'inspect'
            assert request(server, '/api/applications', data, cookie=cookie, headers=headers)[0] == 202
            dash.updates.jobs['operator@pve'].result(timeout=10)
            assert {vmid for vmid, _, _ in calls} == {9403}
            workspace = Workspace(tmp_path / 'runs', 'operator@pve')
            row = ev.read_json(workspace.path / 'updates' / ('a' * 32) / 'job.json')
            assert row['status'] == 'completed'
            scoped = dash.updates.view(access(pve), workspace, workspace.roles())
            assert any(event['kind'] == 'response' for event in scoped['jobs'][0]['console']['events'])
            other = access(pve, 'another@pve')
            other_workspace = Workspace(tmp_path / 'runs', other.username)
            assert dash.updates.view(other, other_workspace, other_workspace.roles())['jobs'] == []
            dash.updates.config = None
            assert request(server, '/api/applications', dict(data, request_id='b' * 32), cookie=cookie, headers=headers)[0] == 400
    finally:
        dash.close()


def test_interrupted_activation_can_roll_back_and_clears_guest_launch_block(installation, tmp_path):
    args = stage(installation, tmp_path)
    result = guest.dispatch(dict(args, op='app_update'))
    directory = guest.state_dir(installation[2])
    interrupted = dict(result['update'], status='activating', root=str(installation[2]))
    guest.save(guest.PENDING, interrupted)
    guest.save(directory / 'state.json', interrupted)
    with pytest.raises(ValueError, match='interrupted'):
        guest.dispatch(dict(args, op='app_update', expected_revision=result['revision']))
    restored = guest.dispatch(dict(args, op='app_rollback', expected_revision=result['revision'], token='d' * 32))
    assert restored['revision'] == installation[3]
    assert not guest.PENDING.exists()


def test_missing_runtime_dependency_prevents_activation(installation, tmp_path, monkeypatch):
    args = stage(installation, tmp_path)
    original_run = guest.run
    def run(argv, **kwargs):
        if len(argv) == 3 and argv[1] == '-c' and argv[2].startswith(('import mcp_client', 'import scenarioforge.cli')):
            raise ValueError('Missing application dependency')
        return original_run(argv, **kwargs)
    monkeypatch.setattr(guest, 'run', run)
    with pytest.raises(ValueError, match='Missing application dependency'):
        guest.dispatch(dict(args, op='app_update'))
    assert git(installation[2], 'rev-parse', 'HEAD') == installation[3]
    assert ['systemctl', 'stop', 'demo.service'] not in installation[4]


def test_manager_revocation_after_inspection_prevents_download_or_update(pve, lab, tmp_path, monkeypatch):
    pve[0]['groups'] += ',caf-maintainers'
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=None, participant=9403, core=None), user)
    calls, agent, _ = install_backend(monkeypatch, tmp_path)
    original = agent.call
    def call(self, vmid, op, **data):
        original(self, vmid, op, **data)
        pve[0]['groups'] = 'caf-orchestration'
        return {'revision': 'a' * 40}
    monkeypatch.setattr(agent, 'call', call)
    monkeypatch.setattr(updates, 'package', lambda *args: pytest.fail('Download after permission revocation'))
    cfg, runtime, _, _ = load(lab[0])
    manager = updates.UpdateManager(cfg, runtime, tmp_path / 'runs')
    try:
        manager.submit(user, 'participant', 'update', 'main', 'a' * 32)
        manager.jobs[user.username].result(timeout=10)
        row = ev.read_json(workspace.path / 'updates' / ('a' * 32) / 'job.json')
        assert row['status'] == 'failed'
        assert [op for _, op, _ in calls] == ['app_inspect']
    finally:
        manager.close()


def test_annotated_tag_resolves_to_commit_and_release_refs_keep_rollback(installation, tmp_path):
    role, source, root, original, _ = installation
    git(source, 'tag', '-a', 'release-test', '-m', 'Approved release')
    output = tmp_path / 'tag-package'
    output.mkdir()
    trace = Trace(output)
    revision, bundle = updates.package({'url': str(source)}, 'release-test', original, output, trace=trace)
    console = trace.path.read_text()
    assert 'fetch --no-tags' in console and 'Host Git exit 0' in console and revision in console
    assert revision == git(source, 'rev-parse', 'main')
    assert revision != git(source, 'rev-parse', 'release-test')
    args = stage(installation, tmp_path)
    guest.dispatch(dict(args, op='app_update'))
    assert git(root, 'rev-parse', 'refs/caf-orchestrator/releases/' + original) == original
    assert git(root, 'rev-parse', 'refs/caf-orchestrator/releases/' + revision) == revision


def test_inspection_lists_tracked_paths_without_contents(installation):
    role, _, root, _, _ = installation
    assert guest.identity(root, role)['modified_files'] == []
    tracked = 'mcp_client.py' if role == 'participant' else 'scenarioforge/cli.py'
    renamed = 'renamed file\nwith newline.py'
    git(root, 'mv', tracked, renamed)
    (root / renamed).write_text((root / renamed).read_text() + '# private content\n')
    info = guest.identity(root, role)
    assert info['modified'] and info['modified_file_count'] == 1
    assert info['modified_files'] == ['RM ' + json.dumps(renamed) + ' <- ' + json.dumps(tracked)]
    assert 'private content' not in json.dumps(info)
    assert 'config.local.yaml' not in json.dumps(info)


@pytest.mark.parametrize('action', ['update', 'rollback'])
def test_manager_dirty_preflight_stops_before_download_or_transfer(pve, lab, tmp_path, monkeypatch, action):
    pve[0]['groups'] += ',caf-maintainers'
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=None, participant=9403, core=None), user)
    calls, agent, _ = install_backend(monkeypatch, tmp_path)
    original = agent.call
    def call(self, vmid, op, **data):
        original(self, vmid, op, **data)
        return {'revision': 'a' * 40, 'modified': True, 'modified_files': [' M "mcp_client.py"']}
    monkeypatch.setattr(agent, 'call', call)
    monkeypatch.setattr(updates, 'package', lambda *a, **kw: pytest.fail('Downloaded source for dirty checkout'))
    cfg, runtime, _, _ = load(lab[0])
    manager = updates.UpdateManager(cfg, runtime, tmp_path / 'runs')
    try:
        manager.submit(user, 'participant', action, 'main', 'a' * 32)
        manager.jobs[user.username].result(timeout=10)
        row = ev.read_json(workspace.path / 'updates' / ('a' * 32) / 'job.json')
        assert row['status'] == 'failed'
        assert 'No source bundle downloaded or transferred' in row['error']
        assert row['installed']['modified_files'] == [' M "mcp_client.py"']
        assert [op for _, op, _ in calls] == ['app_inspect']
        assert not (workspace.path / 'updates' / ('a' * 32) / 'source.bundle').exists()
    finally:
        manager.close()


def test_changed_path_output_is_bounded(monkeypatch):
    records = [b' M ' + ('\u2603' * 300 + str(i)).encode() for i in range(70)]
    monkeypatch.setattr(guest, 'git', lambda *a: subprocess.CompletedProcess([], 0, stdout=b'\0'.join(records) + b'\0'))
    files, count = guest.tracked_changes(Path('/unused'))
    assert count == 70 and 0 < len(files) <= 50
    assert len(json.dumps(files).encode()) <= 8002


def test_legacy_runtime_catalog_replaced_and_backed_up(installation, tmp_path):
    role, _, root, original, _ = installation
    if role != 'participant': pytest.skip('CAF catalog only')
    content = b'{"tools": [{"name": "custom"}]}\n'
    (root / 'kali_tools.json').write_bytes(content)
    assert guest.identity(root, role)['tools_config_replaceable']
    args = stage(installation, tmp_path)
    result = guest.dispatch(dict(args, op='app_update'))
    assert not result['modified']
    assert not (root / 'kali_tools.json').exists()
    assert (root / 'kali_tools.default.json').is_file()
    assert Path(result['update']['tools_config_backup']).read_bytes() == content
    restored = guest.dispatch(dict(args, op='app_rollback', expected_revision=result['revision'], token='e' * 32))
    assert restored['revision'] == original and not restored['modified']


@pytest.mark.parametrize('problem', ['other-edit', 'staged', 'old-target', 'restart'])
def test_runtime_catalog_replacement_refusals_and_failure(installation, tmp_path, monkeypatch, problem):
    role, source, root, original, _ = installation
    if role != 'participant': pytest.skip('CAF catalog only')
    content = b'{"tools": [{"name": "custom"}]}\n'
    (root / 'kali_tools.json').write_bytes(content)
    if problem == 'other-edit': (root / 'mcp_client.py').write_text('# other edit')
    if problem == 'staged': git(root, 'add', 'kali_tools.json')
    if problem == 'old-target':
        git(source, 'mv', 'kali_tools.default.json', 'kali_tools.json')
        git(source, 'commit', '-m', 'Legacy target')
    if problem == 'restart':
        old_run, starts = guest.run, []
        def run(argv, **kwargs):
            if argv[:2] == ['systemctl', 'start']:
                starts.append(argv)
                if len(starts) == 1: raise ValueError('Restart failed')
            return old_run(argv, **kwargs)
        monkeypatch.setattr(guest, 'run', run)
    args = stage(installation, tmp_path)
    with pytest.raises(ValueError): guest.dispatch(dict(args, op='app_update'))
    assert git(root, 'rev-parse', 'HEAD') == original
    assert (root / 'kali_tools.json').read_bytes() == content


def fake_process(proc_root, pid, argv, cwd, name='python3'):
    boot = proc_root / 'sys/kernel/random/boot_id'
    boot.parent.mkdir(parents=True, exist_ok=True)
    boot.write_text('test-boot')
    folder = proc_root / str(pid)
    folder.mkdir()
    (folder / 'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv) + b'\0')
    (folder / 'cwd').symlink_to(cwd)
    (folder / 'comm').write_text(name + '\n')
    (folder / 'exe').symlink_to('/usr/bin/' + name)
    (folder / 'stat').write_text(f'{pid} ({name}) S 1 ' + '0 ' * 17 + '1234')
    return folder


def test_process_inspection_identifies_blockers_without_arguments(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    root = Path('/opt/cyber-agent-flow')
    fake_process(proc, 900001, ['/opt/cyber-agent-flow/venv/bin/python', 'app.py', '--api-key', 'secret-token'], str(root))
    fake_process(proc, 900002, ['bash'], str(root / 'tools'), 'bash')
    fake_process(proc, 900003, ['python', '/opt/cyber-agent-flow-other/app.py'], '/tmp')
    fake_process(proc, 900004, ['python', '-c', '"""Read-only, stdlib-only Linux guest probe, sent through QEMU Guest Agent.', str(root)], '/tmp')
    fake_process(proc, os.getpid(), ['python', str(root / 'helper.py')], str(root))
    found = guest.application_processes(root, proc_root=proc)
    assert {p['pid'] for p in found} == {900001, 900002}
    assert 'secret-token' not in json.dumps(found)
    assert 'app.py' not in json.dumps(found)
    assert next(p for p in found if p['pid'] == 900002)['reason'] == 'working directory is in checkout'
    old_identity = next(p for p in found if p['pid'] == 900002)['identity']
    stat = proc / '900002/stat'
    stat.write_text(stat.read_text().replace('1234', '5678'))
    assert next(p for p in guest.application_processes(root, proc_root=proc) if p['pid'] == 900002)['identity'] != old_identity


def test_process_guard_has_actionable_error_and_inspect_is_read_only(installation, monkeypatch):
    role, _, root, original, calls = installation
    found = [{'pid': 42, 'name': 'python3', 'reason': 'working directory is in checkout'}]
    monkeypatch.setattr(guest, 'application_processes', lambda root: found)
    result = guest.dispatch(dict(op='app_inspect', role=role, root=str(root)))
    assert result['processes'] == found
    assert result['revision'] == original
    assert not any(argv[:2] == ['systemctl', 'stop'] for argv in calls)
    # The integration fixture disables active_processes; test its real function separately below.


def test_final_process_guard_reports_pid(monkeypatch):
    monkeypatch.setattr(guest, 'application_processes', lambda root: [{'pid': 42, 'name': 'bash', 'reason': 'working directory is in checkout'}])
    with pytest.raises(ValueError, match=r'PID 42.*bash.*move idle shells'):
        guest.active_processes(Path('/opt/cyber-agent-flow'))


@pytest.mark.parametrize('managed_service', [None, 'caf-web.service'])
def test_manager_checks_unmanaged_processes_before_source_download(pve, lab, tmp_path, monkeypatch, managed_service):
    pve[0]['groups'] += ',caf-maintainers'
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=None, participant=9403, core=None), user)
    calls, agent, _ = install_backend(monkeypatch, tmp_path)
    original = agent.call
    def call(self, vmid, op, **data):
        original(self, vmid, op, **data)
        return {'revision': 'a' * 40, 'modified': False,
                'processes': [{'pid': 42, 'name': 'python3', 'reason': 'working directory is in checkout'}]}
    monkeypatch.setattr(agent, 'call', call)
    packages = []
    def package(*args, **kwargs):
        packages.append(args)
        raise updates.UpdateError('Reached packaging; service is managed')
    monkeypatch.setattr(updates, 'package', package)
    cfg, runtime, _, _ = load(lab[0])
    if managed_service:
        cfg.setdefault('monitoring', {})['caf_service'] = managed_service
    manager = updates.UpdateManager(cfg, runtime, tmp_path / 'runs')
    try:
        manager.submit(user, 'participant', 'update', 'main', 'a' * 32)
        manager.jobs[user.username].result(timeout=10)
        row = ev.read_json(workspace.path / 'updates' / ('a' * 32) / 'job.json')
        assert row['status'] == 'failed'
        assert bool(packages) == bool(managed_service)
        if not managed_service:
            assert 'PID 42 (python3' in row['error']
            assert 'No source bundle downloaded or transferred' in row['error']
        assert [op for _, op, _ in calls] == ['app_inspect']
    finally:
        manager.close()


def process_record(pid=42, identity='a' * 64):
    return dict(pid=pid, identity=identity, name='python3', reason='working directory is in checkout')


@pytest.mark.parametrize('mode', ['exit', 'stale', 'reused', 'timeout', 'gone'])
def test_confirmed_stop_revalidates_instances_and_never_force_kills(monkeypatch, mode):
    approved = [process_record()]
    live = [process_record(identity='b' * 64)] if mode == 'stale' else approved.copy()
    if mode == 'gone': live.clear()
    signals, closed, recorded = [], [], []
    monkeypatch.setattr(guest, 'application_processes', lambda root: live.copy())
    def open_pid(pid):
        if mode == 'reused': live[:] = [process_record(identity='c' * 64)]
        return 999
    def send(handle, sig):
        signals.append((handle, sig))
        if mode == 'exit': live.clear()
    monkeypatch.setattr(guest.os, 'pidfd_open', open_pid, raising=False)
    monkeypatch.setattr(guest.signal, 'pidfd_send_signal', send, raising=False)
    monkeypatch.setattr(guest.os, 'close', closed.append)
    if mode in ('stale', 'reused', 'timeout'):
        with pytest.raises(ValueError, match='processes changed|Processes still reference'):
            guest.stop_confirmed_processes(Path('/opt/caf'), approved, recorded.append, timeout=0)
    else:
        guest.stop_confirmed_processes(Path('/opt/caf'), approved, recorded.append, timeout=0)
    assert signals == ([(999, guest.signal.SIGTERM)] if mode in ('exit', 'timeout') else [])
    assert recorded == ([42] if signals else [])
    assert closed == ([] if mode in ('stale', 'gone') else [999])


def test_confirmed_stop_rejects_missing_pidfd_support(monkeypatch):
    monkeypatch.delattr(guest.os, 'pidfd_open', raising=False)
    with pytest.raises(ValueError, match='pidfd support'):
        guest.stop_confirmed_processes(Path('/opt/caf'), [process_record()], lambda pid: None)


def test_confirmed_stop_is_journaled_before_source_activation(installation, tmp_path, monkeypatch):
    args = stage(installation, tmp_path)
    args['service'] = None
    root, original = installation[2:4]
    def stop(path, approved, record_signal):
        assert path == root and approved == [process_record()]
        assert git(root, 'rev-parse', 'HEAD') == original
        assert guest.PENDING.exists()
        record_signal(42)
        assert guest.read(guest.PENDING)['signaled_pids'] == [42]
    monkeypatch.setattr(guest, 'stop_confirmed_processes', stop)
    result = guest.dispatch(dict(args, op='app_update', stop_processes=[process_record()]))
    assert result['update']['signaled_pids'] == [42]
    assert result['revision'] == args['revision']
    assert not any(argv[:2] == ['systemctl', 'start'] for argv in installation[4])


@pytest.mark.parametrize('case', ['accepted', 'changed', 'wrong-vm', 'wrong-root', 'other-user', 'rollback'])
def test_manager_scopes_confirmation_and_rechecks_before_transfer(pve, lab, tmp_path, monkeypatch, case):
    pve[0]['groups'] += ',caf-maintainers'
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=None, participant=9403, core=None), user)
    calls, agent, _ = install_backend(monkeypatch, tmp_path)
    original = agent.call
    def call(self, vmid, op, **data):
        original(self, vmid, op, **data)
        return {'revision': 'a' * 40, 'modified': False,
                'processes': [process_record(identity=('b' if case == 'changed' else 'a') * 64)]}
    monkeypatch.setattr(agent, 'call', call)
    packages = []
    def package(*args, **kwargs):
        packages.append(args)
        raise updates.UpdateError('Reached packaging after confirmation')
    monkeypatch.setattr(updates, 'package', package)
    cfg, runtime, _, _ = load(lab[0])
    manager = updates.UpdateManager(cfg, runtime, tmp_path / 'runs')
    try:
        manager.submit(user, 'participant', 'inspect', 'main', 'a' * 32)
        manager.jobs[user.username].result(timeout=10)
        path = workspace.path / 'updates' / ('a' * 32) / 'job.json'
        prior = ev.read_json(path)
        prior['installed']['processes'] = [process_record()]
        if case == 'wrong-vm': prior['vmid'] = 9402
        if case == 'wrong-root': prior['root'] = '/somewhere/else'
        ev.write_json(path, prior)
        if case == 'other-user':
            other = Workspace(tmp_path / 'runs', 'other@pve').path / 'updates' / ('a' * 32)
            other.mkdir(parents=True)
            path.rename(other / 'job.json')
        if case in ('wrong-vm', 'wrong-root', 'other-user', 'rollback'):
            with pytest.raises(updates.UpdateError):
                manager.submit(user, 'participant', 'rollback' if case == 'rollback' else 'update', 'main', 'b' * 32, 'a' * 32)
            assert not packages
        else:
            manager.submit(user, 'participant', 'update', 'main', 'b' * 32, 'a' * 32)
            manager.jobs[user.username].result(timeout=10)
            row = ev.read_json(workspace.path / 'updates' / ('b' * 32) / 'job.json')
            assert row['stop_processes'] == [process_record()]
            assert bool(packages) == (case == 'accepted')
            assert ('processes changed' if case == 'changed' else 'Reached packaging') in row['error']
            with pytest.raises(updates.UpdateError, match='another process confirmation'):
                manager.submit(user, 'participant', 'update', 'main', 'b' * 32)
    finally:
        manager.close()
