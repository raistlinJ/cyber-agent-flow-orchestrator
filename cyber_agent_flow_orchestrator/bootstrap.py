"""Create editable launch defaults without replacing operator configuration."""
from importlib.resources import files
from pathlib import Path
import socket

import yaml

from .auth import private_file


def _missing(path):
    # Broken symlinks are operator-owned paths too; never replace them.
    return not path.exists() and not path.is_symlink()


def prepare(workflow=None, web_config=None):
    workflow_path = Path(workflow or 'workflow.yaml').absolute()
    web_path = Path(web_config or 'web.yaml').absolute()
    # A typo in an explicitly supplied filename must not create a new config.
    for supplied, path in ((workflow, workflow_path), (web_config, web_path)):
        if supplied is not None and not path.is_file():
            raise FileNotFoundError(f'Configuration does not exist: {path}')
    if workflow is None and _missing(workflow_path):
        templates = files('cyber_agent_flow_orchestrator').joinpath('defaults')
        for name in ('catalogs/baseline.json', 'runtime.yaml', 'workflow.yaml'):
            destination = workflow_path.parent / name
            if _missing(destination):
                private_file(destination, templates.joinpath(name).read_bytes())
    if web_config is None and _missing(web_path):
        hostname = socket.getfqdn()
        data = dict(version=1, listen='127.0.0.1', port=8443,
                    public_url='https://localhost:8443',
                    certificate='/certs/cert.pem', private_key='/certs/key.pem',
                    session_idle_seconds=1800, session_max_seconds=7200,
                    samples=['smoke', 'tools-vs-helper'],
                    auth=dict(provider='pve', url=f'https://{hostname}:8006',
                              ca_file='/etc/pve/pve-root-ca.pem',
                              required_group='caf-orchestration', realms=['pve', 'pam']))
        private_file(web_path, yaml.safe_dump(data, sort_keys=False).encode())
    return str(workflow_path), str(web_path)
