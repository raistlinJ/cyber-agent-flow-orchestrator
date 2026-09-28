"""Application service API shared by the CLI and read-only WebUI.

Functions return JSON-serializable values or raise exceptions; none print, parse
CLI arguments, or start a web server. run/recover are the execution entry points.
"""
from pathlib import Path
from datetime import datetime, timezone
from cyber_agent_flow_eval import integration as ev, reporting
from .config import load
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
    state = data['status']
    coordinator_active = reporting.active(root / '.workflow.lock')
    if data.get('sample_id') and not coordinator_active and state in ('preparing', 'evaluating'):
        state = 'interrupted'
    if data.get('sample_id') and not coordinator_active and state == 'queued':
        if (datetime.now(timezone.utc) - datetime.fromisoformat(data['created_at'])).total_seconds() > 15:
            state = 'interrupted'
    stopping = state in ('queued', 'preparing', 'evaluating') and (root / 'stop-request.json').is_file()
    if stopping: state = 'stopping'
    evaluation_status, sample_progress = None, None
    if (evaluation / 'manifest.json').is_file():
        evaluation_status = reporting.results(evaluation) if data.get('sample_id') else reporting.status(evaluation)
    if data.get('sample_id'):
        from .sample_progress import build
        sample_progress = build(root, data, evaluation_status, state, coordinator_active)
        if evaluation_status: evaluation_status.pop('attempts', None)
    runtime = data.get('runtime') or {}
    saved_settings = None
    if data.get('sample_id') and runtime:
        saved_settings = {'participant_vmid': runtime['backend']['participant_vmid'],
                          'provider': runtime['model']['provider'], 'model': runtime['model']['name']}
    return {'output': str(root), 'workflow_id': data['workflow']['id'],
            'workflow_hash': data['workflow_hash'], 'recorded_status': state,
            'coordinator_active': coordinator_active,
            'sample_id': data.get('sample_id'), 'message': 'Stop requested; finishing the current trial, collecting results and cleaning up' if stopping else data.get('message'),
            'sample_progress': sample_progress,
            'saved_settings': saved_settings,
            'error': data.get('error') if data['status'] in ('failed', 'interrupted') else None,
            'stages': {key: {'status': stage['status'], 'attempt_count': len(stage.get('attempts', [])),
                             'error': stage.get('error') if stage['status'] == 'failed' else None, 'log': stage.get('log')}
                       for key, stage in data['stages'].items()},
            'evaluation': evaluation_status}


def list_runs(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Run root does not exist: {root}')
    rows = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not (child / 'workflow.json').is_file():
            continue
        try:
            rows.append(status(child))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            rows.append({'output': str(child), 'error': str(exc)})
    return rows


def results(output, *, all_attempts=False):
    root, _ = journal(output)
    report = reporting.results(root / 'evaluation', all_attempts=all_attempts)
    from .failure_details import collected_failures
    return {'workflow': status(root), 'evaluation': report,
            'failure_diagnostics': collected_failures(root / 'evaluation', report['attempts'])}


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
        except BaseException:
            import shutil
            shutil.rmtree(destination)
            raise
    return {'destination': str(destination), 'attempts': len(report['evaluation']['attempts']),
            'selection': 'all attempts' if all_attempts else 'latest attempt per trial'}
