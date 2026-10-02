import hashlib
import json
import os
import pwd
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import scenario_guest, scenarios, samples, service
from cyber_agent_flow_orchestrator.evaluation_tasks import validate_tasks
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve
from test_user_access import access, vm
from test_workflow import lab

TASKS = [
    dict(id='http-token', family='http', prompt='Read the token at http://10.77.0.10/. Return JSON.',
         verifier=dict(type='json_equals', expected={'token':'test-token'}),
         required_checks=['containers','ports']),
    dict(id='http-title', family='http', prompt='Read the page title.',
         verifier=dict(type='contains_all', expected=['Demo']), required_checks=['services'])
]


def source(tmp_path):
    path = tmp_path/'saved.xml'
    root = ET.Element('Scenarios')
    for name in ['Selected', 'Other']:
        node = ET.SubElement(ET.SubElement(root,'Scenario',name=name),'FlowState')
        node.text = json.dumps({'chain':[{'id':'7','ipv4':'10.77.0.10'}], 'evaluation_tasks':[TASKS[0]], 'keep':'artifact-source'})
    ET.ElementTree(root).write(path)
    return path


def snapshot_args(tmp_path,path):
    row=scenario_guest.inspect(path)[0]
    return dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path), path=str(path),
                token='a'*32, selection_id=row['id'], user=pwd.getpwuid(os.getuid()).pw_name)


def test_task_override_is_in_snapshot_not_source_and_is_hash_checked(tmp_path):
    path=source(tmp_path)
    original=path.read_bytes()
    args=snapshot_args(tmp_path,path)
    result=scenario_guest.dispatch(dict(args,tasks=validate_tasks(TASKS)))
    saved=Path(result['snapshot_path'])
    assert path.read_bytes()==original
    root=ET.parse(saved)
    selected=json.loads(root.find('./Scenario[@name="Selected"]/FlowState').text)
    other=json.loads(root.find('./Scenario[@name="Other"]/FlowState').text)
    assert selected['evaluation_tasks']==TASKS and selected['keep']=='artifact-source'
    assert selected['chain']==[{'id':'7','ipv4':'10.77.0.10'}]
    assert other['evaluation_tasks']==[TASKS[0]]
    assert result['sha256']!=result['source_sha256']
    assert scenario_guest.dispatch(dict(args,tasks=TASKS))==result
    assert scenario_guest.dispatch(dict(op='check',repo=str(tmp_path),token='a'*32,sha256=result['sha256']))
    saved.write_bytes(original)
    with pytest.raises(ValueError,match='changed'):
        scenario_guest.dispatch(dict(op='check',repo=str(tmp_path),token='a'*32,sha256=result['sha256']))


def test_task_preview_is_bounded_and_detects_stale_selection(tmp_path):
    path=source(tmp_path)
    root=ET.parse(path)
    node=root.find('./Scenario/FlowState')
    flow=json.loads(node.text)
    flow['evaluation_tasks'][0]['prompt']='Detailed task ' * 700
    node.text=json.dumps(flow)
    root.write(path)
    args=snapshot_args(tmp_path,path)
    encoded=''
    while True:
        result=scenario_guest.dispatch(dict(args,op='tasks',offset=len(encoded)))
        assert len(result['chunk'])<=4096
        encoded+=result['chunk']
        if len(encoded)==result['total']:break
    assert hashlib.sha256(encoded.encode()).hexdigest()==result['sha256']
    assert json.loads(encoded)==flow['evaluation_tasks']
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError,match='changed'):
        scenario_guest.dispatch(dict(args,op='tasks'))


@pytest.mark.parametrize('tasks', [
    [], TASKS*2, [dict(TASKS[0],prompt='')], [dict(TASKS[0],id='bad id')],
    [dict(TASKS[0],verifier={'type':'shell','expected':'echo nope'})],
    [dict(TASKS[0],verifier={'type':'contains_all','expected':[]})],
    [dict(TASKS[0],required_checks=[])], [dict(TASKS[0],extra=True)],
    [dict(TASKS[0],prompt='x'*65536)], [dict(TASKS[0],verifier={'type':'json_equals','expected':float('nan')})],
])
def test_invalid_tasks(tasks):
    with pytest.raises(samples.SampleRequestError):
        validate_tasks(tasks)


def test_tasks_create_preview_and_rerun_preserve_definitions(pve,lab,tmp_path,monkeypatch):
    monkeypatch.setattr(ev, 'GuestAgent', lambda backend: lab[2])
    pve[0]['resources']['operator@pve']=[vm(9402),vm(9403)]
    user=access(pve)
    workspace=Workspace(tmp_path/'runs',user.username)
    workspace.save_roles(dict(scenarioforge=9402,participant=9403,core=None),user)
    cfg,runtime,_,_=load(lab[0])
    cfg.setdefault('monitoring',{}).update(scenarioforge_path=str(tmp_path),scenarioforge_xml_roots=[str(tmp_path)])
    cfg['scenarioforge']['user']=pwd.getpwuid(os.getuid()).pw_name
    manager=samples.SampleManager(runtime,tmp_path/'runs')
    controller=scenarios.ScenarioExperiments(cfg,runtime,tmp_path/'runs',manager)
    path=source(tmp_path)
    calls=[]
    class Remote:
        def call(self,vmid,op,**data):
            calls.append(op)
            return scenario_guest.dispatch(dict(data,op=op))
    monkeypatch.setattr(scenarios,'guest',lambda backend:Remote())
    def execute(config,output,**kwargs):
        record=ev.read_json(output/'workflow.json')
        flow=json.loads(ET.parse(record['workflow']['scenarioforge']['xml']).find('./Scenario/FlowState').text)
        assert flow['evaluation_tasks']==TASKS
        record['status']='completed'
        ev.write_json(output/'workflow.json',record)
    monkeypatch.setattr(service,'run',execute)
    try:
        selection=next(i for i in controller.catalogue(user)['items'] if i['path']==str(path) and i['scenario']=='Selected')
        assert controller.tasks(user,selection['id'])['tasks']==[TASKS[0]]
        result=controller.create(user,selection['id'],'b'*32,'10.77.0.0/24','',tasks=TASKS,provide_progressive_hints=True)
        output=workspace.run_path(result['run_id'])
        assert ev.read_json(output/'workflow.json')['runtime']['execution']['provide_progressive_hints'] is True
        assert ev.read_json(output/'inputs/evaluation-tasks.json')==TASKS
        assert ev.read_json(output/'workflow.json')['scenario_experiment']['evaluation_tasks']==TASKS
        controller.run_saved(user,result['run_id'],'c'*32)
        manager.jobs[user.username].result(timeout=10)
        assert service.status(output)['recorded_status']=='completed'
        again=controller.run_saved(user,result['run_id'],'d'*32)
        manager.jobs[user.username].result(timeout=10)
        assert ev.read_json(workspace.run_path(again['run_id'])/'inputs/evaluation-tasks.json')==TASKS
        assert service.status(workspace.run_path(again['run_id']))['recorded_status']=='completed'
        assert ev.read_json(workspace.run_path(again['run_id'])/'workflow.json')['runtime']['execution']['provide_progressive_hints'] is True
        with pytest.raises(samples.SampleRequestError):
            controller.create(user,selection['id'],'e'*32,'10.77.0.0/24','',tasks=[])
    finally:
        manager.close()


def test_hints_off_compatible_with_older_evaluator(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'cyber_agent_flow_eval.hints', None)
    execution = {'max_turns': 6, 'provide_progressive_hints': True}
    scenarios.progressive_hint_settings(execution, False)
    assert execution == {'max_turns': 6}
    with pytest.raises(samples.SampleRequestError, match='updated cyber-agent-flow-eval'):
        scenarios.progressive_hint_settings(execution, True)
    assert execution == {'max_turns': 6}
