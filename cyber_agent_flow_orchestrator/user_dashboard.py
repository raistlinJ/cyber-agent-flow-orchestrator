"""PVE dashboards have independent selections, caches, and private run roots."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time

from .config import load
from .monitor import ProxmoxProbe, definitions, snapshot
from .workspaces import Workspace, ROLES
from . import service


class UserDashboard:
    scoped = True

    def __init__(self, config, runs_root, interval=10, probe_factory=None):
        if not 2 <= interval <= 300:
            raise ValueError('Poll interval must be between 2 and 300 seconds')
        self.cfg, self.runtime, _, _ = load(config)
        self.root, self.interval = runs_root, interval
        self.probe_factory = probe_factory or (lambda backend, access: ProxmoxProbe(backend, access))
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='user-monitor')
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)
        self.entries = {}

    def start(self):
        pass  # Monitoring begins only with an authenticated request.

    def close(self):
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

    def read(self, access):
        available = access.inventory()
        permitted = {row['vmid'] for row in available}
        workspace = self.workspace(access)
        initial = self.cfg.get('monitoring', {}).get('initial_roles')
        if initial:
            workspace.seed_roles({k: v if v in permitted else None for k, v in initial.items()}, access)
        roles = {k: v if v in permitted else None for k, v in workspace.roles().items()}
        key = (tuple(roles.items()), tuple(sorted(permitted)))
        with self.lock:
            entry = self.entries.get(access.username)
            if entry is None or entry['key'] != key:
                entry = {'key': key, 'value': None, 'future': None, 'at': 0}
                self.entries[access.username] = entry
            future = entry['future']
            if future and future.done():
                try:
                    entry['value'] = future.result()
                except Exception:
                    entry['value'] = None
                entry['future'], entry['at'] = None, time.monotonic()
            if entry['future'] is None and time.monotonic() - entry['at'] >= self.interval:
                # Bound queued work, including login/session churn.
                if self.slots.acquire(blocking=False):
                    selected = definitions(self.cfg, self.runtime)
                    for item, role in zip(selected, ROLES):
                        item['vmid'] = roles[role]
                    try:
                        entry['future'] = self.pool.submit(snapshot, self.cfg, self.runtime, workspace.runs,
                                                          self.probe_factory(self.runtime['backend'], access), selected=selected)
                    except BaseException:
                        self.slots.release()
                        raise
                    entry['future'].add_done_callback(lambda _: self.slots.release())
            value = deepcopy(entry['value']) if entry['value'] else {
                'checked_at': None, 'workflow_id': self.cfg['id'], 'vms': [], 'runs': [], 'errors': []}
            value.update(refreshing=entry['future'] is not None, poll_seconds=self.interval,
                         available_vms=available, roles=roles, owner=access.username)
            # No completed result from a different user's scope is ever reused.
            if len(self.entries) > 256:
                for name in list(self.entries):
                    if name != access.username and (not self.entries[name]['future'] or self.entries[name]['future'].done()):
                        del self.entries[name]
                        break
        # Remove revoked or moved VMs even if access changed during cache retrieval.
        latest = {r['vmid'] for r in access.inventory()}
        value['vms'] = [vm for vm in value['vms'] if vm['vmid'] is None or vm['vmid'] in latest]
        value['available_vms'] = [vm for vm in available if vm['vmid'] in latest]
        value['roles'] = {k: v if v in latest else None for k, v in roles.items()}
        return value

    def run_detail(self, access, run_id, kind):
        workspace = self.workspace(access)
        path = workspace.run_path(run_id)
        # Caller supplies only an ID; owner, base directory and full path are server-derived.
        value = service.results(path) if kind == 'results' else service.status(path)
        access.current()
        return value
