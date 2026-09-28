"""Live, read-only Proxmox monitoring separate from workflow execution."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from cyber_agent_flow_eval import integration as ev
from . import service
from .guest_monitor import redact


def now():
    return datetime.now(timezone.utc).isoformat()


def elapsed(start):
    if not start:
        return None
    try:
        stamp = datetime.fromisoformat(start)
        if stamp.tzinfo is None:
            return None
        return max(0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return None


class ProxmoxProbe:
    def __init__(self, backend, access=None):
        self.access = access
        self.agent = ev.GuestAgent(dict(backend, command_timeout=5, poll_seconds=1),
                                   authorize=access.qm if access else None)
        self.agent.script = Path(__file__).with_name('guest_monitor.py').read_text()

    def vm(self, vmid):
        if self.access:
            self.access.require_vm(vmid)
        result = subprocess.run(['qm', 'status', str(vmid), '--verbose', '1'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            error = result.stderr.strip()[:500] or 'VM status check failed'
            missing = 'does not exist' in error and ('configuration' in error.lower() or '.conf' in error)
            return {'present': False if missing else None, 'power': 'missing' if missing else 'unknown', 'error': error}
        values = dict(line.split(':', 1) for line in result.stdout.splitlines() if ':' in line)
        values = {key.strip(): value.strip() for key, value in values.items()}
        if 'status' not in values:
            raise ValueError('Unrecognized qm status response')
        return {'present': True, 'power': values['status'], 'name': values.get('name'),
                'qmp_status': values.get('qmpstatus'), 'uptime_seconds': values.get('uptime')}

    def guest(self, vmid, definition, units):
        return self.agent.call(vmid, 'monitor', timeout=25, role=definition['role'], root=definition.get('root'), service_unit=definition.get('unit'), units=units)


def definitions(cfg, runtime):
    options = cfg.get('monitoring', {})
    backend = runtime['backend']
    items = [dict(role='scenarioforge', label='ScenarioForge', vmid=backend['app_vmid'],
                  root=options.get('scenarioforge_path', cfg['scenarioforge'].get('repo', '/opt/scenarioforge')),
                  unit=options.get('scenarioforge_service', 'scenarioforge-web.service')),
             dict(role='participant', label='Cyber-agent-flow', vmid=backend['participant_vmid'], root=runtime['engine']['path'],
                  unit=options.get('caf_service'))]
    items.append(dict(role='core', label='CoreVM', vmid=options.get('core_vmid'), root=None,
                      unit=options.get('core_service', 'core-daemon.service')))
    return items


def recorded_commands(root):
    """Read unfinished guest jobs; a journal alone never proves a job is live."""
    jobs, errors = [], []
    if not Path(root).exists():
        return jobs, errors
    for folder in sorted(Path(root).iterdir()):
        if folder.is_symlink() or not (folder / 'workflow.json').is_file():
            continue
        try:
            journal = ev.read_json(folder / 'workflow.json')
            commands = {prefix + c['id']: c for prefix, phase in [('prepare-', 'prepare'), ('artifact-', 'artifacts')]
                        for c in journal['workflow'].get(phase, [])}
            for key, stage in journal['stages'].items():
                for attempt in stage.get('attempts', []):
                    if attempt.get('stopped'):
                        continue
                    cmd = attempt.get('argv') or commands.get(key, {}).get('argv')
                    jobs.append({'run': folder.name, 'stage': key, 'vmid': attempt['vmid'], 'unit': attempt['unit'],
                                 'command': redact(cmd) if cmd else 'Command unavailable in older journal',
                                 'started_at': attempt.get('started_at'), 'recorded_status': stage['status']})
            for path in (folder / 'evaluation').glob('trials/*/attempt-*'):
                for transport in [path / 'transport.json', *path.glob('hook-*.json')]:
                    if not transport.is_file():
                        continue
                    record = ev.read_json(transport)
                    if record.get('stopped'):
                        continue
                    attempt = ev.read_json(path / 'attempt.json')
                    jobs.append({'run': folder.name, 'stage': attempt['trial_id'], 'vmid': record['vmid'], 'unit': record['unit'],
                                 'command': redact(record['argv']) if record.get('argv') else 'CAF evaluation worker / trial preparation',
                                 'started_at': record.get('started_at') or attempt.get('started_at'), 'recorded_status': attempt['status']})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({'run': folder.name, 'error': str(exc)})
    return jobs, errors


def snapshot(cfg, runtime, runs_root, probe=None, *, selected=None, progress=None):
    probe = probe or ProxmoxProbe(runtime['backend'])
    jobs, errors = recorded_commands(runs_root)
    def check(definition):
        row = dict(definition, present=None, power='not configured', guest_access='not checked',
                   application_present=None, processes=[], services=[], jobs=[])
        vmid = definition['vmid']
        if vmid is None:
            return row
        relevant = [job for job in jobs if job['vmid'] == vmid]
        row['jobs'] = [dict(job, live_state='unconfirmed', elapsed_seconds=elapsed(job['started_at'])) for job in relevant]
        try:
            row.update(probe.vm(vmid))
        except Exception as exc:
            row.update(power='unknown', error=str(exc))
            return row
        if row['power'] != 'running' or row.get('qmp_status') in ('paused', 'suspended', 'prelaunch', 'stopped'):
            return row
        units = list(dict.fromkeys(([definition['unit']] if definition.get('unit') else []) + [j['unit'] for j in relevant]))
        try:
            row.update(probe.guest(vmid, definition, units))
            row['guest_access'] = 'reachable'
            row['observed_at'] = now()
            states = {s['unit']: s for s in row['services']}
            for job in row['jobs']:
                state = states.get(job['unit'], {})
                job['live_state'] = ('running' if state.get('ActiveState') in ('active', 'activating') and state.get('SubState') not in ('exited', 'dead')
                                     else 'finished' if state.get('SubState') in ('exited', 'dead', 'failed') or state.get('LoadState') == 'not-found'
                                     else 'unconfirmed')
                micros = state.get('ExecMainStartTimestampMonotonic')
                if job['live_state'] == 'running' and micros and int(micros) > 0:
                    job['elapsed_seconds'] = max(0, row['guest_monotonic_seconds'] - int(micros) / 1_000_000)
                    job['elapsed_source'] = 'guest process start'
                else:
                    job['elapsed_source'] = 'host journal (includes dispatch)' if job['started_at'] else 'unavailable'
        except Exception as exc:
            row.update(guest_access='unavailable', guest_error=str(exc))
        return row
    items = selected if selected is not None else definitions(cfg, runtime)
    completed = []
    if progress:
        progress(completed)
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(check, item): index for index, item in enumerate(items)}
        vms = [None] * len(items)
        for future in as_completed(pending):
            row = future.result()
            vms[pending[future]] = row
            if row['vmid'] is not None:
                completed.append(row['vmid'])
                if progress:
                    progress(list(completed))
    try:
        runs = service.list_runs(runs_root) if Path(runs_root).is_dir() else []
    except Exception as exc:
        runs = []
        errors.append({'error': str(exc)})
    return {'checked_at': now(), 'workflow_id': cfg['id'], 'vms': vms, 'runs': runs, 'errors': errors,
            'scope': 'This Proxmox node; read-only guest and process observations'}
