"""Fail early on saved-input and endpoint errors; probes contain no trial data."""
from copy import copy
from pathlib import Path
import ipaddress
import json
import os
import time

from cyber_agent_flow_eval import integration as ev
from .diagnostics import clean
from .workflow_progress import checkpoint, now


class InputPreflightError(ValueError):
    pass


def check_judge(runtime, output=None):
    from cyber_agent_flow_eval.judge import resolve_judge, _completion, _json_action
    config = resolve_judge(runtime.get('judge'), runtime['model'])
    if not config['enabled']:
        return {'status':'skip', 'message':'Judge test skipped: judge disabled; exact checks will be used'}
    variable = config['model'].get('api_key_env')
    if variable and not os.environ.get(variable):
        raise ValueError('Judge host credential ' + variable + ' is missing. Set it in the host environment or configure host_environment_file in web.yaml, then restart; the participant VM key is separate.')
    probe = dict(config, max_tokens=min(config['max_tokens'],512))
    messages = [{'role':'system','content':'Synthetic preflight only; no experiment data. Return exactly one JSON action, without calling tools.'},
                {'role':'user','content':'Return {"action":"verdict","passed":false,"score":0,"reason":"synthetic preflight","evidence":[]}.'}]
    timeout = config['timeout_seconds']
    from cyber_agent_flow_eval.judge_stream import EventLog
    log = EventLog(output/'judge-preflight-events.jsonl') if output is not None else None
    if log: log.emit('status', text=f'Test LLM Judge: checking connectivity, authentication and JSON response format; {"no timeout (may wait indefinitely)" if not timeout else "timeout "+str(timeout)+"s"}. Synthetic startup test only, not an experiment verdict. A sample passed=false verdict is expected.')
    started = time.monotonic()
    try:
        try:
            text, _ = _completion(probe, messages, timeout, **({'on_delta':log.delta} if log else {}))
        finally:
            if log: log.flush()
    except ValueError as exc:
        if log: log.emit('error', text=str(exc))
        if timeout and time.monotonic()-started >= timeout * .95:
            raise ValueError(f'Judge synthetic generation probe exceeded {timeout}s; check judge server load and generation settings before deployment. The full review budget is unchanged.') from None
        raise
    action = _json_action(text)
    if (action.get('action')!='verdict' or type(action.get('passed')) is not bool
            or type(action.get('score')) not in (int,float) or not 0 <= action['score'] <= 1
            or not isinstance(action.get('reason'),str) or not isinstance(action.get('evidence'),list)):
        raise ValueError('Judge did not return a usable JSON verdict for the synthetic probe; check model/provider compatibility and token limits')
    if log: log.emit('status', text='Judge test passed: the judge is working. The synthetic passed=false verdict is expected and does not mean the experiment failed.')
    return {'message':'Judge test passed: authenticated from the orchestrator host and returned usable JSON. The synthetic passed=false verdict is expected; no experiment evidence was judged.'}


def check_inputs(workflow):
    runtime, cfg = workflow.journal['runtime'], workflow.journal['workflow']
    execution = runtime['execution']
    for key in ('max_turns','context_window'):
        ev.positive(execution.get(key,8192),key)
    for key in ('wall_seconds','tool_timeout'):
        value = execution.get(key,60)
        if type(value) is not int or value < 0:
            raise ValueError(key+' must be a nonnegative integer; 0 means no timeout')
    policy = execution['network_policy']
    allowed = [ipaddress.ip_network(value,strict=False) for value in policy['allow']]
    raw_denied = [ipaddress.ip_network(value,strict=False) for value in policy['disallow']]
    denied = [network for version in (4,6) for network in ipaddress.collapse_addresses([n for n in raw_denied if n.version==version])]
    if allowed and all(any(a.version==d.version and a.subnet_of(d) for d in denied) for a in allowed):
        raise ValueError('All allowed target networks are excluded by the saved network policy')
    selected = workflow.journal.get('scenario_experiment',{})
    tasks = selected.get('evaluation_tasks')
    if tasks is not None:
        from .evaluation_tasks import validate_tasks
        tasks = validate_tasks(tasks)
        if any(t.get('verification_mode') in ('judge','both') or t.get('verifier',{}).get('type')=='rubric' for t in tasks) and not runtime.get('judge',{}).get('enabled'):
            raise ValueError('Saved tasks require a Judge LLM, but it is disabled')
    for condition in runtime['conditions'] if 'scenarioforge' in cfg else []:
        assets = cfg.get('collect',{}).get(condition['id'],{})
        if 'catalog' not in assets:
            path = Path(condition['catalog'])
            catalog = json.loads(path.read_text())
            names = {tool['name'] for tool in catalog['tools']}
            missing = set(condition['tools']) - names
            if missing: raise ValueError('Condition '+condition['id']+' selects tools missing from its catalog: '+', '.join(sorted(missing)))
        for path in condition.get('guidance_files',[]):
            Path(path).read_text()
    return {'message':'Saved tasks, verification modes, limits, network scope, local catalogs and guidance validated'}


def run(workflow):
    runtime, cfg = workflow.journal['runtime'], workflow.journal['workflow']
    audit = {'started_at':now(),'status':'running','checks':[]}
    workflow.journal['input_preflight'] = audit
    stage = workflow.journal['progress_steps']['preflight']
    stage['input_checks'] = audit['checks']
    def persist():
        ev.write_json(workflow.output/'input-preflight.json',audit)
        workflow.save()
    def check(identifier,label,operation):
        row = {'id':identifier,'label':label,'status':'running','message':'Checking…'}
        audit['checks'].append(row)
        checkpoint(workflow,label+' · checking')
        started = time.monotonic()
        try:
            result = operation() or {}
            row.update(status=result.get('status','pass'), message=clean(result.get('message','Verified')))
            return result
        except (ValueError,OSError,KeyError,TypeError) as exc:
            message = str(exc)
            if 'Unknown guest operation: preflight' in message:
                message = 'VM preflight requires the updated cyber-agent-flow-eval package on the orchestrator host. Update that checkout, sync and restart the orchestrator.'
            row.update(status='fail',message=clean(message))
            return None
        finally:
            row['seconds'] = round(time.monotonic()-started,2)
            checkpoint(workflow,label+' · '+row['status']+': '+row['message'])
            persist()
    check('inputs','Saved experiment inputs',lambda:check_inputs(workflow))
    check('judge','Test LLM Judge',lambda:check_judge(runtime, workflow.output))
    remote = copy(workflow.agent)
    remote.script = Path(__file__).with_name('input_probe_guest.py').read_text()
    participant = runtime['backend']['participant_vmid']
    def engine_check():
        result = workflow.agent.call(participant,'probe',engine=runtime['engine'],user=runtime['backend']['user'],timeout=60)
        dependencies = result.get('runtime',{}).get('dependencies')
        if isinstance(dependencies,dict):
            required = ['mcp','requests','PyYAML','psutil'] + (['ollama'] if runtime['model']['provider']=='ollama_direct' else [])
            missing = [name for name in required if not dependencies.get(name)]
            if missing: raise ValueError('Participant Python environment lacks required packages: '+', '.join(missing))
        result['message'] = 'CAF files, evaluation controls, guest account, systemd and Python dependencies checked'
        return result
    engine = check('engine','Participant CAF runtime',engine_check)
    if engine is not None:
        check('participant-model','Participant model, guest credentials and connectivity',lambda:remote.call(participant,'input_check',role='participant',
            user=runtime['backend']['user'],engine=runtime['engine'],model=runtime['model'],tool_timeout=runtime['execution'].get('tool_timeout',60),tools=any(c['tools'] for c in runtime['conditions']),
            environment_file=runtime['backend'].get('environment_file'),timeout=75))
    sf = cfg.get('scenarioforge',{})
    if sf and runtime['backend'].get('app_vmid'):
        info = check('scenarioforge','ScenarioForge application and configuration',lambda:remote.call(runtime['backend']['app_vmid'],'input_check',role='scenarioforge',
            root=sf.get('repo',cfg.get('monitoring',{}).get('scenarioforge_path','/opt/scenarioforge')),
            python=sf.get('python','/opt/scenarioforge/.venv/bin/python'),user=sf.get('user','scenarioforge'),timeout=30))
        if info is not None and sf.get('mode')=='execute':
            check('core-connection','ScenarioForge to CORE SSH and gRPC',lambda:remote.call(runtime['backend']['app_vmid'],'input_check',role='scenarioforge-connectivity',
                root=sf.get('repo',cfg.get('monitoring',{}).get('scenarioforge_path','/opt/scenarioforge')),
                python=sf.get('python','/opt/scenarioforge/.venv/bin/python'),user=sf.get('user','scenarioforge'),timeout=60))
        core = cfg.get('monitoring',{}).get('core_vmid')
        if core and info is not None:
            check('core','CORE upload paths and HITL interface',lambda:remote.call(core,'input_check',role='core',user=info['core']['user'],core=info['core'],timeout=30))
    if execution := runtime.get('execution'):
        if execution['wall_seconds'] and (not execution.get('tool_timeout',60) or execution.get('tool_timeout',60) > execution['wall_seconds']):
            audit['checks'].append({'id':'budgets','label':'Time budgets','status':'warning','message':'Tool timeout exceeds the trial budget; the trial deadline may stop an active tool.'})
    audit['checks'].append({'id':'scenario-readiness','label':'Scenario target readiness','status':'skip',
                            'message':'Target services, flags and reachability are checked after deployment; preflight cannot guarantee the agent will solve the task.'})
    failures = [row for row in audit['checks'] if row['status']=='fail']
    audit.update(status='failed' if failures else 'passed', ended_at=now())
    persist()
    if failures:
        raise InputPreflightError('Input preflight failed before deployment: '+'; '.join(row['label']+': '+row['message'] for row in failures))
    checkpoint(workflow,'Input preflight passed; scenario preparation may proceed')
    return audit
