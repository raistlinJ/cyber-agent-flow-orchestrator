"""Bounded read-only transcript batches from the owner's host journal."""
import json
import math
import os
import re
from .run_artifacts import open_saved
from . import service


def safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: '[redacted]' if re.fullmatch(r'(?i)password|passwd|api[_-]?key|authorization|cookie|ticket', key)
                else safe(item) for key,item in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[redacted]', value)
        value = re.sub(r'(?i)((?:api[_-]?key|password|passwd|authorization|cookie|ticket)["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;}]+)', r'\1[redacted]', value)
        return ''.join(char for char in value if char in '\n\t' or ord(char) >= 32)
    return value


def read(root, after=0):
    if type(after) is not int or after < 0:
        raise ValueError('Invalid transcript cursor')
    events, cursor = [], after
    try:
        with open_saved(root, 'evaluation/live-transcript.jsonl') as stream:
            size = os.fstat(stream.fileno()).st_size
            if after > size:
                raise ValueError('Transcript cursor exceeds journal size')
            stream.seek(after)
            data = stream.read(64 * 1024)
    except FileNotFoundError:
        data = b''
    end = data.rfind(b'\n') + 1
    for line in data[:end].splitlines(keepends=True):
        cursor += len(line)
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError('Invalid transcript record')
            events.append(dict(safe(record), id=cursor))
        except (ValueError, UnicodeError):
            events.append(dict(id=cursor, event={'type':'status','message':'An unreadable transcript entry was skipped.'}))
    return dict(events=events, cursor=cursor, more=bool(end and len(data)==64*1024))


def state(root):
    record = service.status(root)
    progress = record.get('sample_progress') or {}
    return dict(status=record['recorded_status'], active=bool(record['coordinator_active'] or progress.get('active')),
                phase=progress.get('phase'), current_trial=progress.get('current_trial'),
                vmid=(record.get('saved_settings') or {}).get('participant_vmid'), message=record.get('message'))
