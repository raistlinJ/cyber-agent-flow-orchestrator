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
    monkeypatch.setattr(guest, 'network_scope', lambda url,config:dict(provider_host='models.example',excluded_targets=['192.0.2.8','192.0.2.1'],warnings=[]))
    monkeypatch.setattr(guest, 'check_endpoint', lambda values,route:dict(status='unreachable',message='Endpoint unavailable'))
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
    use = updated  # Save also prepares the immutable credential snapshot.
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
        manager.exchange(user, dict(role='participant', action='save', token=result['token'], settings=settings()))
    pve[0]['groups'] += ',caf-maintainers'
    pve[0]['resources']['bob@pve'] = [vm(9403)]
    bob = access(pve, 'bob@pve')
    Workspace(manager.root, bob.username).save_roles(dict(participant=9403, scenarioforge=None, core=None), bob)
    with pytest.raises(ModelConfigError, match='draft changed'):
        manager.exchange(bob, dict(role='participant', action='save', token=result['token'], settings=settings()))
    selected = manager.exchange(user, dict(role='participant', action='save', token=result['token'], settings=settings()))
    assert calls[-1] == 'write'  # Secret-bearing operations use the existing stdin path.
    current = apply_model(workspace, runtime, 9403)
    assert current['model']['provider'] == 'openai' and current['model']['name'] == 'lab-model'
    assert current['backend']['environment_file'] == selected['environment_file']
    assert apply_model(workspace, runtime, 9402)['model'] == runtime['model']
    for path in workspace.path.rglob('*.json'):
        assert 'guest-only-key' not in path.read_text()
    from cyber_agent_flow_orchestrator.samples import SampleManager
    samples = SampleManager(runtime, manager.root)
    try:
        catalog = samples.catalog(workspace.roles(), workspace)
        assert catalog['provider'] == 'openai'
        assert catalog['model_settings'] == settings()
        assert 'guest-only-key' not in json.dumps(catalog)
        assert 'api_key' not in catalog['model_settings']
    finally: samples.close()
    workspace.save_roles(dict(participant=9402, scenarioforge=9403, core=None), user)
    with pytest.raises(ModelConfigError, match='draft changed'):
        manager.exchange(user, dict(role='participant', action='save', token=selected['token'], settings=settings()))


def test_repeated_reads_preserve_draft_but_changed_guest_invalidates_it(pve, lab, tmp_path, monkeypatch, guest_root):
    manager, user, workspace, runtime, calls = fixture_manager(pve, lab, tmp_path, monkeypatch, guest_root)
    pve[0]['groups'] += ',caf-maintainers'
    path = guest_root / 'configs/cli.json'
    ev.write_json(path, settings())
    first = manager.exchange(user, dict(role='participant', action='read'))
    second = manager.exchange(user, dict(role='participant', action='read'))
    assert first['token'] == second['token']
    manager.exchange(user, dict(role='participant', action='stage', token=first['token'], settings=settings()))
    ev.write_json(path, dict(settings(), model='externally-updated'))
    third = manager.exchange(user, dict(role='participant', action='read'))
    assert third['token'] != first['token']
    with pytest.raises(ModelConfigError, match='draft changed'):
        manager.exchange(user, dict(role='participant', action='stage', token=first['token'], settings=settings()))


def test_model_config_http_requires_csrf_and_rejects_client_paths(pve, lab, tmp_path):
    dashboard = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        with secure_server(dashboard, tmp_path / 'web', auth=make_auth(pve)) as server:
            _, headers, _ = request(server, '/api/login', {'username':'operator@pve', 'password':PASSWORD})
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
            body = dict(role='participant', action='read')
            assert request(server, '/api/model-network-scope', {})[0] == 401
            assert request(server, '/api/model-network-scope', {}, cookie=cookie)[0] == 403
            assert request(server, '/api/model-network-scope', {'url':'http://untrusted'}, cookie=cookie, headers={'X-CSRF-Token':csrf})[0] == 400
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


def test_saved_experiment_rerun_preserves_model_credentials_and_vm(pve, lab, tmp_path, monkeypatch):
    from test_samples import setup_sample
    manager, user, workspace, _, _, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    model = dict(provider='openai', url='https://models.example/v1', name='saved-model', ssl_verify=True, api_key_env='CAF_EVAL_MODEL_API_KEY')
    preference = workspace.path / 'model-9403.json'
    ev.write_json(preference, dict(model=model, engine_path=manager.runtime['engine']['path'], environment_file='/var/lib/caf-model-config/original.env'))
    try:
        draft = manager.create(user, 'smoke', 'a' * 32)
        original = ev.read_json(workspace.run_path(draft['run_id']) / 'workflow.json')['runtime']
        # Changing defaults after Create must not affect either the first run or reruns.
        ev.write_json(preference, dict(model=dict(model, name='new-default'), engine_path=manager.runtime['engine']['path'], environment_file='/var/lib/caf-model-config/new.env'))
        first = manager.run_saved(user, draft['run_id'], 'b' * 32)
        manager.jobs[user.username].result(timeout=15)
        first_path = workspace.run_path(first['run_id']) / 'workflow.json'
        assert ev.read_json(first_path)['runtime'] == original
        pve[0]['resources']['operator@pve'] = [vm(9403), vm(9402)]
        workspace.save_roles(dict(participant=9402, scenarioforge=None, core=None), user)
        again = manager.run_saved(user, first['run_id'], 'c' * 32)
        manager.jobs[user.username].result(timeout=15)
        assert again['run_id'] != first['run_id']
        saved = ev.read_json(workspace.run_path(again['run_id']) / 'workflow.json')
        assert saved['runtime'] == original
        assert saved['runtime']['model'] == model
        from cyber_agent_flow_orchestrator import service
        assert service.status(workspace.run_path(again['run_id']))['saved_settings'] == dict(participant_vmid=9403, provider='openai', model='saved-model')
        assert saved['runtime']['backend']['participant_vmid'] == 9403
        assert saved['runtime']['backend']['environment_file'] == '/var/lib/caf-model-config/original.env'
        pve[0]['resources']['operator@pve'] = [vm(9402)]
        with pytest.raises(AccessDenied):
            manager.run_saved(user, first['run_id'], 'd' * 32)
    finally: manager.close()


def test_local_model_stage_is_private_and_does_not_activate_or_transfer(pve, lab, tmp_path, monkeypatch, guest_root):
    manager, user, workspace, runtime, calls = fixture_manager(pve, lab, tmp_path, monkeypatch, guest_root)
    ev.write_json(guest_root / 'configs/cli.json', dict(settings(), api_key='guest-secret'))
    loaded = manager.exchange(user, dict(role='participant', action='read'))
    request_data = dict(role='participant', action='stage', token=loaded['token'], settings=dict(settings(), model='draft-model'))
    with pytest.raises(AccessDenied): manager.exchange(user, request_data)
    pve[0]['groups'] += ',caf-maintainers'
    with pytest.raises(ModelConfigError): manager.exchange(user, dict(request_data, api_key='must-not-store'))
    before = len(calls)
    result = manager.exchange(user, request_data)
    assert result['local_saved'] and len(calls) == before
    pending = workspace.path / 'model-pending-9403.json'
    assert ev.read_json(pending)['settings']['model'] == 'draft-model'
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert 'guest-secret' not in pending.read_text()
    assert apply_model(workspace, runtime, 9403) == runtime
    assert ev.read_json(guest_root / 'configs/cli.json')['model'] == 'lab-model'
    with pytest.raises(ModelConfigError, match='draft changed'):
        manager.exchange(user, dict(request_data, token='0' * 32))


def test_model_save_reuses_route_helper_and_preserves_managed_policy(guest_root, monkeypatch, tmp_path):
    import subprocess
    path=guest_root/'configs/cli.json'
    ev.write_json(path,dict(settings(),api_key='private-key',network_policy={'allow':['10.0.0.0/24'],'disallow':['10.0.0.10']}))
    helper=tmp_path/'update-llm-destination';helper.touch()
    monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    calls=[]
    def update(command,**kwargs):
        calls.append(command)
        config=ev.read_json(path)
        config['network_policy']['disallow']+=['192.168.20.2','203.0.113.21']
        config['llm_route_destination']='203.0.113.21'
        ev.write_json(path,config)
        return subprocess.CompletedProcess(command,0,json.dumps(dict(status='updated',destination='203.0.113.21',interface='eth2',gateway='192.168.20.2')),'')
    monkeypatch.setattr(guest.subprocess,'run',update)
    loaded=guest.dispatch(dict(role='participant',action='read',root=str(guest_root)))
    result=guest.dispatch(dict(role='participant',action='save',root=str(guest_root),revision=loaded['revision'],settings=dict(settings(),model='updated-model')))
    assert calls==[[str(helper),'--url','https://models.example/v1','--cli-config',str(path),'--json']]
    assert 'private-key' not in json.dumps(calls) + json.dumps(result)
    saved=ev.read_json(path)
    assert saved['llm_route_destination']=='203.0.113.21'
    assert saved['network_policy']['disallow']==['10.0.0.10','192.168.20.2','203.0.113.21']
    assert saved['model']=='updated-model'
    assert result['connectivity']['status']=='unreachable'
    assert result['routing']['interface']=='eth2'


def test_failed_route_update_does_not_save_model_settings(guest_root, monkeypatch, tmp_path):
    import subprocess
    path=guest_root/'configs/cli.json';ev.write_json(path,settings())
    helper=tmp_path/'update-llm-destination';helper.touch();monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    before=path.read_bytes()
    loaded=guest.dispatch(dict(role='participant',action='read',root=str(guest_root)))
    monkeypatch.setattr(guest.subprocess,'run',lambda command,**kwargs:subprocess.CompletedProcess(command,1,'','LLM update failed: route validation failed'))
    with pytest.raises(ValueError,match='route update failed'):
        guest.dispatch(dict(role='participant',action='save',root=str(guest_root),revision=loaded['revision'],settings=dict(settings(),model='new-model')))
    assert path.read_bytes()==before


def test_endpoint_check_uses_selected_destination_and_url_port(monkeypatch):
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    calls=[]
    monkeypatch.setattr(guest.socket,'create_connection',lambda target,timeout:calls.append((target,timeout)) or Connection())
    result=guest.check_endpoint(dict(settings(),url='https://model.example:8443/v1'),{'destination':'203.0.113.20'})
    assert result['status']=='reachable'
    assert calls==[(('203.0.113.20',8443),5)]


def test_provider_scope_uses_guest_dns_and_specific_gateway(monkeypatch):
    import socket
    from types import SimpleNamespace
    monkeypatch.setattr(guest.socket, 'getaddrinfo', lambda *args,**kwargs:[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.8', 11434))])
    calls=[]
    def route(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout='[{"gateway":"192.0.2.1","dev":"ens20"}]')
    monkeypatch.setattr(guest.subprocess, 'run', route)
    result=guest.network_scope('http://models.example:11434', dict(url='http://previous.example',llm_route_destination='192.0.2.99'))
    assert result['excluded_targets']==['192.0.2.1','192.0.2.8']
    assert calls==[['ip','-j','-4','route','get','192.0.2.8']]
    assert not result['warnings']


def test_provider_scope_fallback_never_uses_old_endpoint(monkeypatch):
    import socket
    def no_dns(*args,**kwargs): raise socket.gaierror('offline')
    def no_route(*args,**kwargs): raise OSError('offline')
    monkeypatch.setattr(guest.socket, 'getaddrinfo', no_dns)
    monkeypatch.setattr(guest.subprocess, 'run', no_route)
    config=dict(url='http://models.example:11434',llm_route_destination='192.0.2.8',llm_route_gateway='192.0.2.1')
    assert guest.network_scope(config['url'],config)['excluded_targets']==['192.0.2.1','192.0.2.8']
    result=guest.network_scope('http://new.example:11434',config)
    assert result['excluded_targets']==[] and result['warnings']


def test_scope_request_uses_applied_model_without_replacing_draft(pve,lab,tmp_path,monkeypatch,guest_root):
    manager,user,workspace,runtime,calls=fixture_manager(pve,lab,tmp_path,monkeypatch,guest_root)
    ev.write_json(guest_root/'configs/cli.json',settings())
    manager.exchange(user,dict(role='participant',action='read'))
    ev.write_json(workspace.path/'model-9403.json',dict(model=dict(runtime['model'],url='http://applied.example:11434'),engine_path=runtime['engine']['path']))
    urls=[]
    def scope(url,config):
        urls.append(url)
        return dict(excluded_targets=['192.0.2.8','192.0.2.1'])
    monkeypatch.setattr(guest,'network_scope',scope)
    draft=(workspace.path/'model-draft-participant.json').read_bytes()
    result=manager.network_scope(user)
    assert result['vmid']==9403 and result['excluded_targets']==['192.0.2.8','192.0.2.1']
    assert urls==['http://applied.example:11434']
    assert (workspace.path/'model-draft-participant.json').read_bytes()==draft


def test_legacy_route_helper_is_backed_up_and_retried_on_authorized_save(guest_root, monkeypatch, tmp_path):
    import subprocess
    helper=tmp_path/'update-llm-destination'
    original=b'#!/usr/bin/python3\n# legacy utility\n'
    helper.write_bytes(original);helper.chmod(0o755)
    monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    source=Path(guest.__file__).with_name('guest_llm_route.py').read_text()
    path=guest_root/'configs/cli.json';path.parent.mkdir()
    ev.write_json(path,dict(settings(),model='old-model'))
    loaded=guest.dispatch(dict(role='participant',action='read',root=str(guest_root)))
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if len(calls)==1:
            return subprocess.CompletedProcess(argv,2,'','unrecognized arguments: --cli-config --json')
        assert helper.read_text()==source
        assert stat.S_IMODE(helper.stat().st_mode)==0o755
        return subprocess.CompletedProcess(argv,0,json.dumps(dict(status='updated',destination='192.0.2.8',interface='eth2')),'')
    monkeypatch.setattr(guest.subprocess,'run',run)
    result=guest.dispatch(dict(role='participant',action='save',root=str(guest_root),revision=loaded['revision'],settings=dict(settings(),model='real-model'),route_helper_source=source))
    assert len(calls)==2 and calls[0]==calls[1]
    assert result['routing']['helper_updated']
    assert Path(result['routing']['helper_backup']).read_bytes()==original
    assert stat.S_IMODE(Path(result['routing']['helper_backup']).stat().st_mode)==0o600
    assert ev.read_json(path)['model']=='real-model'


@pytest.mark.parametrize('unsafe',['symlink','writable'])
def test_legacy_route_helper_repair_rejects_unsafe_files(tmp_path, monkeypatch, unsafe):
    helper=tmp_path/'helper'
    if unsafe=='symlink':
        target=tmp_path/'target';target.write_text('original');helper.symlink_to(target)
    else:
        helper.write_text('original');helper.chmod(0o777)
    monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    with pytest.raises(ValueError,match='Unsafe participant routing helper'):
        guest.upgrade_route_helper('print("replacement")')
    assert helper.read_text()=='original'
    assert not list(tmp_path.glob('*.bak'))


def test_failed_retry_does_not_save_model_settings(guest_root, monkeypatch, tmp_path):
    import subprocess
    helper=tmp_path/'helper';helper.write_text('# legacy');helper.chmod(0o755)
    monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    path=guest_root/'configs/cli.json';path.parent.mkdir()
    ev.write_json(path,dict(settings(),model='old-model'))
    original=path.read_bytes()
    loaded=guest.dispatch(dict(role='participant',action='read',root=str(guest_root)))
    responses=iter([subprocess.CompletedProcess([],2,'','unrecognized arguments: --json'),subprocess.CompletedProcess([],1,'','No dedicated LLM interface')])
    monkeypatch.setattr(guest.subprocess,'run',lambda *a,**kw:next(responses))
    with pytest.raises(ValueError,match='helper updated.*No dedicated LLM interface'):
        guest.dispatch(dict(role='participant',action='save',root=str(guest_root),revision=loaded['revision'],settings=dict(settings(),model='new-model'),route_helper_source='print("new helper")'))
    assert path.read_bytes()==original


def test_route_helper_source_cannot_be_supplied_by_web_client(pve, lab, tmp_path, monkeypatch, guest_root):
    manager,user,workspace,runtime,calls=fixture_manager(pve,lab,tmp_path,monkeypatch,guest_root)
    with pytest.raises(ModelConfigError,match='Invalid model configuration request'):
        manager.exchange(user,dict(role='participant',action='read',route_helper_source='print("client code")'))
    assert not calls


def test_pull_never_repairs_installed_legacy_helper(guest_root, monkeypatch, tmp_path):
    helper=tmp_path/'helper';helper.write_text('# legacy');helper.chmod(0o755)
    monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    monkeypatch.setattr(guest.subprocess,'run',lambda *a,**k:pytest.fail('Pull attempted to execute routing helper'))
    guest.dispatch(dict(role='participant',action='read',root=str(guest_root)))
    assert helper.read_text()=='# legacy'
    assert not list(tmp_path.glob('*.bak'))


def test_scenarioforge_model_save_preserves_nonparticipant_exchange(pve, lab, tmp_path, monkeypatch, guest_root):
    manager,user,workspace,runtime,calls=fixture_manager(pve,lab,tmp_path,monkeypatch,guest_root)
    pve[0]['groups']+=',caf-maintainers'
    loaded=manager.exchange(user,dict(role='scenarioforge',action='read'))
    result=manager.exchange(user,dict(role='scenarioforge',action='save',token=loaded['token'],settings=settings('litellm')))
    assert result['settings']==settings('litellm')
    assert guest.env_values((guest_root/'.scenarioforge.env').read_text())['CORETG_AI_MODEL']=='lab-model'


def test_route_service_failure_exposes_cause_without_saving_model(guest_root, monkeypatch, tmp_path):
    import subprocess
    helper=tmp_path/'helper';helper.write_text('# installed');helper.chmod(0o755)
    monkeypatch.setattr(guest,'ROUTE_HELPER',helper)
    path=guest_root/'configs/cli.json';path.parent.mkdir()
    ev.write_json(path,dict(settings(),model='old-model'))
    original=path.read_bytes()
    loaded=guest.dispatch(dict(role='participant',action='read',root=str(guest_root)))
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[0]=='journalctl':
            return subprocess.CompletedProcess(argv,0,'FileNotFoundError: /run/systemd/netif/leases/4\napi_key=private-value','')
        return subprocess.CompletedProcess(argv,1,'',"systemctl start scenarioforge-llm-route.service returned non-zero exit status 1")
    monkeypatch.setattr(guest.subprocess,'run',run)
    with pytest.raises(ValueError) as error:
        guest.dispatch(dict(role='participant',action='save',root=str(guest_root),revision=loaded['revision'],settings=dict(settings(),model='new-model')))
    assert 'FileNotFoundError: /run/systemd/netif/leases/4' in str(error.value)
    assert 'private-value' not in str(error.value)
    assert path.read_bytes()==original
    assert len(calls)==2


def test_route_service_diagnostics_timeout_does_not_hide_original_failure(monkeypatch):
    import subprocess
    def run(*args,**kwargs):
        raise subprocess.TimeoutExpired('journalctl',5)
    monkeypatch.setattr(guest.subprocess,'run',run)
    assert guest.route_service_failure_details()==''
