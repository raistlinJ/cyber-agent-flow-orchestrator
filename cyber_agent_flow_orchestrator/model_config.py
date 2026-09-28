"""Owner-scoped model preferences and fixed VM configuration exchange."""
from copy import deepcopy
import json
from pathlib import Path
import re
import uuid

from cyber_agent_flow_eval import integration as ev
from .auth import private_file
from .guest_model_config import validate
from .monitor import definitions
from .updates import require_maintenance
from .workspaces import Workspace


class ModelConfigError(ValueError):
    pass


def apply_model(workspace, runtime, vmid):
    runtime = deepcopy(runtime)
    path = workspace.path / f'model-{vmid}.json'
    if path.is_symlink(): raise ModelConfigError('Invalid model preference file')
    if path.is_file():
        saved = ev.read_json(path)
        if saved['engine_path'] == runtime['engine']['path']:
            runtime['model'] = saved['model']
            if saved.get('environment_file'):
                runtime['backend']['environment_file'] = saved['environment_file']
    return runtime


class ModelConfigs:
    def __init__(self, cfg, runtime, root, group='caf-maintainers', enabled=True):
        self.cfg, self.runtime, self.root, self.group = cfg, runtime, root, group
        self.enabled = enabled

    def exchange(self, access, data):
        if not isinstance(data, dict) or set(data) - {'role', 'action', 'token', 'settings', 'api_key'}:
            raise ModelConfigError('Invalid model configuration request')
        role, action = data.get('role'), data.get('action')
        if role not in ('participant', 'scenarioforge') or action not in ('read', 'save', 'use') or (action == 'use' and role != 'participant'):
            raise ModelConfigError('Unsupported model configuration action')
        if action != 'read' and not self.enabled:
            raise ModelConfigError('Application configuration writes are disabled by this server')
        expected = {'role', 'action'} if action == 'read' else {'role', 'action', 'token', 'settings'} if action == 'save' else {'role', 'action', 'token'}
        if set(data) - {'api_key'} != expected or ('api_key' in data and action != 'save'):
            raise ModelConfigError('Invalid model configuration fields')
        if action == 'save':
            try: validate(role, data['settings'])
            except ValueError as exc: raise ModelConfigError(str(exc)) from None
            if 'api_key' in data and (not isinstance(data['api_key'], str) or len(data['api_key']) > 8192):
                raise ModelConfigError('Invalid API key')
        access.current()
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()[role]
        if vmid is None: raise ModelConfigError('Save a VM selection for this application first')
        access.require_vm(vmid)
        if action != 'read': require_maintenance(access, self.group)
        definition = next(item for item in definitions(self.cfg, self.runtime) if item['role'] == role)
        draft_path = workspace.path / f'model-draft-{role}.json'
        if draft_path.is_symlink(): raise ModelConfigError('Invalid model draft file')
        try:
            with ev.lease(workspace.path / '.model-config.lock'):
                args = dict(role=role, action=action, root=definition['root'])
                if action != 'read':
                    draft = ev.read_json(draft_path) if draft_path.is_file() else {}
                    if (not isinstance(data.get('token'), str) or not re.fullmatch('[0-9a-f]{32}', data['token']) or
                            draft.get('token') != data['token'] or draft.get('vmid') != vmid or draft.get('root') != definition['root']):
                        raise ModelConfigError('VM selection or configuration draft changed. Pull the configuration again.')
                    args['revision'] = draft['revision']
                    if action == 'save':
                        args['settings'] = data['settings']
                        if 'api_key' in data: args['api_key'] = data['api_key']
                def authorize(argv):
                    access.qm(argv, required_group=self.group if action != 'read' else None)
                agent = ev.GuestAgent(self.runtime['backend'], authorize=authorize)
                agent.script = Path(__file__).with_name('guest_model_config.py').read_text()
                # write uses qm stdin, so a replacement key never enters process argv.
                if action == 'read':
                    result = agent.call(vmid, 'model_config', **args)
                else:
                    with ev.lease(f'/var/lock/cyber-agent-flow-eval-vm-{vmid}.lock'):
                        result = agent.call(vmid, 'write', **args)
                access.current()
                token = uuid.uuid4().hex
                private_file(draft_path, json.dumps(dict(token=token, vmid=vmid, root=definition['root'], revision=result['revision'])).encode(), replace=draft_path.exists())
                if role == 'participant' and action in ('save', 'use'):
                    values = validate(role, result['settings'])
                    model = dict(provider=values['provider'], url=values['url'], name=values['model'], ssl_verify=values['ssl_verify'], api_key_env=result['api_key_env'])
                    saved = dict(model=model, engine_path=self.runtime['engine']['path'], environment_file=result.get('environment_file'))
                    target = workspace.path / f'model-{vmid}.json'
                    if target.is_symlink(): raise ModelConfigError('Invalid model preference file')
                    private_file(target, json.dumps(saved).encode(), replace=target.exists())
                return dict(result, token=token, vmid=vmid, role=role, action=action)
        except ModelConfigError:
            raise
        except (ValueError, OSError, KeyError) as exc:
            if str(exc).startswith('Already locked:'):
                raise ModelConfigError('This VM or configuration is in use. Wait for the current operation to finish.') from None
            raise ModelConfigError(str(exc)) from None
