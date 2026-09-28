"""Import non-secret settings from ScenarioForge's Proxmox provision config."""
from copy import deepcopy
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

import yaml

from cyber_agent_flow_eval import integration as ev
from .auth import private_file
from .config import load

IMPORTED = {'core_vmid', 'app_vmid', 'participant_vmid',
            'llm_provider_type', 'llm_provider_url', 'llm_model'}


def read_provision(path):
    """Parse data, never source a shell file or retain its password fields."""
    path = Path(path)
    values = {}
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            key, value = key.strip(), value.strip()
            error = f'Provision config line {number}'
            if not separator or not re.fullmatch(r'[a-z][a-z0-9_]*', key):
                raise ValueError(error + ': expected lowercase key=value')
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(error + ': unmatched quote')
                value = value[1:-1]
            if key in IMPORTED:
                values[key] = value
    for key, default in (('core_vmid', 9401), ('app_vmid', 9402), ('participant_vmid', 9403)):
        value = values.get(key, str(default))
        if not re.fullmatch(r'[0-9]+', value) or not 100 <= int(value) <= 999999999:
            raise ValueError(f'Provision config: {key} must be a valid Proxmox VM ID')
        values[key] = int(value)
    if len({values[k] for k in ('core_vmid', 'app_vmid', 'participant_vmid')}) != 3:
        raise ValueError('Provision config: VM IDs must be distinct')
    if values.get('llm_provider_type') and values['llm_provider_type'] not in ('ollama_direct', 'openai', 'litellm', 'claude'):
        raise ValueError('Provision config: unsupported llm_provider_type')
    if values.get('llm_provider_url'):
        try:
            url = urlsplit(values['llm_provider_url'])
            valid = (url.scheme in ('http', 'https') and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment and url.port != 0
                     and not re.search(r'[\s\x00-\x1f]', values['llm_provider_url']))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Provision config: llm_provider_url must be an HTTP(S) endpoint without credentials, query or fragment')
    return values


def import_profile(provision_config, workflow, *, output=None):
    """Create a separate editable snapshot; retain existing cached profile edits."""
    values = read_provision(provision_config)
    cfg, runtime, assets, _ = load(workflow)
    cfg, runtime = deepcopy(cfg), deepcopy(runtime)
    old = runtime['backend']
    mapping = {old['app_vmid']: values['app_vmid'], old['participant_vmid']: values['participant_vmid']}
    previous_core = cfg.get('monitoring', {}).get('core_vmid')
    if previous_core:
        mapping[previous_core] = values['core_vmid']
    old.update(app_vmid=values['app_vmid'], participant_vmid=values['participant_vmid'])
    for command in cfg['prepare'] + cfg['artifacts'] + old['before_trial']:
        command['vmid'] = mapping.get(command['vmid'], command['vmid'])
    cfg.setdefault('monitoring', {}).update(core_vmid=values['core_vmid'], initial_roles={
        'scenarioforge': values['app_vmid'], 'participant': values['participant_vmid'], 'core': values['core_vmid']})
    # Only the shipped example lock is derived. A custom shared-lab lock is kept.
    if runtime['execution']['target_lock'] == str(Path(f'/var/lock/caf-lab-{previous_core}.lock').resolve()):
        runtime['execution']['target_lock'] = f'/var/lock/caf-lab-{values["core_vmid"]}.lock'
    for source, target in (('llm_provider_type', 'provider'), ('llm_provider_url', 'url'), ('llm_model', 'name')):
        if values.get(source):
            runtime['model'][target] = values[source]
    # Match CAF's provisioned client convention without copying any API key.
    runtime['model'].setdefault('api_key_env', 'MCP_API_KEY')
    cfg['runtime'] = 'runtime.yaml'
    identity = ev.digest({'workflow': cfg, 'runtime': runtime, 'assets': assets})
    destination = Path(output) if output is not None else Path('.local/provision') / identity[:20]
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ev.lease(destination.parent / ('.' + destination.name + '.lock')):
        if destination.exists() or destination.is_symlink():
            if output is not None or destination.is_symlink():
                raise ValueError('Provision profile destination already exists; choose a new directory')
            load(destination / 'workflow.yaml')
            return str(destination / 'workflow.yaml')
        destination.mkdir(mode=0o700)
        try:
            private_file(destination / 'runtime.yaml', yaml.safe_dump(runtime, sort_keys=False).encode())
            private_file(destination / 'workflow.yaml', yaml.safe_dump(cfg, sort_keys=False).encode())
            load(destination / 'workflow.yaml')
        except BaseException:
            shutil.rmtree(destination)
            raise
    return str(destination / 'workflow.yaml')
