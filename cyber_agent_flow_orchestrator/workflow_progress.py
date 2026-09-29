"""Saved workflow telemetry. Status reads never issue guest operations."""
from datetime import datetime, timezone
from contextlib import contextmanager
from .diagnostics import clean
from .sample_progress import elapsed


def now():
    return datetime.now(timezone.utc).isoformat()


def definitions(journal):
    cfg, backend = journal['workflow'], journal['runtime']['backend']
    app, participant = backend.get('app_vmid'), backend.get('participant_vmid')
    core = cfg.get('monitoring', {}).get('core_vmid')
    steps = [(f"prepare-{c['id']}", 'Prepare scenario inputs', c['vmid'],
              'Prepare the XML, topology, website assets and task definitions.') for c in cfg.get('prepare', [])]
    if journal.get('scenario_experiment'):
        steps.insert(0, ('snapshot', 'Verify saved scenario XML', app, 'Check that the ScenarioForge snapshot still matches the XML frozen for this experiment.'))
    if cfg['scenarioforge']['mode'] == 'execute':
        steps.append(('deploy', 'Deploy and check scenario', app,
                      'ScenarioForge executes the XML on CORE, checks readiness and exports evaluation tasks. Awaiting the command outcome; individual CORE operations are not streamed.'))
    steps += [('fetch', 'Download evaluation package', app, 'Transfer the exported suite to the orchestrator and verify its package hash.'),
              ('readiness', 'Validate readiness', None, 'Validate the exported readiness evidence and its age before evaluation.'),
              ('reproduction', 'Capture scenario bundle', app, 'Collect XML and available reproduction files from ScenarioForge and CORE; validate the saved bundle.')]
    steps += [(f"artifact-{c['id']}", 'Prepare evaluation artifacts', c['vmid'], 'Run the configured artifact preparation command.') for c in cfg.get('artifacts', [])]
    steps += [('freeze', 'Freeze run configuration', participant, 'Save the exact tool catalogs, guidance and runtime settings on the orchestrator.'),
              ('import', 'Build evaluation plan', None, 'Import tasks and verifiers; combine tasks, tool conditions and repetitions.'),
              ('evaluate', 'Run and score trials', participant, 'Transfer trial inputs, run the CAF worker, collect outputs and score each trial on the orchestrator.')]
    return steps, app, core, participant


@contextmanager
def step(workflow, key):
    records = workflow.journal.setdefault('progress_steps', {})
    record = records.setdefault(key, {})
    record.update(status='running', started_at=now(), ended_at=None, error=None)
    workflow.journal['current_step'] = key
    workflow.journal.setdefault('events', []).append(dict(at=record['started_at'], kind='stage', message=key+' started'))
    workflow.journal['events'] = workflow.journal['events'][-100:]
    workflow.save()
    try:
        yield
    except BaseException as exc:
        record.update(status='failed', ended_at=now(), error=clean(str(exc)))
        workflow.save()
        raise
    else:
        record.update(status='completed', ended_at=now())
        workflow.journal.setdefault('events', []).append(dict(at=record['ended_at'], kind='stage', message=key+' completed'))
        workflow.journal['events'] = workflow.journal['events'][-100:]
        workflow.save()


@contextmanager
def observe_command(workflow, vmid):
    """Observe existing QGA polls without extra guest calls or exposing RPC payloads."""
    agent = workflow.agent
    original = getattr(agent, 'qm', None)
    if original is None:
        yield
        return
    def observed(args, **kwargs):
        result = original(args, **kwargs)
        workflow.journal['guest_observation'] = dict(vmid=vmid, at=now(), step=workflow.journal.get('current_step'),
            state='Guest operation returned' if result.get('exited') else 'Guest operation is still running',
            pid=result.get('pid'), exited=result.get('exited'), exitcode=result.get('exitcode'))
        try:
            workflow.save()
        except OSError:
            pass  # Optional observations must not interrupt command cleanup.
        return result
    agent.qm = observed
    try:
        yield
    finally:
        agent.qm = original


def build(journal, state, active):
    if 'scenarioforge' not in journal.get('workflow', {}):
        return None  # Historical participant-only samples use their existing trial view.
    definitions_, app, core, participant = definitions(journal)
    records = journal.get('progress_steps', {})
    current = journal.get('current_step')
    steps = []
    for key, label, vmid, description in definitions_:
        record = records.get(key, {})
        old = journal.get('stages', {}).get(key, {})
        status = record.get('status', old.get('status', 'pending'))
        attempt = (old.get('attempts') or [{}])[-1]
        if key == 'evaluate' and state in ('completed', 'completed_with_errors'):
            status = 'completed'
        if status == 'running' and not active:
            status = 'interrupted' if state == 'interrupted' else 'cancelled' if state == 'cancelled' else 'failed' if state == 'failed' else 'waiting'
        if key == current and state in ('cancelled', 'interrupted') and status in ('running', 'failed'):
            status = state
        started = record.get('started_at', attempt.get('started_at'))
        ended = record.get('ended_at')
        steps.append(dict(id=key, label=label, vmid=vmid, description=description, status=status,
                          started_at=started, ended_at=ended,
                          elapsed_seconds=elapsed(started, ended or (None if active else journal.get('ended_at', journal.get('updated_at')))),
                          active=status == 'running', error=clean(record.get('error') or old.get('error')) if record.get('error') or old.get('error') else None,
                          attempt_count=len(old.get('attempts', [])), log=old.get('log')))
    selected = next((s for s in steps if s['id'] == current), None)
    roles = [
        dict(role='scenarioforge', label='ScenarioForge', vmid=app, responsibility='Prepares and deploys the scenario; exports tasks and readiness evidence.'),
        dict(role='core', label='CORE', vmid=core, responsibility='Hosts the scenario network and services. Deployment is controlled through ScenarioForge’s configured CORE connection.'),
        dict(role='participant', label='CAF participant', vmid=participant, responsibility='Runs the agent and tools for each trial; returns outputs for scoring.'),
        dict(role='orchestrator', label='Orchestrator', vmid=None, responsibility='Coordinates stages, verifies transfers, scores trials and stores run artifacts.')]
    return dict(steps=steps, current_step=current, current=selected, roles=roles,
                completed_steps=sum(s['status']=='completed' for s in steps), total_steps=len(steps),
                observed_at=now(), guest_observation=journal.get('guest_observation'), transfer=journal.get('transfer'), readiness=journal.get('readiness_progress'))

@contextmanager
def observe_transfers(workflow):
    """Record existing download acknowledgements; never make additional RPCs."""
    import base64
    from pathlib import PurePosixPath
    agent = workflow.agent
    original_get, original_call = agent.get, agent.call
    def persist():
        try:
            workflow.save()
        except OSError:
            pass
    def get(vmid, path):
        transfer = dict(vmid=vmid, step=workflow.journal.get('current_step'), file=PurePosixPath(path).name, received_bytes=0,
                        total_bytes=None, verified=False, status='downloading', started_at=now())
        workflow.journal['transfer'] = transfer
        persist()
        def call(target, op, **data):
            result = original_call(target, op, **data)
            if target == vmid and data.get('path') == path:
                if op == 'stat':
                    transfer['total_bytes'] = result.get('size')
                elif op == 'read':
                    transfer['received_bytes'] += len(base64.b64decode(result.get('content', '')))
                transfer['updated_at'] = now()
                persist()
            return result
        agent.call = call
        try:
            content = original_get(vmid, path)
            transfer.update(received_bytes=len(content), total_bytes=len(content),
                            verified=True, status='completed', updated_at=now())
            persist()
            return content
        except BaseException:
            transfer.update(status='failed', updated_at=now())
            persist()
            raise
        finally:
            agent.call = original_call
    agent.get = get
    try:
        yield
    finally:
        agent.get, agent.call = original_get, original_call
