"""Import an owned Fusion provisioner state without evaluating shell code."""
from importlib.resources import files
from pathlib import Path
import json
import os
import shlex
import yaml
from .auth import private_file


def read_assignments(path):
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Fusion state/credentials must be owned by you and chmod 600')
    values={}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        key, value=line.split('=',1)
        tokens=shlex.split(value)
        if len(tokens)>1:
            raise ValueError('Invalid Fusion state assignment')
        values[key]=tokens[0] if tokens else ''
    return values


def prepare(state_dir, output):
    state_dir=Path(state_dir).expanduser().resolve()
    state=read_assignments(state_dir/'state.env')
    credentials=read_assignments(state_dir/'credentials.env')
    if state.get('INSTALLER_OWNER') != 'scenarioforge-vmware-fusion-v1':
        raise ValueError('State is not from the Fusion provisioner')
    if state.get('CYBER_AGENT_FLOW') != '1' or state.get('INSTALL_COMPLETE') != '1':
        raise ValueError('Finish provisioning the Fusion lab with cyber_agent_flow=true first')
    output=Path(output).expanduser().absolute()
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    rows={}
    for role,vmid,user in [('CORE',9401,'corevm'),('APP',9402,'scenarioforge'),('PARTICIPANT',9403,'participant')]:
        rows[str(vmid)]=dict(vmx=state[role+'_VMX'],name=state[role+'_NAME'],username=user,password=credentials[role+'_VM_PASSWORD'])
    inventory_file=output/'fusion-inventory.json'
    private_file(inventory_file,json.dumps(dict(version=1,vms=rows),indent=2).encode())
    from cyber_agent_flow_eval.fusion import inventory, vm_lock_path
    inventory(inventory_file)
    templates=files('cyber_agent_flow_orchestrator').joinpath('defaults')
    runtime=yaml.safe_load(templates.joinpath('runtime.yaml').read_text())
    runtime['backend'].update(type='fusion',inventory_file=str(inventory_file),command_timeout=60,poll_seconds=1)
    runtime['execution']['target_lock']=str(vm_lock_path(runtime['backend'],9401).with_suffix('.target.lock'))
    if state.get('LLM_PROVIDER_TYPE'):runtime['model']['provider']=state['LLM_PROVIDER_TYPE']
    if state.get('LLM_PROVIDER_URL'):runtime['model']['url']=state['LLM_PROVIDER_URL']
    if state.get('LLM_MODEL'):runtime['model']['name']=state['LLM_MODEL']
    workflow=yaml.safe_load(templates.joinpath('workflow.yaml').read_text())
    workflow['monitoring']['initial_roles']=dict(core=9401,scenarioforge=9402,participant=9403)
    web=dict(version=1,listen='127.0.0.1',port=8443,public_url='https://localhost:8443',
             certificate=str(output.parent/'certs/cert.pem'),private_key=str(output.parent/'certs/key.pem'),
             users_file=str(output/'users.json'),auth=dict(provider='fusion',inventory_file=str(inventory_file)),
             samples=['smoke','tools-vs-helper'],updates=False)
    for name,data in [('runtime.yaml',runtime),('workflow.yaml',workflow),('web.yaml',web)]:
        private_file(output/name,yaml.safe_dump(data,sort_keys=False).encode())
    private_file(output/'catalogs/baseline.json',templates.joinpath('catalogs/baseline.json').read_bytes())
    return dict(workflow=str(output/'workflow.yaml'),web_config=str(output/'web.yaml'),users_file=str(output/'users.json'))
