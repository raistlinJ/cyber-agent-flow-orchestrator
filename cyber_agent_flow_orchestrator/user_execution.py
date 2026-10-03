"""Authenticated CLI bridge to private user workspaces and guarded qm calls."""
import getpass
import os
from pathlib import Path
import time

from cyber_agent_flow_eval.proxmox import authorized_operations
from .access import PVEAccess, AccessDenied
from .config import load
from .pve_auth import PVEProvider
from .tls import settings
from .workspaces import Workspace, workflow_vmids
from . import service


def login(web_config, username):
    config = settings(web_config)
    if config['auth']['provider'] == 'fusion':
        from .auth import FusionProvider
        from .access import FusionAccess
        provider = FusionProvider(config['users_file'], config['auth']['inventory_file'])
        record = provider.authenticate(username, getpass.getpass('Local operator password: '))
        if not record:
            raise AccessDenied('Login failed')
        return FusionAccess(provider, record)
    if config['auth']['provider'] != 'pve':
        raise ValueError('User workflows require PVE authentication')
    provider = PVEProvider(config['auth'])
    record = provider.authenticate(username, getpass.getpass('PVE password: '))
    if record and record.get('pending'):
        record = provider.authenticate(record['username'], '', otp=getpass.getpass('PVE TOTP code: '), challenge=record['credential'])
    if not record or record.get('pending'):
        raise AccessDenied('Login failed or orchestrator access not granted')
    started = time.monotonic()
    def current():
        if time.monotonic() - started >= min(config['session_max_seconds'], 7200):
            raise AccessDenied('PVE session expired; log in again to resume')
        if not provider.validate(record):
            raise AccessDenied('Orchestrator access revoked')
        return record
    return PVEAccess(provider, record, session_check=current)


def run(template, root, run_id, access, *, resume=False, retry_steps=False, retry_failed=False, progress=None):
    access.current()
    previous_umask = os.umask(0o077)
    try:
        workspace = Workspace(root, access.username)
        output = workspace.run_path(run_id)
        if resume:
            source = workspace.path / 'inputs' / run_id / 'workflow.yaml'
            if not source.is_file() or source.is_symlink():
                raise ValueError('No saved workflow to resume')
            cfg, runtime, _, _ = load(source)
            # Recovery can address IDs recorded in journals. The dispatch guard
            # checks those too, including if permissions changed since launch.
        else:
            cfg, runtime, _, _ = load(template)
            source = workspace.materialize(cfg, runtime, access, run_id)
            cfg, runtime, _, _ = load(source)
        for vmid in workflow_vmids(cfg, runtime):
            access.require_vm(vmid)
        with authorized_operations(access.qm):
            return service.run(source, output, resume=resume, retry_steps=retry_steps,
                               retry_failed=retry_failed, progress=progress)
    finally:
        os.umask(previous_umask)


def recover(root, run_id, access):
    access.current()
    workspace = Workspace(root, access.username)
    with authorized_operations(access.qm):
        return service.recover(workspace.run_path(run_id))
