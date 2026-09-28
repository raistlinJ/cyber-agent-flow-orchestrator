import json
from pathlib import Path
import stat

import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import guest_model_config as guest
from cyber_agent_flow_orchestrator.model_config import ModelConfigs, ModelConfigError, apply_model
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.access import AccessDenied
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_user_access import access, vm
from test_pve_auth import pve, make_auth
from test_workflow import lab
from test_web import request
from https_fixture import secure_server, PASSWORD
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from test_user_access import Probe


@pytest.fixture
def guest_root(tmp_path, monkeypatch):
    root = (tmp_path / 'application').resolve()
    root.mkdir()
    monkeypatch.setattr(guest, 'LOCK', tmp_path / 'maintenance.lock')
    monkeypatch.setattr(guest, 'SECRETS', tmp_path / 'guest-secrets')
    return root


def settings(provider='openai'):
    return dict(provider=provider, url='https://models.example/v1', model='lab-model', ssl_verify=True)


def test_guest_caf_preserves_config_keys_secrets_and_snapshots(guest_root):
    path = guest_root / 'configs/cli.json'
    ev.write_json(path, dict(settings(), api_key='original-secret', network_policy={'disallow':['10.1.2.3']}, server_command='unchanged'))
    original = path.read_bytes()
    result = guest.dispatch(dict(action='read', role='participant', root=str(guest_root)))
    assert result['api_key_set'] and 'original-secret' not in json.dumps(result)
    updated = guest.dispatch(dict(action='save', role='participant', root=str(guest_root), revision=result['revision'], settings=dict(settings(), model='new-model')))
    assert Path(updated['backup']).read_bytes() == original
    data = ev.read_json(path)
    assert data['api_key'] == 'original-secret' and data['network_policy']['disallow'] == ['10.1.2.3'] and data['server_command'] == 'unchanged'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    use = guest.dispatch(dict(action='use', role='participant', root=str(guest_root), revision=updated['revision']))
    assert 'original-secret' not in json.dumps(use)
    snapshot = Path(use['environment_file'])
    assert snapshot.read_text() == 'CAF_EVAL_MODEL_API_KEY="original-secret"\n'
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match='changed'):
        guest.dispatch(dict(action='save', role='participant', root=str(guest_root), revision=result['revision'], settings=settings()))
    guest.dispatch(dict(action='save', role='participant', root=str(guest_root), revision=updated['revision'], settings=settings(), api_key=''))
    assert ev.read_json(path)['api_key'] == '' and 'original-secret' in snapshot.read_text()


def test_guest_scenarioforge_preserves_unrelated_env_and_key(guest_root):
    path = guest_root / '.scenarioforge.env'
    text = '# Keep this comment\nCORETG_AI_PROVIDER=litellm\nCORETG_AI_API_KEY="a secret"\nCORE_HOST=10.0.0.2\nOTHER="unchanged"\n'
    path.write_text(text)
    before = guest.dispatch(dict(action='read', role='scenarioforge', root=str(guest_root)))
    after = guest.dispatch(dict(action='save', role='scenarioforge', root=str(guest_root), revision=before['revision'], settings=settings('litellm')))
    assert 'a secret' not in json.dumps(after)
    saved = path.read_text()
    assert 'CORE_HOST=10.0.0.2' in saved and '# Keep this comment' in saved and 'CORETG_AI_API_KEY="a secret"' in saved
    assert guest.env_values(saved)['CORETG_AI_BASE_URL'] == 'https://models.example/v1'
    assert Path(after['backup']).read_text() == text


def test_guest_refuses_symlinks_and_embedded_credentials(guest_root, tmp_path):
    (guest_root / '.scenarioforge.env').symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError, match='symlink'):
        guest.dispatch(dict(action='read', role='scenarioforge', root=str(guest_root)))
    with pytest.raises(ValueError, match='credentials'):
        guest.validate('participant', dict(settings(), url='https://user:secret@models.example/v1'))


def fixture_manager(pve, lab, tmp_path, monkeypatch, guest_root):
    original_lease = ev.lease
    monkeypatch.setattr(ev, 'lease', lambda path: original_lease(tmp_path / ('lock-' + ev.digest(str(path))) if str(path).startswith('/var/lock/') else path))
    pve[0]['resources']['operator@pve'] = [vm(9403), vm(9402)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'model-runs', user.username)
    workspace.save_roles(dict(participant=9403, scenarioforge=9402, core=None), user)
    cfg, runtime, _, _ = load(lab[0])
    runtime['engine']['path'] = str(guest_root)
    cfg.setdefault('monitoring', {})['scenarioforge_path'] = str(guest_root)
    calls = []
    class Agent:
        def __init__(self, config, authorize=None): self.authorize = authorize
        def call(self, vmid, op, **data):
            self.authorize(['guest', 'exec', vmid])
            calls.append(op)
            return guest.dispatch(data)
    monkeypatch.setattr(ev, 'GuestAgent', Agent)
    return ModelConfigs(cfg, runtime, workspace.path.parent.parent), user, workspace, runtime, calls


def test_host_import_is_owned_vm_bound_and_changes_sample_runtime(pve, lab, tmp_path, monkeypatch, guest_root):
    manager, user, workspace, runtime, calls = fixture_manager(pve, lab, tmp_path, monkeypatch, guest_root)
    ev.write_json(guest_root / 'configs/cli.json', dict(settings(), api_key='guest-only-key'))
    result = manager.exchange(user, dict(role='participant', action='read'))
    with pytest.raises(AccessDenied):
        manager.exchange(user, dict(role='participant', action='use', token=result['token']))
    pve[0]['groups'] += ',caf-maintainers'
    pve[0]['resources']['bob@pve'] = [vm(9403)]
    bob = access(pve, 'bob@pve')
    Workspace(manager.root, bob.username).save_roles(dict(participant=9403, scenarioforge=None, core=None), bob)
    with pytest.raises(ModelConfigError, match='draft changed'):
        manager.exchange(bob, dict(role='participant', action='use', token=result['token']))
    selected = manager.exchange(user, dict(role='participant', action='use', token=result['token']))
    assert calls[-1] == 'write'  # Secret-bearing operations use the existing stdin path.
    current = apply_model(workspace, runtime, 9403)
    assert current['model']['provider'] == 'openai' and current['model']['name'] == 'lab-model'
    assert current['backend']['environment_file'] == selected['environment_file']
    assert apply_model(workspace, runtime, 9402)['model'] == runtime['model']
    for path in workspace.path.rglob('*.json'):
        assert 'guest-only-key' not in path.read_text()
    from cyber_agent_flow_orchestrator.samples import SampleManager
    samples = SampleManager(runtime, manager.root)
    try: assert samples.catalog(workspace.roles(), workspace)['provider'] == 'openai'
    finally: samples.close()
    workspace.save_roles(dict(participant=9402, scenarioforge=9403, core=None), user)
    with pytest.raises(ModelConfigError, match='draft changed'):
        manager.exchange(user, dict(role='participant', action='save', token=selected['token'], settings=settings()))


def test_model_config_http_requires_csrf_and_rejects_client_paths(pve, lab, tmp_path):
    dashboard = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        with secure_server(dashboard, tmp_path / 'web', auth=make_auth(pve)) as server:
            _, headers, _ = request(server, '/api/login', {'username':'operator@pve', 'password':PASSWORD})
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
            body = dict(role='participant', action='read')
            assert request(server, '/api/model-config', body)[0] == 401
            assert request(server, '/api/model-config', body, cookie=cookie)[0] == 403
            assert request(server, '/api/model-config', dict(body, root='/etc'), cookie=cookie, headers={'X-CSRF-Token':csrf})[0] == 400
    finally: dashboard.close()


def test_new_samples_freeze_selected_model_settings(pve, lab, tmp_path, monkeypatch):
    from test_samples import setup_sample
    manager, user, workspace, _, _, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    model = dict(provider='openai', url='https://models.example/v1', name='custom-model', ssl_verify=True, api_key_env='CAF_EVAL_MODEL_API_KEY')
    ev.write_json(workspace.path / 'model-9403.json', dict(model=model, engine_path=manager.runtime['engine']['path'], environment_file='/var/lib/caf-model-config/fixed-test.env'))
    try:
        result = manager.submit(user, 'smoke', 'e' * 32)
        manager.jobs[user.username].result(timeout=15)
        output = workspace.run_path(result['run_id'])
        saved = ev.read_json(output / 'workflow.json')
        assert saved['runtime']['model'] == model
        assert saved['runtime']['backend']['environment_file'] == '/var/lib/caf-model-config/fixed-test.env'
        inputs = next((output / 'evaluation').glob('trials/*/attempt-*/input.json'))
        assert ev.read_json(inputs)['model'] == model
    finally: manager.close()
