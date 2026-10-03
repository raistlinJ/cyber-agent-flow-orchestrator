"""Application service API shared by the CLI and read-only WebUI.

Functions return JSON-serializable values or raise exceptions; none print, parse
CLI arguments, or start a web server. run/recover are the execution entry points.
"""
from pathlib import Path
from copy import deepcopy
import time
from datetime import datetime, timezone
from cyber_agent_flow_eval import integration as ev, reporting
from .config import load
from .workflow_progress import build as workflow_progress
from .workflow import execute_command, run as execute, recover


def run(config, output, *, progress=None, **options):
    """Execute synchronously; optionally deliver progress messages to a callback."""
    return execute(config, output, progress=progress, **options)


def plan(config):
    cfg, runtime, _, identity = load(config)
    return {'workflow_id': cfg['id'], 'workflow_hash': identity,
            'backend': runtime['backend'], 'scenarioforge': cfg['scenarioforge'],
            'deploy_command': execute_command(cfg['scenarioforge'], runtime['backend'], '<run-id>') if cfg['scenarioforge']['mode'] == 'execute' else None,
            'prepare': cfg['prepare'], 'artifacts': cfg['artifacts'], 'collect': cfg['collect'],
            'conditions': [c['id'] for c in runtime['conditions']],
            'repetitions': runtime.get('repetitions', 1),
            'trial_count': 'exported tasks × conditions × repetitions; finalized after suite import'}


def journal(output):
    root = Path(output).resolve()
    if not (root / 'workflow.json').is_file():
        raise ValueError(f'No workflow journal: {root}')
    return root, ev.read_json(root / 'workflow.json')


def status(output):
    root, data = journal(output)
    evaluation = root / 'evaluation'
    browser_experiment = bool(data.get('sample_id') or data.get('scenario_experiment'))
    state = data['status']
    coordinator_active = reporting.active(root / '.workflow.lock')
    if browser_experiment and not coordinator_active and state in ('preparing', 'evaluating'):
        # The worker may have finished since the journal read, or be handing
        # off from snapshot verification to the workflow coordinator.
        root, data = journal(output)
        state = data['status']
        coordinator_active = reporting.active(root / '.workflow.lock')
        if not coordinator_active and state in ('preparing', 'evaluating'):
            handoff = (state == 'preparing' and data.get('phase') == 'verifying'
                       and (datetime.now(timezone.utc) - datetime.fromisoformat(
                           data.get('updated_at') or data.get('created_at') or '1970-01-01T00:00:00+00:00')).total_seconds() < 15)
            state = 'queued' if handoff else 'interrupted'
    if browser_experiment and not coordinator_active and data['status'] == 'queued':
        if (datetime.now(timezone.utc) - datetime.fromisoformat(data.get('queued_at', data['created_at']))).total_seconds() > 15:
            state = 'interrupted'
    stopping = state in ('queued', 'preparing', 'evaluating') and (root / 'stop-request.json').is_file()
    if stopping: state = 'stopping'
    evaluation_status, sample_progress = None, None
    failures, diagnostics = [], []
    if (evaluation / 'manifest.json').is_file():
        evaluation_status = reporting.results(evaluation) if browser_experiment else reporting.status(evaluation)
    if browser_experiment:
        from .sample_progress import build
        sample_progress = build(root, data, evaluation_status, state, coordinator_active)
        if evaluation_status:
            from .failure_details import trial_failures, collected_failures
            attempts = evaluation_status.pop('attempts', [])
            failures = trial_failures(attempts)
            if not coordinator_active and state in ('failed', 'completed_with_errors', 'interrupted', 'cancelled'):
                diagnostics = collected_failures(evaluation, attempts)
    runtime = data.get('runtime') or {}
    saved_settings = None
    if browser_experiment and runtime:
        saved_settings = {'participant_vmid': runtime['backend']['participant_vmid'],
                          'provider': runtime['model']['provider'], 'model': runtime['model']['name']}
    stages_progress = workflow_progress(data, state, coordinator_active, root=root)
    if stages_progress and sample_progress:
        for stage in stages_progress['steps']:
            if stage['id'] == 'evaluate':
                stage['trials'] = sample_progress['trials']
                stage['events'] = (stage['events'] + [event for event in sample_progress['events'] if event.get('kind') in ('trial', 'hint')])[-60:]
    return {'output': str(root), 'workflow_id': data['workflow']['id'],
            'workflow_hash': data['workflow_hash'], 'recorded_status': state,
            'coordinator_active': coordinator_active,
            'sample_id': data.get('sample_id'), 'scenario_experiment': data.get('scenario_experiment'), 'message': 'Stop requested; finishing the current stage or trial and collecting results' if stopping else data.get('message'),
            'sample_progress': sample_progress,
            'trial_failures': failures, 'failure_diagnostics': diagnostics,
            'workflow_progress': stages_progress,
            'saved_settings': saved_settings,
            'error': data.get('error') if data['status'] in ('failed', 'interrupted') else None,
            'stages': {key: {'status': stage['status'], 'attempt_count': len(stage.get('attempts', [])),
                             'error': stage.get('error') if stage['status'] == 'failed' else None, 'log': stage.get('log')}
                       for key, stage in data['stages'].items()},
            'evaluation': evaluation_status}


def list_runs(root, *, cache=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Run root does not exist: {root}')
    rows = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not (child / 'workflow.json').is_file():
            continue
        try:
            key = str(child)
            def stamp(name):
                try:
                    stat = (child / name).stat()
                    return stat.st_mtime_ns, stat.st_size
                except FileNotFoundError:
                    return None
            signature = tuple(stamp(name) for name in ('workflow.json', 'evaluation/manifest.json', 'evaluation/dataset.jsonl', 'stop-request.json'))
            saved = cache.get(key) if cache is not None else None
            if (saved and saved[0] == signature and time.monotonic() - saved[1] < 300
                    and not reporting.active(child / '.workflow.lock')
                    and not reporting.active(child / 'evaluation' / '.coordinator.lock')):
                rows.append(deepcopy(saved[2]))
                continue
            row = status(child)
            rows.append(row)
            if cache is not None:
                terminal = row['recorded_status'] in ('completed', 'completed_with_errors', 'failed', 'cancelled', 'interrupted')
                if terminal and not row.get('coordinator_active') and not (row.get('evaluation') or {}).get('coordinator_active'):
                    cache[key] = (signature, time.monotonic(), deepcopy(row))
                else:
                    cache.pop(key, None)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            rows.append({'output': str(child), 'error': str(exc)})
    if cache is not None:
        present = {row['output'] for row in rows}
        for key in list(cache):
            if key not in present:
                del cache[key]
    return rows


def results(output, *, all_attempts=False):
    root, _ = journal(output)
    report = reporting.results(root / 'evaluation', all_attempts=all_attempts)
    from .failure_details import collected_failures
    from .run_artifacts import configuration, saved_json
    for attempt in report['attempts']:
        try:
            attempt['flag_progress'] = saved_json(root / 'evaluation', attempt['attempt_path'] + '/progress.json')
        except (OSError, ValueError):
            pass
    return {'workflow': status(root), 'evaluation': report, 'run_configuration': configuration(root),
            'failure_diagnostics': collected_failures(root / 'evaluation', report['attempts'])}


def summary_documents(output, *, report=None):
    """Render current saved results, including failed pre-evaluation workflows."""
    root, _ = journal(output)
    if report is None:
        if (root / 'evaluation/manifest.json').is_file():
            report = results(root, all_attempts=True)
        else:
            from .run_artifacts import configuration
            report = dict(workflow=status(root), evaluation=None, run_configuration=configuration(root))
    from .run_report import render
    return render(report, root=root)


def logs(output, *, stage=None, trial=None, attempt=None, lines=100):
    root, data = journal(output)
    if (stage is None) == (trial is None):
        raise ValueError('Select exactly one stage or trial')
    if trial is not None:
        return reporting.logs(root / 'evaluation', trial, attempt=attempt, lines=lines)
    if stage not in data['stages']:
        raise ValueError('Unknown stage')
    records = data['stages'][stage].get('attempts', [])
    if not records:
        raise ValueError('This stage has no guest command logs')
    number = len(records) if attempt is None else attempt
    if type(number) is not int or not 1 <= number <= len(records):
        raise ValueError('No matching stage attempt')
    name = records[number - 1].get('host_log')
    if not name:
        raise ValueError('No collected log for this stage attempt')
    return {'stage': stage, 'attempt': number, 'logs': {name: reporting.tail(root, root / name, lines)}}


def export_results(output, destination, *, all_attempts=False):
    root, _ = journal(output)
    destination = Path(destination).resolve()
    if destination.is_relative_to(root):
        raise ValueError('Export destination must be outside the run directory')
    reporting.manifest(root / 'evaluation')
    with ev.lease(root / '.workflow.lock'), ev.lease(root / 'evaluation/.coordinator.lock'):
        report = results(root, all_attempts=all_attempts)
        report['workflow']['coordinator_active'] = False
        report['workflow']['evaluation']['coordinator_active'] = False
        report['evaluation']['coordinator_active'] = False
        destination.mkdir(parents=True, exist_ok=False)
        try:
            reporting.write_export(destination, report['evaluation'])
            ev.write_json(destination / 'workflow-summary.json', report['workflow'])
            for filename, content in summary_documents(root, report=report).items():
                (destination / filename).write_text(content, encoding='utf-8')
        except BaseException:
            import shutil
            shutil.rmtree(destination)
            raise
    return {'destination': str(destination), 'attempts': len(report['evaluation']['attempts']),
            'selection': 'all attempts' if all_attempts else 'latest attempt per trial'}
