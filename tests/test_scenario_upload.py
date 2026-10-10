import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import runpy
import shutil
import zipfile
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cyber_agent_flow_orchestrator import scenario_guest, scenarios
from cyber_agent_flow_orchestrator.evaluation_tasks import validate_tasks
from cyber_agent_flow_orchestrator.scenario_upload import validate, MAX_UPLOAD
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_eval import integration as ev
from test_pve_auth import pve, make_auth
from test_user_access import vm, Probe
from test_workflow import lab
from test_web import request
from https_fixture import secure_server, PASSWORD

ROOT = Path(__file__).resolve().parents[1]
SF = ROOT.parent / 'scenarioforge/webapp/reproduction_bundle.py'
XML = b'<Scenarios><Scenario name="Uploaded lab"><ScenarioEditor><FlagSequencing><FlowState>{"chain":[{"id":"1"}]}</FlowState></FlagSequencing></ScenarioEditor></Scenario></Scenarios>'


@pytest.mark.parametrize('content', [b'', b'<broken', b'<other/>', b'<!DOCTYPE Scenarios><Scenarios/>', b'x'*(MAX_UPLOAD+1)])
def test_invalid_uploads(content):
    with pytest.raises(ValueError):
        validate(content)


def test_bundle_validation_and_demo_import(tmp_path):
    if not SF.is_file():
        pytest.skip('Sibling ScenarioForge importer unavailable')
    importer = runpy.run_path(str(SF))['import_scenario_file']
    for key in ('smoke','tools-vs-helper'):
        path = ROOT / 'cyber_agent_flow_orchestrator/static' / ('demo-'+key+'.zip')
        assert validate(path.read_bytes()) == 'reproduction-bundle'
        result = importer(str(path), str(tmp_path / key))
        rows = scenario_guest.inspect(Path(result.xml_path))
        assert len(rows) == 1 and not rows[0]['resolved_chain']
        assert result.bundled_artifact_sources == 1
        assert list((tmp_path / key).rglob('docker-compose.yml'))
        profile = json.loads(next((tmp_path / key).rglob('demo-profile.json')).read_text())
        assert profile['sample_id'] == key
        with zipfile.ZipFile(path) as source:
            required={'README.md','demo-profile.json','evaluation-task-template.json','participant-guide.md','facilitator-guide.md'}
            assert required <= set(source.namelist())
            template=json.loads(source.read('evaluation-task-template.json'))
            assert template==profile['task_template']
            assert template['required_checks']==['containers','services','ports']
            assert len(template['progressive_hints'])==3
            flow=json.loads(ET.fromstring(source.read('scenario.xml')).find('.//FlowState').text)
            assert flow['evaluation_task_template']==template
            bad = io.BytesIO()
            with zipfile.ZipFile(bad, 'w') as target:
                for info in source.infolist():
                    target.writestr(info, source.read(info))
                target.writestr('../escape', b'bad')
            with pytest.raises(ValueError, match='unsafe'):
                validate(bad.getvalue())


def test_shareable_scenarioforge_bundles_include_editable_tasks_and_guides():
    bundles=sorted((ROOT/'ScenarioForge-Bundles').glob('*.zip'))
    assert len(bundles)==5
    for path in bundles:
        assert validate(path.read_bytes())=='reproduction-bundle'
        with zipfile.ZipFile(path) as archive:
            names=set(archive.namelist())
            assert {'scenario.xml','evaluation-tasks.json','participant-guide.md','facilitator-guide.md'} <= names
            tasks=validate_tasks(json.loads(archive.read('evaluation-tasks.json')))
            state=json.loads(ET.fromstring(archive.read('scenario.xml')).find('.//FlowState').text)
            assert state['evaluation_tasks']==tasks
            assert all(task['prompt'] and task['verifier'] and task['required_checks'] and task['progressive_hints'] for task in tasks)


def test_guest_import_plain_xml_and_bundle(tmp_path):
    if not SF.is_file():
        pytest.skip('Sibling ScenarioForge importer unavailable')
    repo = tmp_path / 'scenarioforge'
    (repo / 'webapp').mkdir(parents=True)
    shutil.copyfile(SF, repo / 'webapp/reproduction_bundle.py')
    for index, content in enumerate([XML, (ROOT/'cyber_agent_flow_orchestrator/static/demo-tools-vs-helper.zip').read_bytes()]):
        token = str(index)*32
        result = scenario_guest.dispatch(dict(op='upload-start', repo=str(repo), token=token))
        Path(result['path']).write_bytes(content)
        imported = scenario_guest.dispatch(dict(op='upload-import', repo=str(repo), token=token,
            sha256=hashlib.sha256(content).hexdigest(), user=pwd.getpwuid(os.getuid()).pw_name))
        assert len(imported['items']) == 1
        assert Path(imported['path']).suffix == '.xml'
        assert imported['kind'] == ('xml' if index == 0 else 'reproduction-bundle')
        assert scenario_guest.catalogue({'roots':[str(repo/'uploads')]})['items']


def test_binary_upload_http_auth_scope_validation_and_selection(pve, lab, tmp_path, monkeypatch):
    pve[0]['resources']['operator@pve'] = [vm(9402),vm(9403)]
    dash = UserDashboard(lab[0],tmp_path/'runs',2,lambda b,a:Probe(b,a,[]))
    calls=[]
    class Remote:
        def call(self, vmid, op, **data):
            calls.append((vmid,op))
            if op == 'upload-start':
                return {'path':'/safe/source'}
            if op == 'upload-import':
                return dict(items=[dict(id='a'*64,path='/safe/scenario.xml',scenario='Uploaded lab',
                    sha256=hashlib.sha256(XML).hexdigest(),resolved_chain=True,chain_length=1,bytes=len(XML))],
                    path='/safe/scenario.xml',kind='xml',fidelity='definition')
            if op == 'tasks':
                text=json.dumps({'tasks':None,'suggested_tasks':[dict(id='inventory', family='inventory',
                    prompt='Read the service title.', verifier=dict(type='contains_all', expected=['Demo']),
                    required_checks=['services'])],'context':{}})
                return {'chunk':text,'total':len(text),'sha256':hashlib.sha256(text.encode()).hexdigest()}
            if op == 'snapshot':
                assert data['tasks'][0]['id'] == 'inventory'
                return dict(id='a'*64,path='/safe/scenario.xml',scenario='Uploaded lab',
                            sha256=hashlib.sha256(XML).hexdigest(),resolved_chain=True,chain_length=1,
                            snapshot_path='/safe/snapshot.xml')
            raise AssertionError(op)
        def put(self, vmid, path, content):
            calls.append((vmid,'put'))
            assert content == XML and path == '/safe/source'
    monkeypatch.setattr(scenarios,'guest',lambda backend:Remote())
    monkeypatch.setattr(ev,'GuestAgent',lambda backend:Remote())
    try:
        with secure_server(dash,tmp_path/'web',auth=make_auth(pve)) as server:
            def upload(content=XML,cookie='',csrf=''):
                req=Request(server['origin']+'/api/scenarios/upload',data=content,headers={
                    'Content-Type':'application/octet-stream','Origin':server['origin'],
                    'Cookie':cookie,'X-CSRF-Token':csrf})
                try:
                    response=urlopen(req,context=server['ssl'],timeout=10)
                except HTTPError as exc:
                    return exc.code,exc.read()
                return response.status,response.read()
            assert upload()[0] == 401
            _,headers,_=request(server,'/api/login',{'username':'operator@pve','password':PASSWORD})
            cookie=headers['Set-Cookie'].split(';',1)[0]
            csrf=json.loads(request(server,'/api/session',cookie=cookie)[2])['csrf']
            assert upload(cookie=cookie)[0] == 403
            assert upload(cookie=cookie,csrf=csrf)[0] == 400  # no selected VM
            request(server,'/api/roles',dict(scenarioforge=9402,participant=9403,core=None),
                    cookie=cookie,headers={'X-CSRF-Token':csrf})
            assert upload(b'<broken',cookie,csrf)[0] == 400
            assert not calls
            status,body=upload(cookie=cookie,csrf=csrf)
            assert status == 200, body
            assert json.loads(body)['items'][0]['scenario'] == 'Uploaded lab'
            assert calls == [(9402,'upload-start'),(9402,'put'),(9402,'upload-import')]
            status,_,body=request(server,'/api/experiments/create',
                dict(selection_id='a'*64,request_id='b'*32,allowed_targets='10.77.0.0/24',disallowed_targets=''),
                cookie=cookie,headers={'X-CSRF-Token':csrf})
            assert status == 202, body
            from cyber_agent_flow_orchestrator.workspaces import Workspace
            output=Workspace(tmp_path/'runs','operator@pve').run_path(json.loads(body)['run_id'])
            assert (output/'inputs/uploaded-source.xml').read_bytes() == XML
            assert request(server,'/demo-smoke.zip')[0] == 401
            status,headers,body=request(server,'/demo-smoke.zip',cookie=cookie)
            assert status == 200 and 'attachment' in headers['Content-Disposition']
            assert validate(body) == 'reproduction-bundle'
            def failed_save(*args, **kwargs):
                raise ValueError("execution: unknown fields ['provide_progressive_hints'], missing fields []")
            with monkeypatch.context() as errors:
                errors.setattr(dash, 'experiment', failed_save)
                status,_,body=request(server,'/api/experiments/create',
                    dict(sample_id='smoke',request_id='c'*32),
                    cookie=cookie,headers={'X-CSRF-Token':csrf})
                assert status == 400, body
                message=json.loads(body)['error']
                assert 'Cannot create experiment' in message and 'provide_progressive_hints' in message
                assert 'Authentication service unavailable' not in message
            for endpoint, target, attribute, failure, stage in [
                ('/api/experiments/create', dash, 'experiment', KeyError('snapshot_path'), 'create experiment'),
                ('/api/model-config', dash.model_configs, 'exchange', OSError('settings file is not writable'), 'model settings'),
            ]:
                def fail(*args, **kwargs):
                    raise failure
                with monkeypatch.context() as errors:
                    errors.setattr(target, attribute, fail)
                    status,_,body=request(server,endpoint,dict(sample_id='smoke',request_id='c'*32),
                        cookie=cookie,headers={'X-CSRF-Token':csrf})
                    response=json.loads(body)
                    assert status in (500,503), body
                    assert response['stage'] == stage and len(response['error_id']) == 12
                    assert str(failure) in response['error']
                    assert 'Authentication service unavailable' not in response['error']
            def auth_unavailable(*args, **kwargs):
                raise ValueError('password=must-not-leak')
            with monkeypatch.context() as errors:
                errors.setattr(server['auth'].provider, 'validate', auth_unavailable)
                status,_,body=request(server,'/api/experiments/create',
                    dict(sample_id='smoke',request_id='c'*32),
                    cookie=cookie,headers={'X-CSRF-Token':csrf})
                response=json.loads(body)
                assert status == 503 and response['stage'] == 'authentication'
                assert 'Proxmox authentication/session validation unavailable' in response['error']
                assert 'must-not-leak' not in response['error']
            pve[0]['resources']['operator@pve'] = []
            assert upload(cookie=cookie,csrf=csrf)[0] == 403
            assert len(calls) == 5
    finally:
        dash.close()
