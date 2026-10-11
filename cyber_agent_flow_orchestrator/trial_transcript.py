"""Bounded transcript views from owner-scoped host files; never poll guests."""
import json
import math
from cyber_agent_flow_eval import reporting
from .diagnostics import clean

TAIL_BYTES = 96 * 1024
MAX_EVENTS = 80


def build(evaluation, row, active):
    result = {'events': [], 'live': bool(active and row['status'] == 'running'),
              'truncated': False, 'bytes': 0}
    try:
        attempt = reporting.within(evaluation, evaluation / row['attempt_path'])
        final = reporting.within(evaluation, attempt / 'guest-output/events.jsonl')
        path = final if final.is_file() else reporting.within(evaluation, attempt / 'live-events.jsonl')
        if not path.is_file():
            return result
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - TAIL_BYTES)
            stream.seek(start)
            content = stream.read(TAIL_BYTES)
        result.update(bytes=size, truncated=bool(start), source='collected' if path == final else 'live')
        lines = content.split(b'\n')
        if start:
            lines = lines[1:]  # A partial first record is never a valid event.
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue  # In-flight trailing records become readable next refresh.
            if not isinstance(event, dict):
                continue
            elapsed = event.get('_eval_elapsed_seconds')
            elapsed = elapsed if type(elapsed) in (float, int) and math.isfinite(elapsed) else None
            fields = {key:value for key,value in event.items() if key not in ('type', '_eval_elapsed_seconds')}
            text = json.dumps(fields, indent=2, ensure_ascii=False)
            result['events'].append({'type': clean(event.get('type', 'event'), 80),
                                     'elapsed_seconds': elapsed, 'text': clean(text, 8000),
                                     'truncated': len(text) > 8000})
        result['truncated'] |= len(result['events']) > MAX_EVENTS
        result['events'] = result['events'][-MAX_EVENTS:]
    except (OSError, ValueError, KeyError):
        result['warning'] = 'Transcript unavailable; retry or inspect the run bundle.'
    return result
