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
    fetch = next(stage for stage in result['steps'] if stage['id']=='fetch')
    assert fetch['transfers'] and fetch['transfers'][0]['verified']
    assert fetch['file_count'] and fetch['events']
    prepare = next(stage for stage in result['steps'] if stage['id']=='artifact-generate')
    assert prepare['log_tail'] is not None
    assert prepare['timeout_seconds'] and prepare['exitcode'] == 0
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


def test_command_streams_partial_log_redacts_console_and_heartbeats(tmp_path, monkeypatch):
    import time
    clock = [0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    calls = []
    content = b'planner starting\npassword=hidden-value\n'
    class Agent:
        def qm(self, args, **kwargs):
            return dict(pid=12, exited=False)
        def call(self, vmid, op, **data):
            calls.append((op, data))
            self.qm(['guest', 'exec', vmid])
            return {'content':base64.b64encode(content[data['offset']:]).decode()}
    agent = Agent()
    original = agent.qm
    (tmp_path/'logs').mkdir()
    wf = SimpleNamespace(agent=agent, output=tmp_path, journal={'current_step':'prepare-demo'}, save=lambda:None)
    attempt = dict(log_path='/tmp/demo.log', host_log='logs/demo.log', timeout_seconds=900)
    with observe_command(wf, 9402, attempt):
        agent.qm(['guest', 'exec-status', 9402, 12])
        assert (tmp_path/'logs/demo.log').read_bytes() == content
        assert attempt['live_log_bytes'] == len(content)
        clock[0] = 5
        agent.qm(['guest', 'exec-status', 9402, 12])
        assert len(calls) == 1
        clock[0] = 31
        agent.qm(['guest', 'exec-status', 9402, 12])
        assert calls[-1][1]['offset'] == len(content)
        assert len(calls) == 2  # Nested log-read qm calls do not recurse.
        attempt['exitcode'] = 0
        clock[0] = 62
        agent.qm(['guest', 'exec-status', 9402, 12])
        assert len(calls) == 2  # Cleanup/download operations cannot append duplicate output.
    assert agent.qm == original
    events = wf.journal['events']
    assert any(e['kind']=='heartbeat' and '31s' in e['message'] for e in events)
    assert 'hidden-value' not in str(events)
    assert any('planner starting' in e['message'] for e in events)


def test_live_log_failure_does_not_fail_command_or_repeat_warnings(tmp_path, monkeypatch):
    import time
    clock = [0]
    monkeypatch.setattr(time, 'monotonic', lambda:clock[0])
    class Agent:
        def qm(self, *a, **k):
            return dict(pid=12, exited=False)
        def call(self, *a, **k):
            raise ValueError('secret RPC payload')
    agent = Agent()
    wf = SimpleNamespace(agent=agent, output=tmp_path, journal={'current_step':'deploy'}, save=lambda:None)
    attempt = dict(log_path='/tmp/demo.log', host_log='logs/demo.log', timeout_seconds=900)
    with observe_command(wf, 9402, attempt):
        agent.qm([])
        clock[0] = 11
        agent.qm([])
    assert len(wf.journal['events']) == 1
    assert 'secret RPC payload' not in str(wf.journal)


def test_stage_log_preview_is_bounded_redacted_and_cannot_escape_run(lab, tmp_path):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent, progress=None)
    journal = ev.read_json(output/'workflow.json')
    stage = journal['stages']['artifact-generate']
    path = output/stage['log']
    path.write_text('old line\n'*100+'password=secret-value\n')
    view = build(journal, 'completed', False, root=output)
    preview = next(s for s in view['steps'] if s['id']=='artifact-generate')['log_tail']
    assert len(preview.splitlines()) <= 60
    assert 'secret-value' not in preview and '[redacted]' in preview
    outside = tmp_path/'outside.log'
    outside.write_text('PRIVATE OTHER RUN')
    path.unlink()
    path.symlink_to(outside)
    view = build(journal, 'completed', False, root=output)
    assert next(s for s in view['steps'] if s['id']=='artifact-generate')['log_tail'] is None


def test_trial_reset_commands_keep_evaluation_progress_active(lab):
    from cyber_agent_flow_orchestrator.workflow_progress import step, checkpoint
    config,output,agent,_=lab
    cfg,runtime,_,_=workflow.load(config);output.mkdir()
    wf=workflow.Workflow(output,{'workflow':cfg,'runtime':runtime,'stages':{}},agent,progress=None)
    with step(wf,'evaluate'):
        with step(wf,'reset-trial-1'):
            checkpoint(wf,'Reset command output observed','command-output')
            current=build(wf.journal,'evaluating',True)
            assert current['current_step']=='evaluate' and current['current']['active']
            assert any('Reset command' in e['message'] for e in current['current']['events'])
        assert wf.journal['current_step']=='evaluate'
        checkpoint(wf,'CAF worker started')
        assert wf.journal['progress_steps']['evaluate']['operation']=='CAF worker started'
    assert wf.journal['progress_steps']['evaluate']['status']=='completed'
    assert any('Reset command' in e['message'] for e in wf.journal['progress_steps']['evaluate']['events'])
