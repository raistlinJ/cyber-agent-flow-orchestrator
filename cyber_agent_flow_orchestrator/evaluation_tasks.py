"""Validation for editable ScenarioForge evaluation task definitions."""
import json
import re
from copy import deepcopy
from .samples import SampleRequestError

MAX_TASK_BYTES = 64 * 1024
FIELDS = {'id', 'family', 'split', 'prompt', 'flag_nodes', 'verifier', 'required_checks',
          'discovery', 'starting_facts', 'discoverable_facts', 'objective_requires', 'progressive_hints'}


def validate_tasks(value):
    try:
        size = len(json.dumps(value, allow_nan=False).encode())
    except (ValueError, TypeError):
        raise SampleRequestError('Tasks must contain valid JSON values') from None
    if size > MAX_TASK_BYTES or not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise SampleRequestError('Supply 1–32 tasks, up to 64 KiB of JSON')
    seen = set()
    for index, task in enumerate(value, 1):
        prefix = f'Task {index}: '
        def fail(message):
            raise SampleRequestError(prefix + message)
        if not isinstance(task, dict) or set(task) - FIELDS:
            fail('unknown task fields')
        hints = task.get('progressive_hints', [])
        if not isinstance(hints, list) or len(hints) > 16 or any(not isinstance(h, str) or not h.strip() or len(h) > 1500 for h in hints):
            fail('progressive_hints must be up to 16 nonempty strings of at most 1500 characters')
        task_id = task.get('id')
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', task_id):
            fail('use an ID of 1–80 letters, numbers, dots, underscores or hyphens')
        if task_id in seen:
            fail('task IDs must be unique')
        seen.add(task_id)
        if not isinstance(task.get('family'), str) or not task['family'].strip():
            fail('family is required')
        if task.get('split', 'development') not in ('development', 'validation', 'test'):
            fail('invalid split')
        if 'prompt' in task and (not isinstance(task['prompt'], str) or not task['prompt'].strip()):
            fail('prompt must be nonempty')
        checks = task.get('required_checks')
        if not isinstance(checks, list) or not checks or any(not isinstance(c, str) or not c.strip() for c in checks):
            fail('at least one required readiness check is required')
        if 'flag_nodes' in task:
            refs = task['flag_nodes']
            if 'verifier' in task or not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
                fail('provide distinct flag node IDs without an explicit verifier')
        else:
            if not task.get('prompt'):
                fail('prompt is required')
            verifier = task.get('verifier')
            if not isinstance(verifier, dict) or set(verifier) != {'type', 'expected'} or verifier['type'] not in ('json_equals', 'contains_all'):
                fail('success criteria must use json_equals or contains_all with an expected value')
            if verifier['type'] == 'contains_all':
                expected = verifier['expected']
                if not isinstance(expected, list) or not expected or any(not isinstance(v, str) or not v for v in expected):
                    fail('contains_all expects a nonempty JSON array of strings')
        if type(task.get('discovery', False)) is not bool:
            fail('discovery must be true or false')
        if not task.get('discovery') and set(task) & {'starting_facts','discoverable_facts','objective_requires'}:
            fail('fact declarations require discovery: true')
        # ScenarioForge validates graph references and discovery facts against the deployed scenario.
    return deepcopy(value)
