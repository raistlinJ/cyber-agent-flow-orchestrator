"""Private host workspaces, VM role selections, and ownership of saved runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from cyber_agent_flow_eval import integration as ev
from .access import AccessDenied
from .auth import private_file

ROLES = ('scenarioforge', 'participant', 'core')


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise AccessDenied('Workspace symlinks are not supported')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


class Workspace:
    def __init__(self, root, username):
        if not isinstance(username, str) or not username or len(username) > 64:
            raise AccessDenied('Invalid workspace owner')
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        users = private_directory(root / '_users')
        key = hashlib.sha256(('pve:' + username).encode()).hexdigest()
        self.path = private_directory(users / key)
        self.username = username
        owner = self.path / 'owner.json'
        if owner.is_symlink():
            raise AccessDenied('Invalid workspace owner file')
        with ev.lease(self.path / '.workspace.lock'):
            if owner.exists():
                if json.loads(owner.read_text()) != {'provider': 'pve', 'username': username}:
                    raise AccessDenied('Workspace owner mismatch')
            else:
                private_file(owner, json.dumps({'provider': 'pve', 'username': username}).encode())
        self.runs = private_directory(self.path / 'runs')

    def roles(self):
        path = self.path / 'roles.json'
        if path.is_symlink():
            raise AccessDenied('Invalid VM selection file')
        if not path.exists():
            return dict.fromkeys(ROLES)
        return self.validate_roles(json.loads(path.read_text()))

    @staticmethod
    def validate_roles(value):
        if not isinstance(value, dict) or set(value) != set(ROLES):
            raise ValueError('Select ScenarioForge, participant, and CoreVM roles')
        ids = [n for n in value.values() if n is not None]
        if any(type(n) is not int or not 100 <= n <= 999999999 for n in ids) or len(set(ids)) != len(ids):
            raise ValueError('VM roles require distinct valid VM IDs, or null to clear a role')
        return value

    def save_roles(self, value, access):
        value = self.validate_roles(value)
        for vmid in value.values():
            if vmid is not None:
                access.require_vm(vmid)
        access.current()
        path = self.path / 'roles.json'
        if path.is_symlink():
            raise AccessDenied('Invalid VM selection file')
        with ev.lease(self.path / '.workspace.lock'):
            private_file(path, json.dumps(value).encode(), replace=path.exists())
        return value

    def seed_roles(self, value, access):
        """Initialize imported roles once, preserving saved and cleared choices."""
        path = self.path / 'roles.json'
        if path.exists() or path.is_symlink():
            return self.roles()
        value = self.validate_roles(value)
        for vmid in value.values():
            if vmid is not None:
                access.require_vm(vmid)
        access.current()
        with ev.lease(self.path / '.workspace.lock'):
            if not path.exists() and not path.is_symlink():
                private_file(path, json.dumps(value).encode())
        return self.roles()

    def run_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name):
            raise AccessDenied('Invalid run ID')
        path = self.runs / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.runs):
            raise AccessDenied('Invalid run path')
        return path

    def materialize(self, cfg, runtime, access, run_id):
        """Freeze the selected VM roles into a private per-run input directory."""
        roles = self.roles()
        if any(roles[k] is None for k in ('scenarioforge', 'participant')):
            raise ValueError('Select ScenarioForge and participant VMs in the WebUI first')
        cfg, runtime = deepcopy(cfg), deepcopy(runtime)
        backend = runtime['backend']
        mapping = {backend['app_vmid']: roles['scenarioforge'], backend['participant_vmid']: roles['participant']}
        old_core = cfg.get('monitoring', {}).get('core_vmid')
        if old_core:
            if roles['core'] is not None:
                mapping[old_core] = roles['core']
            elif any(c['vmid'] == old_core for c in cfg['prepare'] + cfg['artifacts'] + backend['before_trial']):
                raise ValueError('This workflow needs a CoreVM selection')
        backend.update(app_vmid=roles['scenarioforge'], participant_vmid=roles['participant'])
        cfg.setdefault('monitoring', {}).pop('core_vmid', None)
        if roles['core']:
            cfg['monitoring']['core_vmid'] = roles['core']
        for command in cfg['prepare'] + cfg['artifacts'] + backend['before_trial']:
            command['vmid'] = mapping.get(command['vmid'], command['vmid'])
        ids = workflow_vmids(cfg, runtime)
        for vmid in ids:
            access.require_vm(vmid)
        # Keep the template target lock shared across users, plus existing per-VM
        # locks. Never use a private per-user lock for the same physical lab.
        inputs = private_directory(self.path / 'inputs')
        self.run_path(run_id)  # Validate ID before using it anywhere on disk.
        directory = inputs / run_id
        if directory.exists() or directory.is_symlink():
            raise ValueError('Run inputs already exist; resume or choose a new run ID')
        private_directory(directory)
        import yaml
        private_file(directory / 'runtime.yaml', yaml.safe_dump(runtime).encode())
        cfg['runtime'] = 'runtime.yaml'
        private_file(directory / 'workflow.yaml', yaml.safe_dump(cfg).encode())
        return directory / 'workflow.yaml'


def workflow_vmids(cfg, runtime):
    result = {runtime['backend']['app_vmid'], runtime['backend']['participant_vmid']}
    if cfg.get('monitoring', {}).get('core_vmid'):
        result.add(cfg['monitoring']['core_vmid'])
    result.update(c['vmid'] for c in cfg.get('prepare', []) + cfg.get('artifacts', []) + runtime['backend'].get('before_trial', []))
    return result
