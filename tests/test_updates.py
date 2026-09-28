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
