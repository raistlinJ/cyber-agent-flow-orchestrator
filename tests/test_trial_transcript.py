import json
from cyber_agent_flow_orchestrator import trial_transcript


def test_partial_live_records_redaction_and_collected_precedence(tmp_path):
    attempt = tmp_path / 'trials/trial-000001/attempt-0001'
    attempt.mkdir(parents=True)
    row = {'attempt_path': str(attempt.relative_to(tmp_path)), 'status': 'running'}
    event = {'type': 'tool_call', 'args': {'api_key': 'private-key'}, '_eval_elapsed_seconds': 2}
    live = attempt / 'live-events.jsonl'
    live.write_text(json.dumps(event) + '\n' + '{"type":"response"')
    result = trial_transcript.build(tmp_path, row, True)
    assert result['live'] and result['source'] == 'live'
    assert len(result['events']) == 1
    assert 'private-key' not in str(result)
    assert '[redacted]' in result['events'][0]['text']
    collected = attempt / 'guest-output'
    collected.mkdir()
    (collected / 'events.jsonl').write_text(json.dumps({'type': 'response', 'text': 'Solved'}) + '\n')
    result = trial_transcript.build(tmp_path, dict(row, status='completed'), False)
    assert not result['live'] and result['source'] == 'collected'
    assert result['events'][0]['type'] == 'response'
    assert 'Solved' in result['events'][0]['text']


def test_transcript_bounded_and_scope_checked(tmp_path):
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    (attempt / 'live-events.jsonl').write_text(''.join(json.dumps({'type': 'response', 'text': 'x' * 2000}) + '\n' for _ in range(200)))
    row = {'attempt_path': 'attempt', 'status': 'running'}
    result = trial_transcript.build(tmp_path, row, True)
    assert result['truncated']
    assert 0 < len(result['events']) <= trial_transcript.MAX_EVENTS
    assert trial_transcript.build(tmp_path, dict(row, attempt_path='../outside'), True)['warning']
    (attempt / 'live-events.jsonl').unlink()
    (attempt / 'live-events.jsonl').symlink_to('/etc/passwd')
    assert trial_transcript.build(tmp_path, row, True)['warning']


def test_judge_stream_includes_progress_reviews_and_final_review(tmp_path):
    attempt=tmp_path/'trials/attempt'
    progress=attempt/'progress-checks/turn-0001'
    progress.mkdir(parents=True)
    (progress/'judge-events.jsonl').write_text(json.dumps({'type':'response','text':'Intermediate','purpose':'progress'})+'\n')
    (attempt/'judge-events.jsonl').write_text(''.join(json.dumps({'type':'response','text':t,'purpose':'final','turn':1})+'\n' for t in ['Final ','verdict']))
    row={'attempt_path':'trials/attempt','status':'running'}
    result=trial_transcript.build(tmp_path,row,True,judge=True)
    assert result['live'] and len(result['events'])==2
    assert 'Intermediate' in result['events'][0]['text']
    assert 'Final verdict' in result['events'][1]['text']
