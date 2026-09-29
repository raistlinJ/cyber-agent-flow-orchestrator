import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import runpy
import shutil
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cyber_agent_flow_orchestrator import scenario_guest, scenarios
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
            bad = io.BytesIO()
            with zipfile.ZipFile(bad, 'w') as target:
                for info in source.infolist():
                    target.writestr(info, source.read(info))
                target.writestr('../escape', b'bad')
            with pytest.raises(ValueError, match='unsafe'):
                validate(bad.getvalue())


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
            if op == 'snapshot':
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
            pve[0]['resources']['operator@pve'] = []
            assert upload(cookie=cookie,csrf=csrf)[0] == 403
            assert len(calls) == 4
    finally:
        dash.close()
