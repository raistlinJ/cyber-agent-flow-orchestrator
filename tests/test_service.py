import json

import pytest

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import service, workflow
from cyber_agent_flow_orchestrator.__main__ import main
from test_workflow import lab


def test_manage_run_from_service_and_cli(lab, tmp_path, capsys):
    config, output, agent, _ = lab
    progress = []
    assert service.run(config, output, agent=agent, progress=progress.append) == 0
    assert progress and not capsys.readouterr().out
    calls = len(agent.calls)
    report = service.status(output)
    assert report['recorded_status'] == 'completed'
    assert report['coordinator_active'] is False
    assert report['evaluation']['attempt_count'] == 2
    assert service.list_runs(output.parent)[0]['workflow_id'] == 'fixture'
    assert main(['results', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['evaluation']['summary']['mean_score'] == .5
    assert main(['logs', str(output), '--stage', 'artifact-generate', '--lines', '1']) == 0
    assert 'prepared' in capsys.readouterr().out
    assert main(['export', str(output), '--destination', str(tmp_path / 'exported')]) == 0
    assert json.loads(capsys.readouterr().out)['attempts'] == 2
    assert (tmp_path / 'exported/workflow-summary.json').is_file()
    assert len(agent.calls) == calls  # Inspection and export never contact guests.


def test_status_pre_evaluation_and_export_error_do_not_create_evaluation_directory(lab, tmp_path):
    config, output, agent, _ = lab
    agent.fail = True
    with pytest.raises(ValueError):
        workflow.run(config, output, agent=agent)
    assert service.status(output)['evaluation'] is None
    with pytest.raises(ValueError, match='No experiment manifest'):
        service.export_results(output, tmp_path / 'export')
    assert not (output / 'evaluation').exists()


def test_exports_refuse_while_workflow_active(lab, tmp_path):
    config, output, agent, _ = lab
    service.run(config, output, agent=agent)
    with ev.lease(output / '.workflow.lock'):
        assert service.status(output)['coordinator_active'] is True
        with pytest.raises(ValueError, match='Already locked'):
            service.export_results(output, tmp_path / 'export')
    assert not (tmp_path / 'export').exists()


def test_resume_alias_routes_options_and_errors_go_to_stderr(monkeypatch, capsys, tmp_path):
    received = {}
    def run(config, output, **options):
        received.update(config=config, output=output, **options)
        return 0
    monkeypatch.setattr(service, 'run', run)
    assert main(['resume', 'workflow.yaml', '--output', 'runs/test', '--retry-failed']) == 0
    assert received['resume'] and received['retry_failed']
    assert main(['status', str(tmp_path / 'missing')]) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'No workflow journal' in captured.err


def test_failed_results_include_collected_diagnostics_without_guest_calls(lab):
    config, output, agent, _ = lab
    service.run(config, output, agent=agent)
    evaluation = output / 'evaluation'
    record_path = next(evaluation.glob('trials/*/attempt-*/attempt.json'))
    record = ev.read_json(record_path)
    record.update(status='error', errors=['The session hit an internal runtime error.'])
    ev.write_json(record_path, record)
    guest = record_path.parent / 'guest-output'
    guest.mkdir()
    (guest / 'worker.log').write_text('discard-me\n' * 100 + 'Traceback:\nValueError: model unavailable; token=secret-value\n')
    ev.write_json(guest / 'model_calls/call-000001.json', dict(
        request={'prompt':'PRIVATE PROMPT', 'api_key':'PRIVATE KEY'},
        response={'text':'PRIVATE RESPONSE'}, error='Model missing; api_key=secret-value'))
    calls = len(agent.calls)
    result = service.results(output)
    diagnostic = result['failure_diagnostics'][0]
    assert diagnostic['model_errors'][0]['error'] == 'Model missing; api_key=[redacted]'
    assert 'ValueError: model unavailable' in diagnostic['logs'][0]['text']
    assert len(diagnostic['logs'][0]['text'].splitlines()) <= 80
    encoded = json.dumps(diagnostic)
    assert 'secret-value' not in encoded and 'PRIVATE' not in encoded
    assert len(agent.calls) == calls


def test_failure_diagnostics_do_not_follow_paths_outside_attempt(tmp_path):
    from cyber_agent_flow_orchestrator.failure_details import collected_failures
    root = tmp_path / 'evaluation'
    folder = root / 'trials/trial-000001/attempt-0001'
    folder.mkdir(parents=True)
    private = tmp_path / 'another-user.log'
    private.write_text('PRIVATE OTHER USER')
    (folder / 'worker.log').symlink_to(private)
    rows = [dict(status='error', trial_id='trial-000001', attempt=1, attempt_path=str(folder.relative_to(root)))]
    result = collected_failures(root, rows)
    assert result[0]['notes'] and not result[0]['logs']
    assert 'PRIVATE OTHER USER' not in json.dumps(result)
