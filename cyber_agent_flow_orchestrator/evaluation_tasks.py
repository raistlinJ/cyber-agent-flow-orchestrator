"""Validation for editable ScenarioForge evaluation task definitions."""
import json
import re
from copy import deepcopy
from .samples import SampleRequestError

MAX_TASK_BYTES = 64 * 1024
FIELDS = {'id', 'family', 'split', 'prompt', 'flag_nodes', 'verifier', 'required_checks',
          'discovery', 'starting_facts', 'discoverable_facts', 'objective_requires', 'progressive_hints', 'rubric', 'verification_mode', 'challenge_plan'}


def _answer_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _answer_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _answer_strings(item)
    elif value is not None:
        yield json.dumps(value)


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
        from cyber_agent_flow_eval.rubric import MODES, validate_rubric
        mode = task.get('verification_mode', 'exact')
        if mode not in MODES:
            fail('verification_mode must be exact, judge or both')
        if 'rubric' in task:
            try:
                validate_rubric(task['rubric'])
            except ValueError as exc:
                fail(str(exc))
        if 'challenge_plan' in task:
            from cyber_agent_flow_eval.challenge_plan import validate_plan
            try:
                validate_plan(task['challenge_plan'], task['rubric'])
            except (ValueError, KeyError) as exc:
                fail('Invalid challenge plan: ' + str(exc))
        if mode in {'judge', 'both'} and 'rubric' not in task:
            fail('judge/both mode requires a rubric')
        if mode == 'judge':
            if not task.get('prompt') or 'verifier' in task or 'flag_nodes' in task:
                fail('judge-only tasks require a prompt and rubric, without an exact verifier or flag_nodes')
        elif 'flag_nodes' in task:
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
        answers = list(_answer_strings(task.get('verifier', {}).get('expected')))
        if any(answer and answer in hint for answer in answers for hint in hints):
            fail('a progressive hint contains a verifier answer; provide guidance instead of the solution')
        if type(task.get('discovery', False)) is not bool:
            fail('discovery must be true or false')
        if not task.get('discovery') and set(task) & {'starting_facts','discoverable_facts','objective_requires'}:
            fail('fact declarations require discovery: true')
        # ScenarioForge validates graph references and discovery facts against the deployed scenario.
    return deepcopy(value)
