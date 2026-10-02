"""Bounded failure diagnostics from collected host files; never contact guests."""
import json
from cyber_agent_flow_eval import reporting
from .diagnostics import clean


def trial_failures(rows):
    """Explain unsuccessful execution even when a worker omitted errors."""
    failures = []
    for row in rows:
        state = row.get('status')
        if state in ('completed', 'running'):
            continue
        errors = row.get('errors') or []
        if isinstance(errors, str):
            errors = [errors]
        failures.append(dict(trial_id=row['trial_id'], attempt=row['attempt'],
            condition_id=row.get('condition_id'), task_id=row.get('task_id'), status=state,
            errors=[clean(error) for error in errors[:10]] or
                (['CAF requested an interactive decision; unattended evaluation stopped without a final answer.'] if state == 'interaction_required' else
                 [f"Trial ended with status {state}; the worker did not record an error message. Inspect collected diagnostics."]),
            attempt_path=row.get('attempt_path')))
    return failures


def collected_failures(root, rows):
    failures = []
    for row in rows:
        if row.get('status') in ('completed', 'running'):
            continue
        item = dict(trial_id=row['trial_id'], attempt=row['attempt'], status=row.get('status'),
                    errors=trial_failures([row])[0]['errors'], model_errors=[], logs=[], notes=[], worker_results=[], transport=None, interactions=[])
        failures.append(item)
        if len(failures) > 20:
            failures.pop()
            break
        try:
            folder = reporting.within(root, root / row['attempt_path'])
            def saved(path):
                path = reporting.within(folder, path)
                if not path.is_file():
                    return None
                with path.open('rb') as stream:
                    content = stream.read(2 * 1024 * 1024 + 1)
                if len(content) > 2 * 1024 * 1024:
                    item['notes'].append('A saved diagnostic record was too large to preview.')
                    return None
                try:
                    record = json.loads(content)
                    return record if isinstance(record, dict) else None
                except (ValueError, UnicodeError):
                    item['notes'].append('A saved diagnostic record could not be decoded.')
                    return None
            transport = saved(folder / 'transport.json')
            if transport:
                service = transport.get('service_status') or {}
                item['transport'] = dict(phase=clean(transport.get('phase', 'unknown')),
                    unit=clean(transport.get('unit', 'unknown')), stopped=transport.get('stopped'),
                    collected=transport.get('collected'),
                    service={key: clean(service[key]) for key in ('LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'ExecMainCode') if key in service})
            for base in (folder / 'guest-output', folder):
                # The remote backend collects into guest-output; local workers write in folder.
                events = reporting.within(folder, base / 'events.jsonl')
                if events.is_file():
                    with events.open('rb') as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 262144))
                        lines = stream.read(262144).splitlines()
                    explanations = {
                        'tool_timeout_decision': 'A tool reached a timeout checkpoint and requested a wait, background or kill decision.',
                        'dangerous_tool_approval': 'CAF requested approval before executing a dangerous tool.',
                        'post_tool_reply_decision': 'The model returned an empty final reply after tool calls and one automatic retry; CAF requested retry or cancel.'}
                    for line in lines:
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(event, dict) or event.get('type') not in explanations:
                            continue
                        detail = dict(type=event['type'], explanation=explanations[event['type']],
                                      path=str(events.relative_to(root)))
                        if isinstance(event.get('tool'), str):
                            detail['tool'] = clean(event['tool'])
                        for key in ('timeout_seconds', 'elapsed_seconds'):
                            if type(event.get(key)) in (int, float):
                                detail[key] = event[key]
                        item['interactions'].append(detail)
                        item['interactions'] = item['interactions'][-10:]
                result = saved(base / 'result.json')
                if result:
                    errors = result.get('errors') or []
                    if isinstance(errors, str):
                        errors = [errors]
                    item['worker_results'].append(dict(path=str((base / 'result.json').relative_to(root)),
                        status=clean(result.get('status', 'unknown')), errors=[clean(error) for error in errors[:10]]))
                path = reporting.within(folder, base / 'worker.log')
                if path.is_file():
                    with path.open('rb') as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 65536))
                        lines = stream.read(65536).decode(errors='replace').splitlines()[-80:]
                    item['logs'].append(dict(path=str(path.relative_to(root)),
                        text='\n'.join(clean(line) for line in lines)[-12000:]))
                calls = reporting.within(folder, base / 'model_calls')
                if not calls.is_dir():
                    continue
                for candidate in sorted(calls.glob('call-*.json'))[-10:]:
                    path = reporting.within(folder, candidate)
                    with path.open('rb') as stream:
                        content = stream.read(2 * 1024 * 1024 + 1)
                    if len(content) > 2 * 1024 * 1024:
                        item['notes'].append('A model-call record was too large to preview.')
                        continue
                    try:
                        record = json.loads(content)
                    except (ValueError, UnicodeError):
                        item['notes'].append('A model-call record could not be decoded.')
                        continue
                    # Do not send prompts, request headers, responses or model-call bodies to the UI.
                    if isinstance(record, dict) and record.get('error'):
                        item['model_errors'].append(dict(path=str(path.relative_to(root)), error=clean(record['error'])))
        except (OSError, ValueError, KeyError, TypeError):
            item['notes'].append('Some collected diagnostics could not be read safely.')
        if item['logs'] and not any(log['text'].strip() for log in item['logs']):
            item['notes'].append('The collected worker log is empty. Check the saved worker result and service exit state; an empty log does not establish the cause.')
        if not item['worker_results']:
            item['notes'].append('No saved worker result.json was collected.')
        if not item['logs'] and not item['model_errors']:
            item['notes'].append('No collected worker log or model-call error is available on the host. '
                                 'The failure may have occurred before collection; this view does not contact the VM.')
    return failures
