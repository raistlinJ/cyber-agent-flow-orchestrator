"""PVE dashboards have independent selections, caches, and private run roots."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time

from .config import load
from .monitor import ProxmoxProbe, definitions, snapshot
from .workspaces import Workspace, ROLES
from . import service
from .observation_cache import CachedProbe, ObservationCache


class UserDashboard:
    scoped = True

    def __init__(self, config, runs_root, interval=10, probe_factory=None, *, samples=None, updates=None):
        if not 2 <= interval <= 300:
            raise ValueError('Poll interval must be between 2 and 300 seconds')
        self.cfg, self.runtime, _, _ = load(config)
        self.root, self.interval = runs_root, interval
        self.probe_factory = probe_factory or (lambda backend, access: ProxmoxProbe(backend, access))
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='user-monitor')
        self.lock = threading.RLock()
        self.creation_records = {}
        self.start_records = {}
        self.slots = threading.BoundedSemaphore(4)
        self.entries = {}
        from .samples import SampleManager
        self.samples = SampleManager(self.runtime, runs_root, samples)
        from .scenarios import ScenarioExperiments
        self.scenarios = ScenarioExperiments(self.cfg, self.runtime, runs_root, self.samples)
        from .updates import UpdateManager
        self.updates = UpdateManager(self.cfg, self.runtime, runs_root, updates)
        from .model_config import ModelConfigs
        self.model_configs = ModelConfigs(self.cfg, self.runtime, runs_root, (self.updates.config or {}).get('group', 'caf-maintainers'), enabled=self.updates.config is not None)

    def start(self):
        pass  # Monitoring begins only with an authenticated request.

    def close(self):
        self.samples.close()
        self.updates.close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def workspace(self, access):
        access.current()
        return Workspace(self.root, access.username)

    def select(self, access, roles):
        workspace = self.workspace(access)
        result = workspace.save_roles(roles, access)
        with self.lock:
            self.entries.pop(access.username, None)
        return {'roles': result}

    def read(self, access, *, force=False, observe=True, fresh=False):
        available = access.inventory()
        permitted = {row['vmid'] for row in available}
        # inventory() has just checked this identity. Recheck inventory/identity
        # at the end as well, without a redundant remote check to open local files.
        workspace = Workspace(self.root, access.username)
        initial = self.cfg.get('monitoring', {}).get('initial_roles')
        if initial:
            workspace.seed_roles({k: v if v in permitted else None for k, v in initial.items()}, access)
        roles = {k: v if v in permitted else None for k, v in workspace.roles().items()}
        key = (tuple(roles.items()), tuple(sorted(permitted)))
        with self.lock:
            entry = self.entries.get(access.username)
            if entry is None or entry['key'] != key:
                entry = {'key': key, 'value': None, 'future': None, 'at': 0, 'completed': [],
                         'observations': ObservationCache(), 'run_cache': {}, 'run_cache_lock': threading.Lock()}
                self.entries[access.username] = entry
            future = entry['future']
            if future and future.done():
                try:
                    entry['value'] = future.result()
                except Exception:
                    entry['value'] = None
                entry['future'], entry['at'] = None, time.monotonic()
            if entry['future'] is None and (force or (observe and time.monotonic() - entry['at'] >= self.interval)):
                # Bound queued work, including login/session churn.
                if self.slots.acquire(blocking=False):
                    selected = definitions(self.cfg, self.runtime)
                    for item, role in zip(selected, ROLES):
                        item['vmid'] = roles[role]
                    entry['completed'] = []
                    def progress(completed):
                        with self.lock:
                            entry['completed'] = completed
                    try:
                        entry['future'] = self.pool.submit(snapshot, self.cfg, self.runtime, workspace.runs,
                                                          CachedProbe(self.probe_factory(self.runtime['backend'], access), entry['observations'], fresh=fresh),
                                                          selected=selected, progress=progress, include_runs=False)
                    except BaseException:
                        self.slots.release()
                        raise
                    def finish(future):
                        self.slots.release()
                        with self.lock:
                            if entry['future'] is future:
                                try:
                                    entry['value'] = future.result()
                                except Exception:
                                    entry['value'] = None
                                entry['future'], entry['at'] = None, time.monotonic()
                    entry['future'].add_done_callback(finish)
            value = deepcopy(entry['value']) if entry['value'] else {
                'checked_at': None, 'workflow_id': self.cfg['id'], 'vms': [], 'runs': [], 'errors': []}
            value.update(refreshing=entry['future'] is not None, poll_seconds=self.interval,
                         available_vms=available, roles=roles, owner=access.username)
            completed = set(entry['completed'])
            # No completed result from a different user's scope is ever reused.
            if len(self.entries) > 256:
                for name in list(self.entries):
                    if name != access.username and (not self.entries[name]['future'] or self.entries[name]['future'].done()):
                        del self.entries[name]
                        break
        # Remove revoked or moved VMs even if access changed during cache retrieval.
        latest = {r['vmid'] for r in access.inventory()}
        # Warm caches can finish while the request performs its final access
        # check. Return that observation immediately, avoiding another poll.
        with self.lock:
            if entry['value']:
                for field in ('checked_at', 'vms', 'errors', 'scope'):
                    if field in entry['value']:
                        value[field] = deepcopy(entry['value'][field])
            value['refreshing'] = entry['future'] is not None
            completed = set(entry['completed'])
        value['vms'] = [vm for vm in value['vms'] if vm['vmid'] is None or vm['vmid'] in latest]
        value['available_vms'] = [vm for vm in available if vm['vmid'] in latest]
        value['roles'] = {k: v if v in latest else None for k, v in roles.items()}
        selected_ids = {vmid for vmid in value['roles'].values() if vmid is not None}
        total, done = len(selected_ids), len(completed & selected_ids)
        value['loading'] = {'completed': done, 'total': total,
                            'percent': round(100 * done / total) if total else 100,
                            'status': 'Checking VM power, guest access and applications' if value['refreshing'] else 'VM checks complete'}
        # Trial progress is independent of a potentially slow guest observation.
        with entry['run_cache_lock']:
            value['runs'] = service.list_runs(workspace.runs, cache=entry['run_cache'])
        value['samples'] = self.samples.catalog(value['roles'], workspace)
        value['scenarios'] = dict(enabled=True, vmid=value['roles'].get('scenarioforge'),
            allowed_targets=', '.join(self.runtime['execution']['network_policy']['allow']),
            disallowed_targets=', '.join(self.runtime['execution']['network_policy']['disallow']),
            max_turns=self.runtime['execution']['max_turns'], wall_seconds=self.runtime['execution']['wall_seconds'])
        from .model_config import apply_model
        runtime = apply_model(workspace, self.runtime, value['roles'].get('participant'))
        value['experiment_defaults'] = dict(
            source='Server runtime YAML plus saved model settings for the selected participant VM',
            engine=dict(path=runtime['engine']['path'], python=runtime['engine']['python']),
            model={key: runtime['model'].get(key) for key in ('provider', 'name', 'url', 'ssl_verify')},
            evaluation=dict(repetitions=1, **{key: runtime['execution'].get(key, {'tool_timeout':60,'context_window':8192}.get(key)) for key in
                ('max_turns', 'wall_seconds', 'tool_timeout', 'context_window')}))
        value['updates'] = self.updates.view(access, workspace, value['roles'])
        access.current()
        return value

    def run_detail(self, access, run_id, kind):
        workspace = self.workspace(access)
        path = workspace.run_path(run_id)
        # Caller supplies only an ID; owner, base directory and full path are server-derived.
        if kind == 'results' and not (path / 'evaluation/manifest.json').is_file():
            from .run_artifacts import configuration
            value = {'workflow': service.status(path), 'evaluation': None, 'run_configuration': configuration(path)}
        else:
            value = service.results(path) if kind == 'results' else service.status(path)
        access.current()
        return value

    def run_sample(self, access, sample_id, request_id):
        created = self.scenarios.create_sample(access, sample_id, request_id)
        return self.scenarios.run_saved(access, created['run_id'], request_id)

    def experiment(self, access, action, data):
        if action != 'run':
            return self._experiment(access, action, data)
        import re
        from .samples import SampleRequestError
        request_id = data.get('request_id', '')
        if not re.fullmatch('[0-9a-f]{32}', request_id):
            raise SampleRequestError('Supply a valid request ID')
        key = (access.username, request_id)
        def progress(step, message, state='running'):
            from datetime import datetime, timezone
            with self.lock:
                self.start_records[key] = dict(step=step, total=5, message=message, state=state,
                    updated_at=datetime.now(timezone.utc).isoformat())
                while len(self.start_records) > 256:
                    self.start_records.pop(next(iter(self.start_records)))
        progress(1, 'Validating the saved experiment and account access')
        try:
            result = self._experiment(access, action, data, progress=progress)
            progress(5, 'Coordinator accepted the experiment; follow execution in Progress', 'completed')
            return result
        except Exception:
            with self.lock:
                current = self.start_records.get(key, {})
            progress(current.get('step', 1), current.get('message', 'Starting experiment'), 'failed')
            raise

    def _experiment(self, access, action, data, progress=lambda step, message: None):
        if action == 'create' and ('sample_id' in data or 'selection_id' in data):
            import re
            from .samples import SampleRequestError
            request_id = data.get('request_id', '')
            if not re.fullmatch('[0-9a-f]{32}', request_id):
                raise SampleRequestError('Supply a valid request ID')
            key = (access.username, request_id)
            total = 8 if 'sample_id' in data else 4
            def progress(step, message, state='running'):
                from datetime import datetime, timezone
                with self.lock:
                    self.creation_records[key] = dict(step=step, total=total, message=message,
                        state=state, updated_at=datetime.now(timezone.utc).isoformat())
                    while len(self.creation_records) > 256:
                        self.creation_records.pop(next(iter(self.creation_records)))
            progress(1, 'Checking experiment inputs and waiting for the creation workspace')
            try:
                create = self.scenarios.create_sample if 'sample_id' in data else self.scenarios.create
                result = create(access, **data, progress=progress)
                progress(total, 'Experiment saved; ready to run', 'completed')
                return result
            except Exception:
                with self.lock:
                    current = self.creation_records.get(key, {})
                progress(current.get('step', 1), current.get('message', 'Creating experiment'), 'failed')
                raise
        if action != 'create':
            path = self.workspace(access).run_path(data['run_id'])
            from cyber_agent_flow_eval import integration as ev
            try:
                record = ev.read_json(path / 'workflow.json')
            except FileNotFoundError:
                from .samples import SampleRequestError
                raise SampleRequestError('Experiment not found') from None
            if record.get('scenario_experiment'):
                return {'run': self.scenarios.run_saved, 'stop': self.scenarios.stop}[action](access, **data, **({'progress':progress} if action == 'run' else {}))
        return {'create':self.samples.create, 'run':self.samples.run_saved, 'stop':self.samples.stop}[action](access, **data, **({'progress':progress} if action == 'run' else {}))

    def creation_status(self, access, request_id, *, starting=False):
        import re
        if not re.fullmatch('[0-9a-f]{32}', request_id):
            raise ValueError('Invalid creation request ID')
        access.current()
        with self.lock:
            return deepcopy((self.start_records if starting else self.creation_records).get((access.username, request_id),
                dict(step=0, total=None, state='pending', message='Waiting for the launch request to start' if starting else 'Waiting for the creation request to start')))

    def maintain(self, access, data):
        return self.updates.submit(access, **data)

    def artifact(self, access, run_id, artifact_id):
        from .run_artifacts import download
        workspace = self.workspace(access)
        return download(workspace.run_path(run_id), artifact_id)

    def dataset(self, access, run_id):
        workspace = self.workspace(access)
        path = workspace.run_path(run_id) / 'evaluation/dataset.csv'
        if path.is_symlink() or not path.resolve().is_relative_to(workspace.runs) or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Dataset is not available for browser download')
        data = path.read_bytes()
        access.current()
        return data
