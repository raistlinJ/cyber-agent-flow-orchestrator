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
                      'ScenarioForge executes the XML on CORE, checks readiness and exports evaluation tasks. Command output and coordinator heartbeats are collected during execution.'))
    steps += [('fetch', 'Download evaluation package', app, 'Transfer the exported suite to the orchestrator and verify its package hash.'),
              ('readiness', 'Validate readiness', None, 'Validate the exported readiness evidence and its age before evaluation.'),
              ('reproduction', 'Capture scenario bundle', app, 'Collect XML and available reproduction files from ScenarioForge and CORE; validate the saved bundle.')]
    steps += [(f"artifact-{c['id']}", 'Prepare evaluation artifacts', c['vmid'], 'Run the configured artifact preparation command.') for c in cfg.get('artifacts', [])]
    steps += [('freeze', 'Freeze run configuration', participant, 'Save the exact tool catalogs, guidance and runtime settings on the orchestrator.'),
              ('import', 'Build evaluation plan', None, 'Import tasks and verifiers; combine tasks, tool conditions and repetitions.'),
              ('evaluate', 'Run and score trials', participant, 'Transfer trial inputs, run the CAF worker, collect outputs and score each trial on the orchestrator.')]
    steps.insert(0, ('preflight', 'Check VM readiness and clean up leftover jobs', None, 'Reserve configured VMs, stop recorded jobs from inactive runs, and verify no active CAF services remain.'))
    return steps, app, core, participant


def checkpoint(workflow, message, kind='checkpoint'):
    key = workflow.journal.get('current_step')
    record = workflow.journal.setdefault('progress_steps', {}).setdefault(key, {})
    event = dict(at=now(), kind=kind, message=clean(message))
    record['operation'] = event['message']
    record.setdefault('events', []).append(event)
    record['events'] = record['events'][-60:]
    workflow.save()


@contextmanager
def step(workflow, key):
    records = workflow.journal.setdefault('progress_steps', {})
    previous = workflow.journal.get('current_step')
    parent = previous if previous != key and records.get(previous, {}).get('status') == 'running' else None
    record = records.setdefault(key, {})
    if parent:
        record['parent_step'] = parent
    record.update(status='running', started_at=now(), ended_at=None, error=None)
    workflow.journal['current_step'] = key
    workflow.journal.setdefault('events', []).append(dict(at=record['started_at'], kind='stage', message=key+' started'))
    workflow.journal['events'] = workflow.journal['events'][-100:]
    checkpoint(workflow, 'Started '+key)
    try:
        yield
    except BaseException as exc:
        record.update(status='failed', ended_at=now(), error=clean(str(exc)))
        checkpoint(workflow, 'Failed: '+str(exc), 'error')
        raise
    else:
        record.update(status='completed', ended_at=now())
        workflow.journal.setdefault('events', []).append(dict(at=record['ended_at'], kind='stage', message=key+' completed'))
        workflow.journal['events'] = workflow.journal['events'][-100:]
        checkpoint(workflow, 'Completed '+key)
    finally:
        if parent:
            records[parent].setdefault('events', []).extend(record.get('events', []))
            records[parent]['events'] = records[parent]['events'][-60:]
            records[parent]['last_command'] = key
            workflow.journal['current_step'] = parent
            workflow.save()


@contextmanager
def observe_command(workflow, vmid, attempt=None):
    """Collect bounded live output during coordinator polls, never status reads."""
    import base64
    import time
    agent = workflow.agent
    original = getattr(agent, 'qm', None)
    if original is None:
        yield
        return
    started = time.monotonic()
    last_read, last_heartbeat, offset = -float('inf'), started, 0
    collecting = False

    def event(kind, message):
        workflow.journal.setdefault('events', []).append(dict(at=now(), kind=kind, message=clean(message)))
        workflow.journal['events'] = workflow.journal['events'][-100:]
        record = workflow.journal.setdefault('progress_steps', {}).setdefault(workflow.journal.get('current_step'), {})
        record.setdefault('events', []).append(dict(at=now(), kind=kind, message=clean(message)))
        record['events'] = record['events'][-60:]

    def observed(args, **kwargs):
        nonlocal collecting, last_read, last_heartbeat, offset
        result = original(args, **kwargs)
        if collecting:
            return result
        clock = time.monotonic()
        workflow.journal['guest_observation'] = dict(vmid=vmid, at=now(), step=workflow.journal.get('current_step'),
            state='Guest operation returned' if result.get('exited') else 'Guest operation is still running',
            pid=result.get('pid'), exited=result.get('exited'), exitcode=result.get('exitcode'))
        workflow.journal.setdefault('progress_steps', {}).setdefault(workflow.journal.get('current_step'), {})['guest_observation'] = workflow.journal['guest_observation']
        if attempt and 'exitcode' not in attempt and clock - last_read >= 10:
            last_read = clock
            collecting = True
            try:
                block = agent.call(vmid, 'read', path=attempt['log_path'], offset=offset, length=8192, timeout=10)
                content = base64.b64decode(block['content'], validate=True)
                if content:
                    # Preserve the full partial log privately; console excerpts are redacted.
                    with (workflow.output / attempt['host_log']).open('ab') as stream:
                        stream.write(content)
                    offset += len(content)
                    text = content.decode('utf-8', errors='replace')
                    for line in text.splitlines()[-20:]:
                        event('command-output', f"VM {vmid} · {workflow.journal.get('current_step')}: {line}")
                    attempt['live_log_bytes'] = offset
                    attempt['last_output_at'] = now()
            except Exception as exc:
                # Log may not exist until systemd starts. Diagnostics never fail a command.
                if not attempt.get('live_log_warning'):
                    event('log-observation', f'VM {vmid}: live output unavailable ({type(exc).__name__}); retrying; full log collected at command end')
                    attempt['live_log_warning'] = True
            finally:
                collecting = False
        if clock - last_heartbeat >= 30 and not result.get('exited') and (not attempt or 'exitcode' not in attempt):
            last_heartbeat = clock
            age = round(clock - started)
            event('heartbeat', f"VM {vmid} · {workflow.journal.get('current_step')}: guest operation still running after {age}s; "
                  f"{offset} log bytes collected; command limit {attempt.get('timeout_seconds', 'unknown') if attempt else 'unknown'}s")
        try:
            workflow.save()
        except OSError:
            pass
        return result
    agent.qm = observed
    try:
        yield
    finally:
        agent.qm = original


def build(journal, state, active, root=None):
    if 'scenarioforge' not in journal.get('workflow', {}):
        return None  # Historical participant-only samples use their existing trial view.
    definitions_, app, core, participant = definitions(journal)
    records = journal.get('progress_steps', {})
    current = journal.get('current_step')
    child_key = current if records.get(current, {}).get('parent_step') else None
    if child_key:
        current = records[child_key]['parent_step']
    steps = []
    for key, label, vmid, description in definitions_:
        record = records.get(key, {})
        old = journal.get('stages', {}).get(key, {})
        nested = child_key if key == current and child_key else record.get('last_command')
        if nested:
            child = records.get(nested, {})
            record = dict(record)
            if nested == child_key:
                record['events'] = [*record.get('events', []), *child.get('events', [])][-60:]
                record['operation'] = child.get('operation', record.get('operation'))
                record['guest_observation'] = child.get('guest_observation')
            old = journal.get('stages', {}).get(nested, old)
        status = record.get('status', old.get('status', 'pending'))
        attempt = (old.get('attempts') or [{}])[-1]
        if key == 'evaluate' and state in ('completed', 'completed_with_errors'):
            status = state
        if status == 'running' and not active:
            status = 'interrupted' if state == 'interrupted' else 'cancelled' if state == 'cancelled' else 'failed' if state == 'failed' else 'waiting'
        if key == current and state in ('cancelled', 'interrupted') and status in ('running', 'failed'):
            status = state
        started = record.get('started_at', attempt.get('started_at'))
        ended = record.get('ended_at')
        log_tail = None
        if root is not None and old.get('log'):
            from cyber_agent_flow_eval import reporting
            try:
                path = reporting.within(root, root / old['log'])
                with path.open('rb') as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 32768))
                    log_tail = '\n'.join(clean(line) for line in stream.read(32768).decode(errors='replace').splitlines()[-60:])[-12000:]
            except (OSError, ValueError):
                pass
        files = old.get('files', {})
        steps.append(dict(id=key, label=label, vmid=vmid, description=description, status=status,
                          started_at=started, ended_at=ended,
                          elapsed_seconds=elapsed(started, ended or (None if active else journal.get('ended_at', journal.get('updated_at')))),
                          active=status == 'running', error=clean(record.get('error') or old.get('error')) if record.get('error') or old.get('error') else None,
                          attempt_count=len(old.get('attempts', [])), log=old.get('log'),
                          live_log_bytes=attempt.get('live_log_bytes'), last_output_at=attempt.get('last_output_at'),
                          vm_checks=record.get('vm_checks', []), operation=record.get('operation'), events=record.get('events', [])[-60:],
                          guest_observation=record.get('guest_observation'), transfers=record.get('transfers', [])[-20:],
                          timeout_seconds=attempt.get('timeout_seconds'), exitcode=attempt.get('exitcode'), log_tail=log_tail,
                          file_count=len(files), files=list(files)[:100],
                          readiness=journal.get('readiness_progress') if key=='readiness' else None))
    selected = next((s for s in steps if s['id'] == current), None)
    roles = [
        dict(role='scenarioforge', label='ScenarioForge', vmid=app, responsibility='Prepares and deploys the scenario; exports tasks and readiness evidence.'),
        dict(role='core', label='CORE', vmid=core, responsibility='Hosts the scenario network and services. Deployment is controlled through ScenarioForge’s configured CORE connection.'),
        dict(role='participant', label='CAF participant', vmid=participant, responsibility='Runs the agent and tools for each trial; returns outputs for scoring.'),
        dict(role='orchestrator', label='Orchestrator', vmid=None, responsibility='Coordinates stages, verifies transfers, scores trials and stores run artifacts.')]
    return dict(steps=steps, current_step=current, current=selected, roles=roles,
                completed_steps=sum(s['status'] in ('completed', 'completed_with_errors') for s in steps), total_steps=len(steps),
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
        record = workflow.journal.setdefault('progress_steps', {}).setdefault(workflow.journal.get('current_step'), {})
        record.setdefault('transfers', []).append(transfer)
        record['transfers'] = record['transfers'][-20:]
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
