import base64
import gzip
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from cyber_agent_flow_orchestrator import scenario_guest as guest
from test_evaluation_tasks import source


def test_reference_export_is_readonly_cached_and_checksum_bound(tmp_path, monkeypatch):
    path=source(tmp_path)
    original=path.read_bytes()
    row=guest.inspect(path)[0]
    calls=[]
    def export(command, **kwargs):
        calls.append(command)
        assert command[3]=='guides' and '--phase' not in command
        snapshot=Path(command[command.index('--xml')+1])
        assert snapshot.parent==path.parent and snapshot!=path
        assert snapshot.read_bytes()==original
        # Even an exporter that rewrites its input must leave the source intact.
        snapshot.write_bytes(b'exporter changed its copy')
        folder=Path(command[command.index('--output-dir')+1])
        (folder/'reference.participant-guide.html').write_text('<!doctype html><html><head><style>h1{color:green}</style></head><body><h1>Observed task</h1></body></html>')
        return subprocess.CompletedProcess(command,0,'','')
    monkeypatch.setattr(subprocess,'run',export)
    args=dict(op='references', roots=[str(tmp_path)], repo=str(tmp_path), path=str(path),
              selection_id=row['id'],kind='participant-guide',offset=0)
    result=guest.dispatch(args)
    assert hashlib.sha256(result['chunk'].encode()).hexdigest()==result['sha256']
    details=json.loads(gzip.decompress(base64.b64decode(result['chunk'])))
    assert details['xml_sha256']==row['sha256']
    assert 'Observed task' in details['html']
    assert path.read_bytes()==original
    assert guest.dispatch(args)==result and len(calls)==1
    path.write_bytes(original+b' ')
    with pytest.raises(ValueError,match='changed'):
        guest.dispatch(args)


def test_reference_export_reports_failures_and_rejects_path_escape(tmp_path,monkeypatch):
    path=source(tmp_path)
    row=guest.inspect(path)[0]
    args=dict(op='references', roots=[str(tmp_path)],repo=str(tmp_path),path=str(path),
              selection_id=row['id'],kind='attack-graph')
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a[0],1,'','secret exception'))
    with pytest.raises(ValueError,match='could not export') as error:
        guest.dispatch(args)
    assert 'secret exception' not in str(error.value)
    with pytest.raises(ValueError,match='outside'):
        guest.dispatch(dict(args,roots=[str(tmp_path/'unrelated')]))
    with pytest.raises(ValueError,match='Unknown'):
        guest.dispatch(dict(args,kind='arbitrary'))


from test_pve_auth import pve
from test_workflow import lab


def test_host_reference_access_is_owner_scoped_and_requires_current_vm_access(pve,lab,tmp_path,monkeypatch):
    from cyber_agent_flow_orchestrator import scenarios,samples
    from cyber_agent_flow_orchestrator.config import load
    from cyber_agent_flow_orchestrator.workspaces import Workspace
    from cyber_agent_flow_orchestrator.access import AccessDenied
    from test_user_access import access,vm
    pve[0]['resources']['operator@pve']=[vm(9402),vm(9403)]
    user=access(pve)
    workspace=Workspace(tmp_path/'runs',user.username)
    workspace.save_roles(dict(scenarioforge=9402,participant=9403,core=None),user)
    cfg,runtime,_,_=load(lab[0]);cfg.setdefault('monitoring',{})['scenarioforge_xml_roots']=[str(tmp_path)]
    manager=samples.SampleManager(runtime,tmp_path/'runs')
    controller=scenarios.ScenarioExperiments(cfg,runtime,tmp_path/'runs',manager)
    path=source(tmp_path)
    def export(command,**kwargs):
        folder=Path(command[command.index('--output-dir')+1])
        (folder/'reference.participant-guide.html').write_text('<html><head></head><body>Guide</body></html>')
        return subprocess.CompletedProcess(command,0,'','')
    monkeypatch.setattr(subprocess,'run',export)
    class Remote:
        def call(self,vmid,op,**data):
            return guest.dispatch(dict(data,op=op,repo=str(tmp_path)))
    monkeypatch.setattr(scenarios,'guest',lambda backend:Remote())
    try:
        selection=next(item for item in controller.catalogue(user)['items'] if item['scenario']=='Selected')
        result=controller.references(user,selection['id'],'participant-guide')
        assert result['scenario']=='Selected' and 'Guide' in result['html']
        with pytest.raises(samples.SampleRequestError,match='Reload'):
            controller.references(user,'f'*64,'participant-guide')
        pve[0]['resources']['operator@pve']=[]
        with pytest.raises(AccessDenied):
            controller.references(access(pve),selection['id'],'participant-guide')
    finally:manager.close()


def test_reference_reads_exporter_declared_filename(tmp_path,monkeypatch):
    path=source(tmp_path);row=guest.inspect(path)[0]
    def export(command,**kwargs):
        assert command[3]=='guides' and '--phase' not in command
        output=Path(command[command.index('--output-dir')+1])
        guide=output/'scenario.participant.html';guide.write_text('<h1>Declared guide file</h1>')
        return subprocess.CompletedProcess(command,0,json.dumps({'ok':True,'outputs':{'participant':{'html':str(guide)}}}),'')
    monkeypatch.setattr(subprocess,'run',export)
    result=guest.dispatch(dict(op='references',roots=[str(tmp_path)],repo=str(tmp_path),path=str(path),selection_id=row['id'],kind='participant-guide'))
    details=json.loads(gzip.decompress(base64.b64decode(result['chunk'])))
    assert 'Declared guide file' in details['html']


def test_reference_rejects_missing_or_escaped_declared_file(tmp_path,monkeypatch):
    path=source(tmp_path);row=guest.inspect(path)[0]
    outside=tmp_path/'outside.html';outside.write_text('private content')
    monkeypatch.setattr(subprocess,'run',lambda command,**kw:subprocess.CompletedProcess(command,0,
        json.dumps({'outputs':{'participant':{'html':str(outside)}}}),''))
    with pytest.raises(ValueError,match='did not produce'):
        guest.dispatch(dict(op='references',roots=[str(tmp_path)],repo=str(tmp_path),path=str(path),selection_id=row['id'],kind='participant-guide'))
