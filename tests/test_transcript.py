import json
import threading
from urllib.request import Request,urlopen
import pytest
from cyber_agent_flow_orchestrator import transcript
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from cyber_agent_flow_eval import integration as ev
from test_pve_auth import pve,make_auth
from test_workflow import lab
from test_user_access import Probe
from test_web import request
from https_fixture import secure_server,PASSWORD


def test_incremental_cursor_waits_for_complete_lines_and_redacts_credentials(tmp_path):
    evaluation=tmp_path/'evaluation';evaluation.mkdir()
    path=evaluation/'live-transcript.jsonl'
    a=json.dumps({'event':{'type':'prompt','text':'Fetch token from the website','args':{'api_key':'private-key'}}}).encode()+b'\n'
    b=json.dumps({'event':{'type':'tool_result','result':'FLAG{observed} Bearer private-auth'}}).encode()+b'\n'
    path.write_bytes(a+b[:10])
    batch=transcript.read(tmp_path)
    assert batch['cursor']==len(a) and len(batch['events'])==1 and not batch['more']
    assert batch['events'][0]['event']['args']['api_key']=='[redacted]'
    assert transcript.safe({'usage':float('nan')})=={'usage':None}
    path.write_bytes(a+b)
    batch=transcript.read(tmp_path,len(a))
    assert batch['cursor']==len(a+b) and len(batch['events'])==1
    assert 'FLAG{observed}' in batch['events'][0]['event']['result']
    assert 'private-auth' not in json.dumps(batch)
    with pytest.raises(ValueError):transcript.read(tmp_path,-1)
    path.unlink();path.symlink_to('/etc/passwd')
    with pytest.raises(OSError):transcript.read(tmp_path)


def test_https_stream_delivers_before_completion_replays_cursor_and_is_owner_scoped(pve,lab,tmp_path,monkeypatch):
    dashboard=UserDashboard(lab[0],tmp_path/'runs',2,lambda b,a:Probe(b,a,[]))
    root=Workspace(tmp_path/'runs','operator@pve').run_path('sample-live')
    root.mkdir(parents=True);ev.write_json(root/'workflow.json',{'status':'evaluating'})
    (root/'evaluation').mkdir()
    path=root/'evaluation/live-transcript.jsonl'
    first=json.dumps({'trial_id':'trial-1','event':{'type':'prompt','text':'Actual prompt'}})+'\n'
    path.write_text(first)
    finished=threading.Event()
    monkeypatch.setattr(transcript,'state',lambda root:dict(status='completed' if finished.is_set() else 'evaluating',active=not finished.is_set()))
    try:
        with secure_server(dashboard,tmp_path/'secure',auth=make_auth(pve)) as server:
            assert request(server,'/api/runs/sample-live/transcript-stream')[0]==401
            code,headers,_=request(server,'/api/login',{'username':'operator@pve','password':PASSWORD})
            assert code==200
            cookie=headers['Set-Cookie'].split(';',1)[0]
            req=Request(server['origin']+'/api/runs/sample-live/transcript-stream',headers={'Cookie':cookie})
            with urlopen(req,context=server['ssl'],timeout=5) as stream:
                assert stream.headers['Content-Type'].startswith('text/event-stream')
                for _ in range(12):
                    line=stream.readline()
                    if b'Actual prompt' in line:break
                else:pytest.fail('Proxy buffered the live transcript')
                assert not finished.is_set()
                second=json.dumps({'event':{'type':'response','text':'Second returned output'}})+'\n'
                with path.open('a') as saved:saved.write(second)
                for _ in range(12):
                    if b'Second returned output' in stream.readline():break
                else:pytest.fail('Incremental transcript did not arrive')
            finished.set()
            code,_,body=request(server,'/api/runs/sample-live/transcript-stream',cookie=cookie,headers={'Last-Event-ID':str(len(first.encode()))})
            assert code==200 and b'Second returned output' in body and b'Actual prompt' not in body
            assert b'event: complete' in body
            assert request(server,'/api/runs/other-live/transcript-stream',cookie=cookie)[0]==404
            # Another valid account cannot read the owner's identically named run.
            _,headers,_=request(server,'/api/login',{'username':'other@pve','password':PASSWORD})
            other=headers['Set-Cookie'].split(';',1)[0]
            assert request(server,'/api/runs/sample-live/transcript-stream',cookie=other)[0]==404
    finally:dashboard.close()
