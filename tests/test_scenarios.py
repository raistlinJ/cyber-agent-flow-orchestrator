import hashlib
import os
import pwd
import pytest

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import scenario_guest as guest, scenarios, samples, service
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve
from test_user_access import access, vm
from test_workflow import lab

XML = b'<Scenarios><Scenario name="Saved lab"><FlowState>{"chain":[{"id":"entry"}]}</FlowState></Scenario><Scenario name="Unresolved"/></Scenarios>'


def test_listing_snapshot_and_stale_selection(tmp_path):
    source = tmp_path / 'lab.xml'
    source.write_bytes(XML)
    (tmp_path / 'link.xml').symlink_to(source)
    (tmp_path / 'bad.xml').write_text('<broken')
    rows = guest.catalogue(dict(roots=[str(tmp_path)]))['items']
    assert [row['scenario'] for row in rows] == ['Saved lab', 'Unresolved']
    assert rows[0]['resolved_chain'] and not rows[1]['resolved_chain']
    assert len(guest.catalogue(dict(roots=[str(tmp_path)], query='saved'))['items']) == 1
    args = dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path), path=str(source),
                token='a' * 32, selection_id=rows[0]['id'], user=pwd.getpwuid(os.getuid()).pw_name)
    saved = guest.dispatch(args)
    source.write_bytes(XML + b' ')
    assert open(saved['snapshot_path'], 'rb').read() == XML
    assert guest.dispatch(dict(op='check', token='a'*32, repo=str(tmp_path), sha256=rows[0]['sha256']))
    with pytest.raises(ValueError, match='changed'):
        guest.dispatch(dict(args, token='b'*32))
    with pytest.raises(ValueError, match='outside'):
        guest.dispatch(dict(args, roots=[str(tmp_path / 'elsewhere')]))
    with open(saved['snapshot_path'], 'wb') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='changed'):
        guest.dispatch(dict(op='check', token='a'*32, repo=str(tmp_path), sha256=rows[0]['sha256']))


def test_unresolved_scenario_cannot_snapshot(tmp_path):
    source = tmp_path / 'lab.xml'
    source.write_bytes(XML)
    row = guest.inspect(source)[1]
    with pytest.raises(ValueError, match='Flow chain'):
        guest.dispatch(dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path), path=str(source),
                            token='a'*32, selection_id=row['id']))


@pytest.mark.parametrize("real_workflow", [False, True])
def test_saved_scenario_create_launch_and_changed_snapshot(pve, lab, tmp_path, monkeypatch, real_workflow):
    pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=9402, participant=9403, core=None), user)
    cfg, runtime, _, _ = load(lab[0])
    manager = samples.SampleManager(runtime, tmp_path / 'runs')
    controller = scenarios.ScenarioExperiments(cfg, runtime, tmp_path / 'runs', manager)
    calls = []
    class Guest:
        changed = False
        def call(self, vmid, op, **data):
            calls.append((vmid, op, data))
            item = dict(id='a'*64, scenario='Saved lab', path='/opt/scenarioforge/uploads/lab.xml',
                        sha256=hashlib.sha256(XML).hexdigest(), resolved_chain=True, chain_length=1, bytes=len(XML))
            if op == 'list':
                return dict(items=[item], roots=data['roots'], truncated=False)
            if op == 'snapshot':
                return dict(item, snapshot_path='/opt/scenarioforge/outputs/caf-orchestrator/'+data['token']+'/scenario.xml')
            if self.changed:
                raise ValueError('Saved scenario XML changed')
            return {}
    remote = Guest()
    monkeypatch.setattr(scenarios, 'guest', lambda backend: remote)
    launched = []
    def run(source, output, **kwargs):
        cfg, runtime, _, _ = load(source)
        launched.append((cfg, runtime))
        if real_workflow:
            from cyber_agent_flow_orchestrator import workflow
            agent, manifest = lab[2], lab[3]
            agent.marker = dict(state='complete', readiness_passed=True, archive='/exports/suite.zip',
                                package_hash=manifest['package_hash'], suite_id=cfg['scenarioforge']['suite_id'])
            return workflow.run(source, output, agent=agent, **kwargs)
        kwargs['progress']('Running agent evaluation')
        record = ev.read_json(output / 'workflow.json')
        record['status'] = 'completed'
        ev.write_json(output / 'workflow.json', record)
    monkeypatch.setattr(service, 'run', run)
    try:
        controller.catalogue(user)
        settings = dict(repetitions=2, max_turns=7, wall_seconds=45, tool_timeout=15, context_window=4096)
        reply = controller.create(user, 'a'*64, 'b'*32, '10.77.0.0/24', '', evaluation=settings)
        output = workspace.run_path(reply['run_id'])
        assert reply['status'] == 'ready' and not launched
        record = ev.read_json(output / 'workflow.json')
        assert record['runtime']['conditions'][0]['tools'] == ['nmap', 'curl', 'python3']
        assert record['runtime']['backend']['before_trial'] == []
        assert record['runtime']['repetitions'] == 2
        assert all(record['runtime']['execution'][key] == value for key, value in settings.items() if key != 'repetitions')
        assert controller.create(user, 'a'*64, 'b'*32, '10.77.0.0/24', '') == reply
        controller.run_saved(user, reply['run_id'], 'c'*32)
        manager.jobs[user.username].result(timeout=10)
        assert ev.read_json(output / 'workflow.json')['status'] == 'completed'
        assert launched[0][0]['scenarioforge']['mode'] == 'execute'
        assert launched[0][0]['scenarioforge']['xml'] == record['scenario_experiment']['snapshot_path']
        assert launched[0][1]['execution']['network_policy']['allow'] == ['10.77.0.0/24']
        remote.changed = True
        again = controller.run_saved(user, reply['run_id'], 'd'*32)
        manager.jobs[user.username].result(timeout=10)
        assert ev.read_json(workspace.run_path(again['run_id']) / 'workflow.json')['status'] == 'failed'
        assert len(launched) == 1
        with pytest.raises(samples.SampleRequestError):
            controller.create(user, 'f'*64, 'e'*32, '10.77.0.0/24', '')
        assert all(vmid == 9402 for vmid, _, _ in calls)
    finally:
        manager.close()


@pytest.mark.parametrize("value", [{}, {"repetitions":1}, dict(repetitions=True,max_turns=3,wall_seconds=30,tool_timeout=10,context_window=1000), dict(repetitions=1,max_turns=0,wall_seconds=30,tool_timeout=10,context_window=1000)])
def test_invalid_evaluation_settings(value):
    with pytest.raises(samples.SampleRequestError):
        scenarios.evaluation_settings(value)
