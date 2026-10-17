import json
import urllib.error
from pathlib import Path
import pytest
import yaml
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import input_preflight, input_probe_guest as guest, workflow
from test_workflow import lab


def judge_runtime(variable='TEST_PREFLIGHT_KEY'):
    return {'model':{'provider':'openai','name':'participant','url':'http://model.invalid/v1'},
            'judge':{'enabled':True,'model':{'provider':'openai','name':'judge','url':'http://judge.invalid/v1','api_key_env':variable}}}


def test_missing_judge_key_fails_before_any_model_request(monkeypatch):
    monkeypatch.delenv('TEST_PREFLIGHT_KEY',raising=False)
    monkeypatch.setattr('cyber_agent_flow_eval.judge._completion',lambda *args, **kwargs:pytest.fail('No request with missing credentials'))
    with pytest.raises(ValueError,match='host credential.*missing'):
        input_preflight.check_judge(judge_runtime())


def test_judge_probe_is_synthetic_and_validates_contract(monkeypatch,tmp_path):
    monkeypatch.setenv('TEST_PREFLIGHT_KEY','never-log-this-key')
    calls=[]
    def completion(config,messages,timeout, **kwargs):
        calls.append((config,messages,timeout))
        return json.dumps({'action':'verdict','passed':False,'score':0,'reason':'probe','evidence':[]}),{}
    monkeypatch.setattr('cyber_agent_flow_eval.judge._completion',completion)
    result=input_preflight.check_judge(judge_runtime(),tmp_path)
    assert 'authenticated' in result['message']
    assert 'synthetic passed=false verdict is expected' in result['message']
    events=(tmp_path/'judge-preflight-events.jsonl').read_text()
    assert 'Test LLM Judge' in events
    assert 'does not mean the experiment failed' in events
    assert 'never-log-this-key' not in json.dumps(calls)
    assert all('Synthetic' in m['content'] or 'preflight' in m['content'] for m in calls[0][1])
    monkeypatch.setattr('cyber_agent_flow_eval.judge._completion',lambda *args, **kwargs:('{"action":"read_evidence","file":"anything"}',{}))
    with pytest.raises(ValueError,match='usable JSON verdict'):
        input_preflight.check_judge(judge_runtime())


@pytest.mark.parametrize('failure',['judge-key','guest-key','core-path','network-policy'])
def test_input_failure_blocks_all_prepare_deploy_and_trials(lab,monkeypatch,failure):
    config,output,agent,_=lab
    cfg=yaml.safe_load(config.read_text())
    runtime_path=config.parent/cfg['runtime']
    runtime=yaml.safe_load(runtime_path.read_text())
    original=agent.call
    if failure=='judge-key':
        runtime['judge']=judge_runtime()['judge']
        monkeypatch.delenv('TEST_PREFLIGHT_KEY',raising=False)
    if failure=='network-policy':runtime['execution']['network_policy']['disallow']=runtime['execution']['network_policy']['allow']
    if failure=='core-path':
        cfg['monitoring']={'core_vmid':9404}
        config.write_text(yaml.safe_dump(cfg))
    def call(vmid,op,**data):
        if op=='input_check' and ((failure=='guest-key' and data['role']=='participant') or (failure=='core-path' and data['role']=='core')):
            raise ValueError('Simulated missing credential or unwritable guest path')
        return original(vmid,op,**data)
    agent.call=call
    runtime_path.write_text(yaml.safe_dump(runtime))
    with pytest.raises(input_preflight.InputPreflightError,match='before deployment'):
        workflow.run(config,output,agent=agent,progress=None)
    audit=ev.read_json(output/'input-preflight.json')
    assert audit['status']=='failed' and any(c['status']=='fail' for c in audit['checks'])
    assert not [op for _,op,_ in agent.calls if op in ('hook','start','preflight')]
    assert not (output/'evaluation').exists()
    assert ev.read_json(output/'workflow.json')['progress_steps']['preflight']['status']=='failed'


def test_successful_preflight_is_saved_and_shown_in_progress(lab):
    config,output,agent,_=lab
    workflow.run(config,output,agent=agent,progress=None)
    audit=ev.read_json(output/'input-preflight.json')
    assert audit['status']=='passed'
    assert {'inputs','judge','engine','participant-model','scenarioforge'} <= {c['id'] for c in audit['checks']}
    first_hook=next(i for i,(_,op,_) in enumerate(agent.calls) if op=='hook')
    assert any(op=='input_check' for _,op,_ in agent.calls[:first_hook])
    from cyber_agent_flow_orchestrator import service
    step=next(s for s in service.status(output)['workflow_progress']['steps'] if s['id']=='preflight')
    assert step['input_checks']==audit['checks']


@pytest.mark.parametrize('provider',['openai','litellm','ollama_direct','claude'])
def test_participant_synthetic_probe_exercises_generation_and_tools(monkeypatch,provider):
    calls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):
            if provider=='claude':return b'{"content":[{"type":"text","text":"READY"}]}'
            if provider=='ollama_direct':return b'{"message":{"content":"READY"}}'
            return b'{"choices":[{"message":{"content":"READY"}}]}'
    def open_request(request,**kwargs):
        calls.append(request)
        return Response()
    monkeypatch.setattr(guest.urllib.request,'urlopen',open_request)
    result=guest.check_model({'provider':provider,'url':'http://model.invalid/v1','name':'configured'},'private-test-key',tools=True)
    body=json.loads(calls[0].data)
    assert body['model']=='configured' and body['tools']
    assert 'Synthetic' in body['messages'][0]['content']
    assert 'private-test-key' not in json.dumps(result)


@pytest.mark.parametrize('status',[401,404,400])
def test_participant_http_failure_is_actionable_and_redacted(monkeypatch,status):
    def unavailable(request,**kwargs):
        raise urllib.error.HTTPError(request.full_url,status,'refused',{},None)
    monkeypatch.setattr(guest.urllib.request,'urlopen',unavailable)
    with pytest.raises(ValueError,match='HTTP '+str(status)) as error:
        guest.check_model({'provider':'openai','url':'http://model.invalid/v1','name':'bad-model'},'private-test-key')
    assert 'private-test-key' not in str(error.value)


def test_guest_uses_exact_service_environment_and_never_returns_key(tmp_path,monkeypatch):
    monkeypatch.setattr(guest,'access',lambda *a,**k:None)
    monkeypatch.setattr(guest.pwd,'getpwnam',lambda name:object())
    secret=tmp_path/'worker.env'
    secret.write_text('ACTUAL_KEY="private=key"\n')
    seen=[]
    monkeypatch.setattr(guest,'check_model',lambda model,key,**kwargs:seen.append(key) or {'message':'ready'})
    data={'role':'participant','user':'participant','engine':{'path':'/engine','python':'/engine/python'},
          'model':{'api_key_env':'ACTUAL_KEY'},'environment_file':str(secret)}
    result=guest.dispatch(data)
    assert seen==['private=key'] and 'private=key' not in json.dumps(result)
    secret.write_text('OTHER_KEY="something"\n')
    with pytest.raises(ValueError,match='ACTUAL_KEY.*missing'):
        guest.dispatch(data)
    assert len(seen)==1


def test_virtualenv_python_symlink_is_valid_input(tmp_path,monkeypatch):
    executable=tmp_path/'python-real'
    executable.write_text('fixture')
    alias=tmp_path/'python'
    alias.symlink_to(executable)
    class Result:
        returncode=0
    monkeypatch.setattr(guest.subprocess,'run',lambda *args,**kwargs:Result())
    guest.access(alias,'participant',executable=True)


def test_judge_preflight_honors_600_second_budget_and_persists_stream(tmp_path,monkeypatch):
    monkeypatch.setenv('TEST_PREFLIGHT_KEY','private-key')
    runtime=judge_runtime()
    runtime['judge']['timeout_seconds']=600
    def completion(config,messages,timeout,on_delta):
        assert timeout==600
        on_delta('reasoning','Synthetic check only.')
        on_delta('response','{"action":"verdict","passed":false,"score":0,"reason":"preflight","evidence":[]}')
        return '{"action":"verdict","passed":false,"score":0,"reason":"preflight","evidence":[]}',{}
    monkeypatch.setattr('cyber_agent_flow_eval.judge._completion',completion)
    input_preflight.check_judge(runtime,tmp_path)
    events=(tmp_path/'judge-preflight-events.jsonl').read_text()
    assert 'Synthetic check only.' in events and 'passed' in events
    assert 'private-key' not in events


def test_judge_probe_supports_no_timeout_and_logs_warning(monkeypatch,tmp_path):
    runtime=judge_runtime();runtime['judge']['timeout_seconds']=0
    monkeypatch.setenv('TEST_PREFLIGHT_KEY','fixture-key')
    def completion(config,messages,timeout,**kwargs):
        assert timeout==0
        return json.dumps(dict(action='verdict',passed=False,score=0,reason='synthetic',evidence=[])),{}
    monkeypatch.setattr('cyber_agent_flow_eval.judge._completion',completion)
    assert 'authenticated' in input_preflight.check_judge(runtime,tmp_path)['message']
    assert 'no timeout (may wait indefinitely)' in (tmp_path/'judge-preflight-events.jsonl').read_text()


def test_old_caf_no_timeout_fails_before_model_probe(tmp_path,monkeypatch):
    monkeypatch.setattr(guest,'access',lambda *a,**k:None)
    monkeypatch.setattr(guest.pwd,'getpwnam',lambda name:object())
    monkeypatch.setattr(guest,'check_model',lambda *a,**k:pytest.fail('Must fail before endpoint probe'))
    (tmp_path/'mcp_kali.py').write_text('# Old CAF version')
    with pytest.raises(ValueError,match='No-timeout tool actions require an updated'):
        guest.dispatch(dict(role='participant',user='participant',engine=dict(path=str(tmp_path),python='/python'),tool_timeout=0,model={}))
