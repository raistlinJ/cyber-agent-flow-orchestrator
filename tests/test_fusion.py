import json
from pathlib import Path
import shlex
import yaml
import pytest
from cyber_agent_flow_orchestrator import fusion_setup
from cyber_agent_flow_orchestrator.auth import Auth, DesktopProvider, FusionProvider, create_user
from cyber_agent_flow_orchestrator.access import AccessDenied
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.tls import settings
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from https_fixture import PASSWORD, secure_server
from test_web import request
from test_user_access import Probe
from test_workflow import lab


@pytest.fixture
def fusion_lab(tmp_path):
    state_dir=tmp_path/'Fusion state';state_dir.mkdir()
    state={'INSTALLER_OWNER':'scenarioforge-vmware-fusion-v1','INSTALL_COMPLETE':'1','CYBER_AGENT_FLOW':'1',
           'LLM_PROVIDER_TYPE':'openai','LLM_PROVIDER_URL':'https://api.openai.com/v1','LLM_MODEL':'configured-model'}
    credentials={}
    for role in ['CORE','APP','PARTICIPANT']:
        vmx=tmp_path/(role+' VM.vmx');vmx.touch()
        state[role+'_VMX']=str(vmx);state[role+'_NAME']=role+' VM'
        credentials[role+'_VM_PASSWORD']='guest-test-password'
    for name,data in [('state.env',state),('credentials.env',credentials)]:
        path=state_dir/name;path.write_text('\n'.join(k+'='+shlex.quote(v) for k,v in data.items()));path.chmod(0o600)
    result=fusion_setup.prepare(state_dir,tmp_path/'local')
    create_user(result['users_file'],'operator',PASSWORD)
    return result


def test_fusion_import_loads_configs_and_preserves_provider(fusion_lab):
    cfg,runtime,_,_=load(fusion_lab['workflow'])
    web=settings(fusion_lab['web_config'])
    assert runtime['backend']['type']=='fusion'
    assert runtime['model']['provider']=='openai'
    assert runtime['model']['name']=='configured-model'
    assert web['auth']['provider']=='fusion'
    assert web['certificate']==str(Path(fusion_lab['workflow']).parent.parent/'certs/cert.pem')
    assert web['auth']['inventory_file']==runtime['backend']['inventory_file']
    assert runtime['execution']['target_lock'].startswith(str(Path.home()))
    assert 'guest-test-password' not in Path(fusion_lab['workflow']).read_text()
    assert 'guest-test-password' not in Path(fusion_lab['web_config']).read_text()


def test_fusion_http_login_inventory_and_session_revocation(fusion_lab,tmp_path):
    web=settings(fusion_lab['web_config'])
    provider=FusionProvider(fusion_lab['users_file'],web['auth']['inventory_file'])
    auth=Auth(provider=provider)
    calls=[]
    dashboard=UserDashboard(fusion_lab['workflow'],tmp_path/'runs',probe_factory=lambda backend,access:Probe(backend,access,calls),updates=False)
    try:
        with secure_server(dashboard,tmp_path/'https',auth=auth) as server:
            code,headers,body=request(server,'/api/login',{'username':'operator','password':PASSWORD})
            assert code==200,body
            cookie=headers['Set-Cookie'].split(';')[0]
            code,_,body=request(server,'/api/session',cookie=cookie)
            session=json.loads(body);assert session['provider']=='fusion'
            csrf={'X-CSRF-Token':session['csrf']}
            code,_,body=request(server,'/api/status?refresh=0',cookie=cookie)
            assert code==200,body
            status=json.loads(body)
            assert {vm['vmid'] for vm in status['available_vms']}=={9401,9402,9403}
            assert status['model_config_writable'] is True
            assert 'guest-test-password' not in body.decode()
            access=auth.access(cookie.split('=',1)[1])
            access.require_vm(9403)
            with pytest.raises(AccessDenied):access.require_vm(9999)
            code,_,body=request(server,'/api/logout',{},cookie=cookie,headers=csrf)
            assert code==200,body
            with pytest.raises(AccessDenied):access.require_vm(9403)
    finally:dashboard.close()


def test_fusion_runs_full_workflow_with_guest_transport(lab, tmp_path):
    from cyber_agent_flow_orchestrator import workflow
    from cyber_agent_flow_eval import integration as ev
    config,output,agent,_=lab
    cfg=yaml.safe_load(config.read_text())
    runtime_path=config.parent/cfg['runtime']
    runtime=yaml.safe_load(runtime_path.read_text())
    rows={}
    for vmid in [9401,9402,9403]:
        vmx=tmp_path/(str(vmid)+'.vmx');vmx.touch()
        rows[str(vmid)]=dict(vmx=str(vmx),username='participant',password='test-guest-password')
    inventory=tmp_path/'fusion.json';inventory.write_text(json.dumps(dict(version=1,vms=rows)));inventory.chmod(0o600)
    runtime['backend'].update(type='fusion',inventory_file=str(inventory))
    runtime_path.write_text(yaml.safe_dump(runtime))
    workflow.run(config,output,agent=agent,progress=None)
    assert ev.read_json(output/'workflow.json')['status']=='completed'
    assert any(op=='preflight' for vmid,op,data in agent.calls)


def test_default_launcher_uses_imported_fusion_profile(fusion_lab, tmp_path, monkeypatch):
    from cyber_agent_flow_orchestrator.__main__ import main
    from cyber_agent_flow_orchestrator import web
    Path(fusion_lab['workflow']).parent.rename(tmp_path/'fusion-local')
    # Test launch selection; config inventory paths remain in the original
    # generated files and are not needed by the fake server.
    monkeypatch.chdir(tmp_path)
    calls=[]
    monkeypatch.setattr(web,'serve',lambda *args,**kwargs:calls.append((args,kwargs)) or 0)
    assert main([])==0
    args,kwargs=calls[-1]
    assert args[0]==str(tmp_path/'fusion-local/workflow.yaml')
    assert args[1]==str(tmp_path/'fusion-local/runs')
    assert kwargs['web_config']==str(tmp_path/'fusion-local/web.yaml')
    assert not (tmp_path/'web.yaml').exists()


def test_desktop_session_without_password(fusion_lab, tmp_path):
    import getpass
    web=settings(fusion_lab['web_config'])
    auth=Auth(provider=DesktopProvider(web['auth']['inventory_file']))
    dashboard=UserDashboard(fusion_lab['workflow'],tmp_path/'runs',probe_factory=lambda backend,access:Probe(backend,access,[]),updates=False)
    try:
        with secure_server(dashboard,tmp_path/'https',auth=auth) as server:
            code,headers,body=request(server,'/api/session')
            assert code==200,body
            session=json.loads(body)
            assert session['provider']=='desktop'
            assert session['username']==getpass.getuser()
            assert 'Secure' in headers['Set-Cookie']
            cookie=headers['Set-Cookie'].split(';')[0]
            code,_,body=request(server,'/api/status?refresh=0',cookie=cookie)
            assert code==200,body
            assert {vm['vmid'] for vm in json.loads(body)['available_vms']}=={9401,9402,9403}
            code,_,_=request(server,'/api/logout',{},cookie=cookie)
            assert code==403
    finally:dashboard.close()


def test_local_overrides_remote_listen(fusion_lab):
    path=Path(fusion_lab['web_config'])
    data=yaml.safe_load(path.read_text());data['listen']='0.0.0.0'
    path.write_text(yaml.safe_dump(data))
    config=settings(path,local=True)
    assert config['listen']=='127.0.0.1'
    assert config['auth']['provider']=='desktop'


def test_legacy_desktop_profile_requires_local_flag(fusion_lab):
    path=Path(fusion_lab['web_config'])
    data=yaml.safe_load(path.read_text())
    data['auth']['provider']='desktop'
    data.pop('users_file')
    path.write_text(yaml.safe_dump(data))
    before=path.read_bytes()
    assert settings(path)['auth']['provider']=='fusion'
    assert settings(path,local=True)['auth']['provider']=='desktop'
    assert path.read_bytes()==before
