"""Bounded transcript views from owner-scoped host files; never poll guests."""
import json
import math
from cyber_agent_flow_eval import reporting
from .diagnostics import clean

TAIL_BYTES = 96 * 1024
MAX_EVENTS = 80


def build(evaluation, row, active, *, judge=False):
    result = {'events': [], 'live': bool(active and row['status'] == 'running'),
              'truncated': False, 'bytes': 0}
    try:
        attempt = reporting.within(evaluation, evaluation / row['attempt_path'])
        if judge:
            paths = sorted(attempt.glob('progress-checks/turn-*/judge-events.jsonl'))[-8:]
            paths.append(attempt/'judge-events.jsonl')
        else:
            final = reporting.within(evaluation, attempt / 'guest-output/events.jsonl')
            paths = [final if final.is_file() else attempt / 'live-events.jsonl']
        for path in paths:
            part = read_file(evaluation, path, result['live'])
            result['bytes'] += part['bytes']
            result['truncated'] |= part['truncated']
            result['events'].extend(part['events'])
            if part.get('updated_at'): result['updated_at'] = part['updated_at']
            if part.get('warning'): result['warning'] = part['warning']
        result['source'] = 'judge' if judge else 'collected' if paths[0].name=='events.jsonl' else 'live'
        result['truncated'] |= len(result['events']) > MAX_EVENTS
        result['events'] = result['events'][-MAX_EVENTS:]
    except (OSError, ValueError, KeyError):
        result['warning'] = 'Transcript unavailable; retry or inspect the run bundle.'
    return result


def read_file(root, path, active):
    result = {'events':[], 'live':bool(active), 'truncated':False, 'bytes':0}
    try:
        path = reporting.within(root, path)
        if not path.is_file(): return result
        with path.open('rb') as stream:
            stream.seek(0,2)
            size = stream.tell()
            start = max(0,size-TAIL_BYTES)
            stream.seek(start)
            content = stream.read(TAIL_BYTES)
        result.update(bytes=size,truncated=bool(start))
        lines = content.split(b'\n')[1:] if start else content.split(b'\n')
        records = []
        for line in lines:
            try: event = json.loads(line)
            except (ValueError,UnicodeDecodeError): continue
            if not isinstance(event,dict): continue
            # Join adjacent streamed fragments before rendering/redacting so
            # tokens form readable text rather than a card per chunk.
            if (records and event.get('type') in ('response','reasoning') and
                    all(event.get(k)==records[-1].get(k) for k in ('type','turn','purpose')) and
                    isinstance(event.get('text'),str) and isinstance(records[-1].get('text'),str)):
                records[-1]['text'] += event['text']
                records[-1]['at'] = event.get('at')
            else: records.append(event)
        if records and records[-1].get('at'): result['updated_at'] = records[-1]['at']
        for event in records:
            elapsed = event.get('_eval_elapsed_seconds')
            elapsed = elapsed if type(elapsed) in (float,int) and math.isfinite(elapsed) else None
            fields = {k:v for k,v in event.items() if k not in ('type','_eval_elapsed_seconds')}
            text = json.dumps(fields,indent=2,ensure_ascii=False)
            result['events'].append({'type':clean(event.get('type','event'),80),
                'elapsed_seconds':elapsed,'text':clean(text,8000),'truncated':len(text)>8000})
        result['truncated'] |= len(result['events']) > MAX_EVENTS
        result['events'] = result['events'][-MAX_EVENTS:]
    except (OSError,ValueError,KeyError):
        result['warning'] = 'Transcript unavailable; retry or inspect the run bundle.'
    return result
