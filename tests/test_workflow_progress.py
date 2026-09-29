import base64
from types import SimpleNamespace
import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import workflow, service
from cyber_agent_flow_orchestrator.workflow_progress import observe_transfers, observe_command, build
from test_workflow import lab


def test_stage_progress_survives_results_and_status_is_read_only(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent, progress=None)
    count = len(agent.calls)
    result = service.status(output)['workflow_progress']
    assert len(agent.calls) == count
    assert result['completed_steps'] == result['total_steps']
    assert all(s['started_at'] and s['ended_at'] for s in result['steps'])
    assert result['transfer']['verified']
    assert result['transfer']['vmid'] == 9403
    assert [r['vmid'] for r in result['roles']] == [9402, None, 9403, None]
    assert result['current_step'] == 'evaluate'
    assert result['readiness']['session_id'] == 9
    assert all(c['status']=='pass' for c in result['readiness']['checks'])


def test_failed_stage_keeps_location_and_timing(lab):
    config, output, agent, _ = lab
    agent.fail = True
    with pytest.raises(ValueError):
        workflow.run(config, output, agent=agent, progress=None)
    result = service.status(output)['workflow_progress']
    failed = result['current']
    assert failed['id'] == 'artifact-generate'
    assert failed['status'] == 'failed'
    assert failed['vmid'] == 9403
    assert failed['ended_at'] and 'exited 1' in failed['error']
    assert next(s for s in result['steps'] if s['id']=='evaluate')['status']=='pending'


def test_download_counts_existing_reads_and_restores_methods_on_failure():
    records, calls = [], []
    class Agent:
        def call(self, vmid, op, **data):
            calls.append(op)
            return {'size': 3} if op == 'stat' else {'content': base64.b64encode(b'abc').decode()}
        def get(self, vmid, path):
            self.call(vmid, 'stat', path=path)
            self.call(vmid, 'read', path=path)
            assert wf.journal['transfer']['received_bytes'] == 3
            assert not wf.journal['transfer']['verified']
            raise ValueError('checksum mismatch')
    agent = Agent()
    original_get, original_call = agent.get, agent.call
    wf = SimpleNamespace(agent=agent, journal={'current_step':'fetch'}, save=lambda:records.append(True))
    with pytest.raises(ValueError), observe_transfers(wf):
        agent.get(9402, '/exports/suite.zip')
    assert calls == ['stat','read']
    assert wf.journal['transfer']['status'] == 'failed'
    assert not wf.journal['transfer']['verified']
    assert agent.get == original_get and agent.call == original_call


def test_guest_observation_excludes_output_and_cannot_prevent_cleanup():
    agent = SimpleNamespace(qm=lambda *a, **k:dict(pid=12, exited=False, **{'out-data':'secret-output'}))
    original = agent.qm
    def save():
        raise OSError('disk unavailable')
    wf = SimpleNamespace(agent=agent, journal={'current_step':'deploy'}, save=save)
    with observe_command(wf, 9402):
        assert agent.qm(['guest','exec'])['pid'] == 12
    assert agent.qm == original
    assert 'secret-output' not in str(wf.journal)
    assert wf.journal['guest_observation']['step']=='deploy'


def test_interrupted_stage_does_not_keep_running(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent, progress=None)
    journal = ev.read_json(output/'workflow.json')
    journal['progress_steps']['evaluate'].update(status='running', ended_at=None)
    result = build(journal, 'interrupted', False)
    assert result['current']['status']=='interrupted'
    assert not result['current']['active']
