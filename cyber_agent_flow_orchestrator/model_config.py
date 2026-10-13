"""Owner-scoped model preferences and fixed VM configuration exchange."""
from cyber_agent_flow_eval.fusion import vm_lock_path

from copy import deepcopy
import hashlib
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

    def network_scope(self, access):
        access.current()
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()['participant']
        if vmid is None:
            raise ModelConfigError('Select and save a participant VM first')
        access.require_vm(vmid)
        runtime = apply_model(workspace, self.runtime, vmid)
        agent = ev.GuestAgent(runtime['backend'], authorize=access.qm)
        agent.script = Path(__file__).with_name('guest_model_config.py').read_text()
        result = agent.call(vmid, 'model_config', role='participant', action='scope',
                            root=runtime['engine']['path'], url=runtime['model']['url'])
        access.current()
        return dict(result, vmid=vmid)

    def exchange(self, access, data):
        if not isinstance(data, dict) or set(data) - {'role', 'action', 'token', 'settings', 'api_key'}:
            raise ModelConfigError('Invalid model configuration request')
        role, action = data.get('role'), data.get('action')
        if role not in ('participant', 'scenarioforge') or action not in ('read', 'stage', 'save', 'use', 'tools') or (action in ('stage', 'use', 'tools') and role != 'participant'):
            raise ModelConfigError('Unsupported model configuration action')
        if action not in ('read', 'tools') and not self.enabled:
            raise ModelConfigError('Application configuration writes are disabled by this server')
        expected = {'role', 'action'} if action in ('read', 'tools') else {'role', 'action', 'token', 'settings'} if action in ('stage', 'save') else {'role', 'action', 'token'}
        if set(data) - {'api_key'} != expected or ('api_key' in data and action != 'save'):
            raise ModelConfigError('Invalid model configuration fields')
        if action in ('stage', 'save'):
            try: validate(role, data['settings'])
            except ValueError as exc: raise ModelConfigError(str(exc)) from None
            if 'api_key' in data and (not isinstance(data['api_key'], str) or len(data['api_key']) > 8192):
                raise ModelConfigError('Invalid API key')
        access.current()
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()[role]
        if vmid is None: raise ModelConfigError('Save a VM selection for this application first')
        access.require_vm(vmid)
        if action not in ('read', 'tools') and self.runtime['backend']['type'] != 'fusion': require_maintenance(access, self.group)
        definition = next(item for item in definitions(self.cfg, self.runtime) if item['role'] == role)
        draft_path = workspace.path / f'model-draft-{role}.json'
        if draft_path.is_symlink(): raise ModelConfigError('Invalid model draft file')
        try:
            with ev.lease(workspace.path / '.model-config.lock'):
                args = dict(role=role, action=action, root=definition['root'])
                if action not in ('read', 'tools'):
                    draft = ev.read_json(draft_path) if draft_path.is_file() else {}
                    if (not isinstance(data.get('token'), str) or not re.fullmatch('[0-9a-f]{32}', data['token']) or
                            draft.get('token') != data['token'] or draft.get('vmid') != vmid or draft.get('root') != definition['root']):
                        raise ModelConfigError('VM selection or configuration draft changed. Pull the configuration again.')
                    args['revision'] = draft['revision']
                    if role == 'participant':
                        args['route_helper_source'] = Path(__file__).with_name('guest_llm_route.py').read_text()
                    if action == 'stage':
                        # Pending settings are not active defaults. Never persist an API key here.
                        target = workspace.path / f'model-pending-{vmid}.json'
                        if target.is_symlink(): raise ModelConfigError('Invalid pending model file')
                        private_file(target, json.dumps(dict(settings=data['settings'], vmid=vmid,
                            root=definition['root'], revision=draft['revision'])).encode(), replace=target.exists())
                        return dict(action=action, role=role, vmid=vmid, token=data['token'], local_saved=True)
                    if action == 'save':
                        args['settings'] = data['settings']
                        if 'api_key' in data: args['api_key'] = data['api_key']
                def authorize(argv):
                    access.qm(argv, required_group=self.group if action not in ('read', 'tools') and self.runtime['backend']['type'] != 'fusion' else None)
                agent = ev.GuestAgent(self.runtime['backend'], authorize=authorize)
                agent.script = Path(__file__).with_name('guest_model_config.py').read_text()
                # write uses qm stdin, so a replacement key never enters process argv.
                if action in ('read', 'tools'):
                    result = agent.call(vmid, 'model_config', **args)
                else:
                    with ev.lease(vm_lock_path(self.runtime['backend'], vmid)):
                        result = agent.call(vmid, 'write', timeout=120, **args)
                access.current()
                if action == 'tools':
                    catalog = result.pop('catalog', None)
                    assets = result.pop('assets', [])
                    summaries = result.get('tools')
                    if (not isinstance(catalog, dict) or not isinstance(catalog.get('tools'), list)
                            or not isinstance(summaries, list) or not summaries):
                        raise ModelConfigError('Participant VM returned an invalid tool catalog')
                    names = [item.get('name') for item in summaries if isinstance(item, dict)]
                    if (len(names) != len(summaries) or len(set(names)) != len(names)
                            or any(not isinstance(name, str) or not name for name in names)):
                        raise ModelConfigError('Participant VM returned invalid tool metadata')
                    if (not isinstance(assets, list) or len(assets) > 500
                            or any(not isinstance(item, dict) or set(item) - {
                                'id', 'kind', 'label', 'name', 'description', 'files',
                                'bytes', 'content', 'sha256'} for item in assets)):
                        raise ModelConfigError('Participant VM returned invalid CAF guidance metadata')
                    asset_ids = []
                    for item in assets:
                        asset_id, body = item.get('id'), item.get('content')
                        if (not isinstance(asset_id, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,160}', asset_id)
                                or asset_id in asset_ids or not isinstance(body, str)
                                or len(body.encode()) > 256 * 1024
                                or item.get('sha256') != hashlib.sha256(body.encode()).hexdigest()):
                            raise ModelConfigError('Participant VM returned invalid CAF guidance content')
                        asset_ids.append(asset_id)
                    if sum(len(item['content'].encode()) for item in assets) > 2 * 1024 * 1024:
                        raise ModelConfigError('Participant VM returned too much CAF guidance content')
                    content = (json.dumps(catalog, indent=2, sort_keys=True) + '\n').encode()
                    digest = hashlib.sha256(content).hexdigest()
                    catalog_path = workspace.path / 'participant-tools-catalog.json'
                    metadata_path = workspace.path / 'participant-tools.json'
                    assets_path = workspace.path / 'participant-guidance-assets.json'
                    private_file(catalog_path, content, replace=catalog_path.exists())
                    asset_content = (json.dumps(assets, indent=2, sort_keys=True) + '\n').encode()
                    asset_digest = hashlib.sha256(asset_content).hexdigest()
                    private_file(assets_path, asset_content, replace=assets_path.exists())
                    asset_summaries = [{key: value for key, value in item.items() if key != 'content'} for item in assets]
                    metadata = dict(vmid=vmid, root=definition['root'], source='participant-vm',
                                    catalog=str(catalog_path), catalog_sha256=digest,
                                    source_path=result.get('source_path'), tools=summaries,
                                    assets=str(assets_path), assets_sha256=asset_digest,
                                    guidance_assets=asset_summaries)
                    private_file(metadata_path, json.dumps(metadata, indent=2).encode(), replace=metadata_path.exists())
                    return dict(action=action, role=role, vmid=vmid, source='participant-vm',
                                catalog_sha256=digest, source_path=result.get('source_path'), tools=summaries,
                                guidance_assets=asset_summaries)
                token = uuid.uuid4().hex
                if action == 'read' and draft_path.is_file():
                    previous = ev.read_json(draft_path)
                    if (previous.get('vmid') == vmid and previous.get('root') == definition['root']
                            and previous.get('revision') == result['revision']):
                        token = previous['token']
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
