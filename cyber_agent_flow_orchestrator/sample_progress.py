"""Owner-scoped sample progress from host journals only; no guest polling."""
from datetime import datetime, timezone
from cyber_agent_flow_eval import integration as ev, reporting
from .samples import CATALOG

PHASES = {'preparing': 'Preparing the guest workspace', 'uploading': 'Transferring worker and trial inputs',
          'starting': 'Starting the guest worker', 'executing': 'Guest worker executing',
          'stopping': 'Stopping the guest worker', 'collecting': 'Collecting trial outputs',
          'collected': 'Scoring and exporting trial results'}


def elapsed(start, end=None):
    if not start:
        return None
    try:
        finish = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
        return max(0, (finish - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def build(root, journal, report, state, coordinator):
    item = CATALOG.get(journal['sample_id'], {})
    active = coordinator or state == 'queued'
    rows = report.get('attempts', []) if report else []
    planned = report['planned_trials'] if report else item.get('trials', 0)
    finished = sum(row['status'] != 'running' for row in rows)
    events = list(journal.get('events', []))[-100:]
    trials, current = [], None
    for row in rows:
        trial = {key: row.get(key) for key in ('trial_id', 'condition_id', 'task_id', 'repetition',
                 'attempt', 'status', 'started_at', 'ended_at', 'verified_success', 'score', 'execution_seconds')}
        trial['elapsed_seconds'] = row.get('elapsed_seconds', elapsed(row.get('started_at'), row.get('ended_at')))
        if row.get('started_at'):
            events.append(dict(at=row['started_at'], kind='trial', message=f"{row['trial_id']} · {row['condition_id']} · attempt {row['attempt']} started"))
        if row.get('ended_at'):
            events.append(dict(at=row['ended_at'], kind='trial', message=f"{row['trial_id']} · {row['status']} · verified success: {row.get('verified_success')}"))
        transport = reporting.within(root / 'evaluation', root / 'evaluation' / row['attempt_path'] / 'transport.json')
        if transport.is_file():
            record = ev.read_json(transport)
            trial['transport'] = {key: record.get(key) for key in ('phase', 'updated_at', 'files_uploaded', 'files_total',
                                      'bytes_uploaded', 'bytes_total', 'execution_started_at')}
            trial['transport']['activity'] = PHASES.get(record.get('phase'),
                'Collecting trial outputs' if record.get('stopped') and not record.get('collected') else
                'Scoring and exporting trial results' if record.get('collected') else
                'Preparing or executing the guest trial (detailed stage unavailable)')
            trial['transport']['service'] = {key: record.get('service_status', {}).get(key)
                                            for key in ('ActiveState', 'SubState', 'Result', 'ExecMainPID')}
            for event in record.get('phase_events', [])[-20:]:
                events.append(dict(at=event['at'], kind='trial', message=f"{row['trial_id']} · {PHASES.get(event['phase'], event['phase'])}"))
        if row['status'] == 'running':
            if active: current = trial
            else: trial.update(status='unconfirmed', elapsed_seconds=None)
        trials.append(trial)
    events.sort(key=lambda event: event['at'])
    return dict(name=item.get('name', journal['sample_id']), active=active, phase=journal.get('phase', state),
                started_at=journal.get('started_at', journal.get('created_at')), ended_at=journal.get('ended_at'),
                updated_at=journal.get('updated_at', journal.get('created_at')), observed_at=datetime.now(timezone.utc).isoformat(),
                elapsed_seconds=elapsed(journal.get('started_at', journal.get('created_at')),
                    journal.get('ended_at') or (None if active else journal.get('updated_at', journal.get('created_at')))),
                planned_trials=planned, finished_trials=finished, percent=round(100 * finished / planned) if planned else None,
                verified_successes=sum(row.get('verified_success') is True for row in rows),
                errors=sum(row['status'] not in ('completed', 'running') for row in rows),
                max_turns=item.get('max_turns'), wall_seconds=item.get('wall_seconds'),
                current_trial=current, trials=trials, events=events[-100:])
