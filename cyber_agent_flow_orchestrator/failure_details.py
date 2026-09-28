"""Bounded failure diagnostics from collected host files; never contact guests."""
import json
from cyber_agent_flow_eval import reporting
from .diagnostics import clean


def collected_failures(root, rows):
    failures = []
    for row in rows:
        if row.get('status') in ('completed', 'running'):
            continue
        item = dict(trial_id=row['trial_id'], attempt=row['attempt'], model_errors=[], logs=[], notes=[])
        failures.append(item)
        if len(failures) > 20:
            failures.pop()
            break
        try:
            folder = reporting.within(root, root / row['attempt_path'])
            for base in (folder / 'guest-output', folder):
                # The remote backend collects into guest-output; local workers write in folder.
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
        if not item['logs'] and not item['model_errors']:
            item['notes'].append('No collected worker log or model-call error is available on the host. '
                                 'The failure may have occurred before collection; this view does not contact the VM.')
    return failures
