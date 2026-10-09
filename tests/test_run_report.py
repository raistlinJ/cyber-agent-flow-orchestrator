import json
import zipfile

import pytest
from cyber_agent_flow_orchestrator import run_report, service, workflow, run_artifacts
from cyber_agent_flow_eval import integration as ev
from test_workflow import lab


def fixture_report():
    base=dict(trial_id='trial-1',task_id='read-web',condition_id='baseline',repetition=1,attempt=1,status='completed',
              verified_success=True,execution_seconds=2,elapsed_seconds=4,model_calls=2,
              provider_usage=[dict(prompt_tokens=3,completion_tokens=2),None],usage_complete=False,
              report_telemetry=dict(available=True,calls={'curl':1},results={'curl':1},notes=[]),
              started_at='2026-10-02T10:00:00+00:00',ended_at='2026-10-02T10:00:04+00:00',
              final_answer='Found the token.\n```\n<script>alert(1)</script>')
    retry=dict(base,attempt=2,verified_success=False,provider_usage=[],execution_seconds=6)
    error=dict(base,trial_id='trial-2',condition_id='helper',status='error',verified_success=None,execution_seconds=None,
               report_telemetry=dict(available=False,calls={},results={},notes=[]),final_answer='',errors=['Connection refused; api_key=private-key'])
    return dict(workflow=dict(output='/runs/example',workflow_id='test-workflow',recorded_status='completed_with_errors'),
                run_configuration=dict(model=dict(name='local-model',provider='ollama_direct'),
                    conditions=[dict(id='baseline',tools=['curl','nmap']),dict(id='helper',tools=['curl','helper'])],
                    tasks=[dict(id='read-web',family='web',prompt='Read http://10.0.0.1/.\n<script>literal</script>\n```')],
                    system_prompts=[dict(text='Exact system instructions')],
                    schedule=[dict(trial_id='trial-1',condition_id='baseline'),dict(trial_id='trial-2',condition_id='helper'),dict(trial_id='trial-3',condition_id='helper')]),
                evaluation=dict(attempts=[base,retry,error],planned_trials=3,unstarted_trials=1,attempt_count=3,conditions={}))


def test_report_handles_retries_missing_metrics_and_untrusted_text():
    report=fixture_report()
    docs=run_report.render(report,recorded_at='fixed')
    md,html,svg=(docs[k] for k in ('experiment-summary.md','experiment-summary.html','experiment-charts.svg'))
    assert '| PASS / FAIL | 0 / 1 |' in md
    assert '| Attempts recorded | 3 |' in md
    assert 'Attempt history' in md
    assert '1 fail' in svg and '1 error, 1 pending' in svg
    assert '6.00 s' in svg and 'Not recorded' in svg
    assert '<script>' not in html and '&lt;script&gt;literal&lt;/script&gt;' in html
    assert 'private-key' not in html and '[redacted]' in html
    assert '````text' in md  # Output fences cannot break out of the code block.
    assert 'Exact system instructions' in md
    assert 'nmap' in html and 'Observed tool' in html
    assert 'src="http' not in html and 'href="http' not in html
    assert 'experiment-charts.svg' in md


def test_reported_usage_does_not_invent_missing_tokens():
    assert run_report.reported_tokens({}) is None
    assert run_report.reported_tokens(dict(provider_usage=[None,{}])) is None
    assert run_report.reported_tokens(dict(provider_usage=[dict(total_tokens=0)]))==0
    assert run_report.reported_tokens(dict(provider_usage=[dict(input_tokens=4,output_tokens=3),dict(total_tokens=2)]))==9


def test_report_includes_judge_verdict_errors_and_escaped_reasons():
    report=fixture_report()
    report['run_configuration']['judge']={'enabled':True,'model':{'name':'review-model','provider':'openai'}}
    reviewed=report['evaluation']['attempts'][1]
    reviewed.update(judge_enabled=True,judge_passed=False,judge_score=0,judge_seconds=2,judge_calls=2,
                    judge_prompt_tokens=40,judge_output_tokens=15,judge_execution_trace_reviewed=True,judge_reason='Unsupported completion claim <script>unsafe</script>')
    report['evaluation']['attempts'][2].update(judge_enabled=True,judge_error='Judge endpoint unavailable',judge_evidence_warning='No execution logs collected')
    docs=run_report.render(report,recorded_at='fixed')
    md,html=docs['experiment-summary.md'].replace('\\',''),docs['experiment-summary.html']
    assert 'Judge agent reviews' in md and 'review-model' in md
    assert '| trial-1 | FAIL | 0 | 2.00 s | 2 | 40 | 15 | Yes |' in md
    assert '| trial-2 | ERROR |' in md and 'Judge endpoint unavailable' in md
    assert 'Judge evidence limitation' in md and 'No execution logs collected' in md
    assert '<script>unsafe</script>' not in html and '&lt;script&gt;unsafe&lt;/script&gt;' in html


def test_downloads_capture_prompts_actual_tools_and_never_probe_guests(lab,tmp_path):
    config,output,agent,_=lab
    workflow.run(config,output,agent=agent)
    folder=next((output/'evaluation/trials').glob('*/attempt-*'))
    events=folder/'guest-output/events.jsonl'
    events.parent.mkdir(exist_ok=True)
    events.write_text('\n'.join(json.dumps(e) for e in [
        dict(type='tool_call',tool='curl',args={'url':'secret-output-must-stay-private'}),
        dict(type='tool_result',tool='curl',result='secret-output-must-stay-private'),
    ])+'\n{partial')
    calls=len(agent.calls)
    snapshot=service.summary_documents(output)
    assert 'Some tool events are unreadable' in snapshot['experiment-summary.md']
    assert 'curl' in snapshot['experiment-summary.html']
    assert 'secret-output-must-stay-private' not in snapshot['experiment-summary.html']
    for artifact_id,filename in [('report-markdown','experiment-summary.md'),('report-html','experiment-summary.html'),('report-charts','experiment-charts.svg')]:
        with run_artifacts.download(output,artifact_id) as (stream,mime,name):
            assert name==filename and len(stream.read())>100
    with run_artifacts.download(output,'run-bundle') as (stream,_,_):
        with zipfile.ZipFile(stream) as archive:
            assert all(name in archive.namelist() for name in snapshot)
    service.export_results(output,tmp_path/'export')
    assert (tmp_path/'export/experiment-summary.md').is_file()
    assert len(agent.calls)==calls


def test_failed_before_evaluation_still_has_shareable_report(lab):
    config,output,agent,_=lab
    agent.fail=True
    with pytest.raises(ValueError):
        workflow.run(config,output,agent=agent)
    calls=len(agent.calls)
    docs=service.summary_documents(output)
    assert 'Task prompts' in docs['experiment-summary.md']
    assert 'Evaluation has not started' in docs['experiment-summary.md']
    assert '| Status | failed |' in docs['experiment-summary.md']
    assert not (output/'evaluation').exists()
    assert len(agent.calls)==calls
