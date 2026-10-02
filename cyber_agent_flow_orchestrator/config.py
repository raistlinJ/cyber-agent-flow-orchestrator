"""Strict workflow schema and immutable host input identity."""
from copy import deepcopy
from pathlib import Path
import tempfile
import yaml

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_eval.backends import guest_path


def job(value, backend):
    ev.fields(value, ['id', 'vmid', 'argv', 'timeout_seconds', 'user', 'cwd', 'environment_file'],
              ['id', 'vmid', 'argv', 'timeout_seconds'], 'guest command')
    ev.identifier(value['id'])
    # Use the evaluator's validation for the common guest job schema.
    ev.resolve_backend(dict(backend, before_trial=[{k: v for k, v in value.items() if k != 'id'}]))
    if not 100 <= value['vmid'] <= 999999999:
        raise ValueError('Command vmid must be a Proxmox VM ID')


def load(path):
    path = Path(path).resolve()
    cfg = yaml.load(path.read_text(), Loader=ev.StrictLoader)
    ev.fields(cfg, ['version', 'id', 'runtime', 'scenarioforge', 'prepare', 'artifacts', 'collect', 'max_readiness_age_seconds', 'monitoring'],
              ['version', 'id', 'runtime', 'scenarioforge'], 'workflow')
    if type(cfg['version']) is not int or cfg['version'] != 1:
        raise ValueError('Only workflow version 1 is supported')
    ev.identifier(cfg['id'])
    runtime = ev.read_runtime(path.parent / cfg['runtime'])
    runtime['execution'].setdefault('auto_approve_dangerous', True)
    if runtime['backend']['type'] != 'proxmox':
        raise ValueError('Orchestration currently requires proxmox; macos/linux/windows are placeholders')
    backend = runtime['backend']
    if 'app_vmid' not in backend:
        raise ValueError('Runtime backend.app_vmid is required')
    monitoring = cfg.get('monitoring', {})
    ev.fields(monitoring, ['core_vmid', 'scenarioforge_path', 'scenarioforge_service', 'caf_service', 'core_service', 'initial_roles', 'scenarioforge_xml_roots'], [], 'monitoring')
    if 'scenarioforge_xml_roots' in monitoring:
        roots = monitoring['scenarioforge_xml_roots']
        if not isinstance(roots, list) or not 1 <= len(roots) <= 10:
            raise ValueError('scenarioforge_xml_roots must contain 1 to 10 guest directories or XML paths')
        for root in roots:
            guest_path(root, 'scenarioforge_xml_roots')
    if 'initial_roles' in monitoring:
        from .workspaces import Workspace
        Workspace.validate_roles(monitoring['initial_roles'])
    if 'core_vmid' in monitoring:
        ev.positive(monitoring['core_vmid'], 'monitoring.core_vmid')
        if not 100 <= monitoring['core_vmid'] <= 999999999 or monitoring['core_vmid'] in (backend['app_vmid'], backend['participant_vmid']):
            raise ValueError('monitoring.core_vmid must identify a distinct Proxmox VM')
    if 'scenarioforge_path' in monitoring:
        guest_path(monitoring['scenarioforge_path'], 'monitoring.scenarioforge_path')
    import re
    for key in ('scenarioforge_service', 'caf_service', 'core_service'):
        if key in monitoring and (not isinstance(monitoring[key], str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service', monitoring[key])):
            raise ValueError('monitoring.' + key + ' must name a systemd .service unit')
    sf = cfg['scenarioforge']
    if not isinstance(sf, dict):
        raise ValueError('scenarioforge must be a mapping')
    if sf.get('mode') == 'reuse_export':
        ev.fields(sf, ['mode', 'archive', 'reproduction_archive'], ['mode', 'archive'], 'scenarioforge')
        guest_path(sf['archive'], 'archive')
    elif sf.get('mode') == 'execute':
        ev.fields(sf, ['mode', 'xml', 'scenario', 'suite_id', 'output_root', 'repo', 'python', 'user', 'environment_file', 'timeout_seconds', 'tasks', 'split', 'reproduction_archive'],
                  ['mode', 'xml', 'scenario', 'suite_id', 'output_root'], 'scenarioforge')
        sf.setdefault('repo', '/opt/scenarioforge')
        sf.setdefault('python', '/opt/scenarioforge/.venv/bin/python')
        sf.setdefault('user', 'scenarioforge')
        sf.setdefault('timeout_seconds', 1800)
        for key in ('xml', 'output_root', 'repo', 'python'):
            guest_path(sf[key], 'scenarioforge.' + key)
        if 'tasks' in sf:
            guest_path(sf['tasks'], 'scenarioforge.tasks')
        sf.setdefault('split', 'development')
        if sf['split'] not in ('development', 'validation', 'test'):
            raise ValueError('scenarioforge.split must be development, validation, or test')
        ev.identifier(sf['suite_id'])
        if not isinstance(sf['scenario'], str) or not sf['scenario']:
            raise ValueError('scenarioforge.scenario is required')
        job(dict(id='deploy', vmid=backend['app_vmid'], argv=[sf['python']],
                 timeout_seconds=sf['timeout_seconds'], user=sf['user'], cwd=sf['repo'],
                 **({'environment_file': sf['environment_file']} if 'environment_file' in sf else {})), backend)
    else:
        raise ValueError('scenarioforge.mode must be reuse_export or execute')
    if 'reproduction_archive' in sf:
        guest_path(sf['reproduction_archive'], 'scenarioforge.reproduction_archive')
    seen = set()
    for phase in ('prepare', 'artifacts'):
        cfg.setdefault(phase, [])
        if not isinstance(cfg[phase], list):
            raise ValueError(phase + ' must be a list')
        for command in cfg[phase]:
            job(command, backend)
            if command['id'] in seen:
                raise ValueError('Duplicate guest command ID')
            seen.add(command['id'])
    cfg.setdefault('collect', {})
    if not isinstance(cfg['collect'], dict):
        raise ValueError('collect must map condition IDs to guest artifact paths')
    conditions = {c['id']: c for c in runtime['conditions']}
    for name, assets in cfg['collect'].items():
        if name not in conditions:
            raise ValueError('Unknown collect condition: ' + name)
        ev.fields(assets, ['catalog', 'guidance_files'], [], 'collect condition')
        if 'catalog' in assets:
            guest_path(assets['catalog'], 'collected catalog')
        if 'guidance_files' in assets:
            if not isinstance(assets['guidance_files'], list):
                raise ValueError('collected guidance_files must be a list')
            for item in assets['guidance_files']:
                guest_path(item, 'collected guidance')
    cfg.setdefault('max_readiness_age_seconds', 3600)
    ev.positive(cfg['max_readiness_age_seconds'], 'max_readiness_age_seconds')
    # Hash and subsequently freeze local inputs. Collected artifacts are frozen
    # after guest preparation; their original template paths are placeholders.
    files = {}
    validation = deepcopy(runtime)
    with tempfile.TemporaryDirectory() as temporary:
        empty = Path(temporary) / 'catalog.json'
        empty.write_text('{"tools": []}')
        for original, condition in zip(runtime['conditions'], validation['conditions']):
            assets = cfg['collect'].get(condition['id'], {})
            if 'catalog' in assets:
                condition['catalog'] = str(empty)
            else:
                files[original['catalog']] = Path(original['catalog']).read_text()
            if 'guidance_files' in assets:
                condition['guidance_files'] = []
            else:
                for item in original['guidance_files']:
                    files[item] = Path(item).read_text()
        validation.pop('suite', None)
        validation['tasks'] = [dict(id='validation', prompt='Schema validation only', family='validation',
                                    split='development', scenario_id='validation',
                                    verifier={'type': 'contains_all', 'expected': ['validation']})]
        check = Path(temporary) / 'runtime.yaml'
        check.write_text(yaml.safe_dump(validation))
        ev.resolve(check)
    source = {p.name: p.read_text() for p in sorted(Path(__file__).parent.glob('*.py'))}
    identity = ev.digest({'workflow': cfg, 'runtime': runtime, 'files': files,
                          'orchestrator_source': source, 'evaluator_source': ev.source_identity(None)})
    return cfg, runtime, files, identity
