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
