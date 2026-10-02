from copy import deepcopy
import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import workflow, vm_preflight
from test_workflow import lab


def test_preflight_recovers_recorded_leftovers_before_new_run(lab):
    config, old, agent, _ = lab
    workflow.run(config, old, agent=agent, progress=None)
    journal=ev.read_json(old/'workflow.json')
    attempt=journal['stages']['artifact-generate']['attempts'][0]
    attempt['stopped']=False
    ev.write_json(old/'workflow.json',journal)
    hook=old/'evaluation/trials/trial-000001/attempt-0001/hook-000.json'
    hook.parent.mkdir(parents=True,exist_ok=True)
    ev.write_json(hook,dict(vmid=attempt['vmid'],unit='caf-orchestrator-abcd',stopped=False))
    new=old.parent/'next-run';new.mkdir()
    current=deepcopy(journal);current.update(stages={},progress_steps={},status='running')
    wf=workflow.Workflow(new,current,agent,progress=None);wf.save()
    agent.calls.clear()
    vm_preflight.run(wf)
    checks=[data for vmid,op,data in agent.calls if op=='preflight']
    assert any(attempt['unit'] in check['units'] for check in checks)
    assert ev.read_json(old/'workflow.json')['stages']['artifact-generate']['attempts'][0]['stopped']
    assert ev.read_json(hook)['stopped']
    assert any('caf-orchestrator-abcd' in check['units'] for check in checks)
    assert current['progress_steps']['preflight']['status']=='completed'


def test_preflight_does_not_touch_jobs_in_a_locked_run(lab):
    config,old,agent,_=lab
    workflow.run(config,old,agent=agent,progress=None)
    journal=ev.read_json(old/'workflow.json')
    new=old.parent/'next-run';new.mkdir()
    current=deepcopy(journal);current.update(stages={},progress_steps={})
    wf=workflow.Workflow(new,current,agent,progress=None);wf.save();agent.calls.clear()
    with ev.lease(old/'.workflow.lock'),pytest.raises(ValueError,match='Already locked'):
        vm_preflight.run(wf)
    assert not agent.calls
    assert current['progress_steps']['preflight']['status']=='failed'
