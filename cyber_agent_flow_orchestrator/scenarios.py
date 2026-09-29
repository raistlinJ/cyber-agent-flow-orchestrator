"""Saved ScenarioForge XML experiments using the existing host workflow."""
from copy import deepcopy
from pathlib import Path
import ipaddress
import json
import re
import shutil

import yaml
from cyber_agent_flow_eval import integration as ev, reporting
from cyber_agent_flow_eval.proxmox import authorized_operations
from .auth import private_file
from .config import load
from .diagnostics import clean
from .samples import SampleBusy, SampleCancelled, SampleRequestError, now, submission_lock
from .workspaces import Workspace, private_directory, workflow_vmids


def guest(backend):
    agent = ev.GuestAgent(backend)
    agent.script = Path(__file__).with_name('scenario_guest.py').read_text()
    return agent


def networks(text, required=False):
    if not isinstance(text, str) or len(text) > 4096:
        raise SampleRequestError('Enter IP addresses or CIDR networks separated by commas')
    parts = [value.strip() for value in text.split(',') if value.strip()]
    if len(parts) > 64 or (required and not parts):
        raise SampleRequestError('Supply at least one allowed target network (up to 64)')
    try:
        return list(dict.fromkeys(str(ipaddress.ip_network(value, strict=False)) for value in parts))
    except ValueError:
        raise SampleRequestError('Targets must be IP addresses or CIDR networks') from None


class ScenarioExperiments:
    def __init__(self, cfg, runtime, root, samples):
        self.cfg, self.runtime, self.root, self.samples = cfg, runtime, root, samples

    def settings(self):
        sf = self.cfg['scenarioforge']
        repo = sf.get('repo', self.cfg.get('monitoring', {}).get('scenarioforge_path', '/opt/scenarioforge'))
        roots = self.cfg.get('monitoring', {}).get('scenarioforge_xml_roots') or [
            repo + '/uploads', repo + '/outputs', '/tmp/scenarioforge-eval-out/webui-xml']
        if sf.get('xml') and sf['xml'] not in roots:
            roots = [*roots, sf['xml']]
        return repo, roots

    def catalogue(self, access, query=''):
        if not isinstance(query, str) or len(query) > 160:
            raise SampleRequestError('Search text must be at most 160 characters')
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()['scenarioforge']
        if vmid is None:
            raise SampleRequestError('Select and save a ScenarioForge VM on Lab setup first')
        access.require_vm(vmid)
        repo, roots = self.settings()
        with authorized_operations(access.qm):
            result = guest(self.runtime['backend']).call(vmid, 'list', roots=roots, query=query, timeout=30)
        access.current()
        path = workspace.path / 'scenario-catalogue.json'
        private_file(path, json.dumps(dict(result, vmid=vmid, repo=repo, roots=roots)).encode(), replace=path.exists())
        return dict(result, vmid=vmid)

    def create(self, access, selection_id, request_id, allowed_targets, disallowed_targets):
        if not re.fullmatch('[0-9a-f]{32}', request_id) or not re.fullmatch('[0-9a-f]{64}', selection_id):
            raise SampleRequestError('Choose a listed scenario and supply a valid request ID')
        policy = dict(allow=networks(allowed_targets, True), disallow=networks(disallowed_targets))
        access.current()
        workspace = Workspace(self.root, access.username)
        run_id = 'scenario-' + request_id
        output = workspace.run_path(run_id)
        with self.samples.lock, submission_lock(workspace):
            if output.exists():
                record = ev.read_json(output / 'workflow.json')
                if record.get('scenario_experiment', {}).get('id') != selection_id:
                    raise SampleRequestError('Request ID belongs to another scenario')
                return dict(run_id=run_id, status=record['status'])
            roles = workspace.roles()
            if any(roles[role] is None for role in ('scenarioforge', 'participant')):
                raise SampleRequestError('Select and save ScenarioForge and participant VMs first')
            access.require_vms(v for v in roles.values() if v is not None)
            try:
                catalogue = ev.read_json(workspace.path / 'scenario-catalogue.json')
            except FileNotFoundError:
                raise SampleRequestError('Load scenarios from the VM first') from None
            if catalogue['vmid'] != roles['scenarioforge']:
                raise SampleRequestError('ScenarioForge VM changed; reload its scenarios')
            item = next((row for row in catalogue['items'] if row['id'] == selection_id), None)
            if not item or not item['resolved_chain']:
                raise SampleRequestError('Choose a scenario with a saved resolved Flow chain')
            sf = self.cfg['scenarioforge']
            with authorized_operations(access.qm):
                captured = guest(self.runtime['backend']).call(roles['scenarioforge'], 'snapshot',
                    roots=catalogue['roots'], repo=catalogue['repo'], path=item['path'],
                    selection_id=selection_id, token=request_id, user=sf.get('user', 'scenarioforge'), timeout=40)
            captured.update(snapshot_token=request_id, repo=catalogue['repo'])
            runtime = deepcopy(self.runtime)
            runtime['execution']['network_policy'] = policy
            runtime['backend']['before_trial'] = []
            runtime['repetitions'] = 1
            baseline = Path(__file__).with_name('sample_data') / 'baseline.json'
            runtime['conditions'] = [dict(id='baseline', catalog=str(baseline), tools=['nmap', 'curl', 'python3'], guidance_files=[])]
            cfg = dict(version=1, id='scenario-' + request_id, runtime='runtime.yaml',
                scenarioforge=dict(mode='execute', xml=captured['snapshot_path'], scenario=captured['scenario'],
                    suite_id='scenario-' + request_id, output_root=catalogue['repo'] + '/outputs/caf-orchestrator-evaluations',
                    repo=catalogue['repo'], python=sf.get('python', catalogue['repo'] + '/.venv/bin/python'),
                    user=sf.get('user', 'scenarioforge'), timeout_seconds=sf.get('timeout_seconds', 1800),
                    **({'environment_file': sf['environment_file']} if sf.get('environment_file') else {})),
                prepare=[], artifacts=[], collect={}, monitoring=deepcopy(self.cfg.get('monitoring', {})),
                max_readiness_age_seconds=self.cfg['max_readiness_age_seconds'])
            source = workspace.materialize(cfg, runtime, access, run_id)
            saved_runtime = yaml.safe_load((source.parent / 'runtime.yaml').read_text())
            private_file(source.parent / 'baseline.json', baseline.read_bytes())
            saved_runtime['conditions'][0]['catalog'] = str(source.parent / 'baseline.json')
            private_file(source.parent / 'runtime.yaml', yaml.safe_dump(saved_runtime).encode(), replace=True)
            cfg, runtime, _, identity = load(source)
            private_directory(output)
            private_directory(output / 'inputs')
            private_file(output / 'inputs/source-selection.json', json.dumps(captured).encode())
            ev.write_json(output / 'workflow.json', self.record(cfg, runtime, identity, captured, request_id))
        return dict(run_id=run_id, status='ready')

    def record(self, cfg, runtime, identity, captured, token):
        return dict(version=1, workflow=cfg, runtime=runtime, workflow_hash=identity,
                    token='scenario-' + token, scenario_experiment=captured, status='ready', phase='ready',
                    created_at=now(), updated_at=now(), stages={}, events=[],
                    message='Ready. Run deploys the saved scenario and evaluates it with baseline tools.')

    def run_saved(self, access, run_id, request_id):
        if not re.fullmatch('[0-9a-f]{32}', request_id):
            raise SampleRequestError('Invalid request ID')
        access.current()
        workspace = Workspace(self.root, access.username)
        output = workspace.run_path(run_id)
        with self.samples.lock, submission_lock(workspace):
            record = ev.read_json(output / 'workflow.json')
            if not record.get('scenario_experiment'):
                raise SampleRequestError('Not a saved scenario experiment')
            if record.get('launch_request_id') == request_id:
                return dict(run_id=run_id, status=record['status'])
            self.samples.jobs = {name: future for name, future in self.samples.jobs.items() if not future.done()}
            if self.samples.stopping.is_set() or access.username in self.samples.jobs or len(self.samples.jobs) >= 2:
                raise SampleBusy('An experiment is already running or all workers are busy')
            from .service import list_runs
            if any(row.get('coordinator_active') or row.get('recorded_status') in ('queued', 'stopping') for row in list_runs(workspace.runs)):
                raise SampleBusy('Only one experiment can run at a time for this account')
            source = workspace.path / 'inputs' / run_id / 'workflow.yaml'
            if record['status'] != 'ready':
                new_id = 'scenario-' + request_id
                new_output = workspace.run_path(new_id)
                if new_output.exists():
                    previous = ev.read_json(new_output / 'workflow.json')
                    if previous.get('launch_request_id') == request_id:
                        return dict(run_id=new_id, status=previous['status'])
                    raise SampleRequestError('Run ID already exists')
                destination = workspace.path / 'inputs' / new_id
                shutil.copytree(source.parent, destination)
                saved_runtime = yaml.safe_load((destination / 'runtime.yaml').read_text())
                saved_runtime['conditions'][0]['catalog'] = str(destination / 'baseline.json')
                private_file(destination / 'runtime.yaml', yaml.safe_dump(saved_runtime).encode(), replace=True)
                source = destination / 'workflow.yaml'
                cfg, runtime, _, identity = load(source)
                output, run_id = new_output, new_id
                private_directory(output)
                private_directory(output / 'inputs')
                private_file(output / 'inputs/source-selection.json', json.dumps(record['scenario_experiment']).encode())
                record = self.record(cfg, runtime, identity, record['scenario_experiment'], request_id)
            for vmid in workflow_vmids(record['workflow'], record['runtime']):
                access.require_vm(vmid)
            record.update(status='queued', phase='queued', launch_request_id=request_id, queued_at=now(),
                          message='Waiting to deploy and evaluate the saved scenario')
            ev.write_json(output / 'workflow.json', record)
            self.samples.jobs[access.username] = self.samples.pool.submit(self._run, workspace, source, output, access)
        return dict(run_id=run_id, status='queued')

    def stop(self, access, run_id):
        access.current()
        workspace = Workspace(self.root, access.username)
        output = workspace.run_path(run_id)
        record = ev.read_json(output / 'workflow.json')
        if not record.get('scenario_experiment') or record['status'] not in ('queued', 'preparing', 'evaluating'):
            raise SampleRequestError('This scenario experiment is not running')
        ev.write_json(output / 'stop-request.json', {'requested_at': now()})
        return dict(run_id=run_id, status='stopping')

    def _run(self, workspace, source, output, access):
        def progress(message):
            if (output / 'stop-request.json').is_file():
                raise SampleCancelled('Stopped after the current stage or trial; deployed scenario remains in place')
            if self.samples.stopping.is_set():
                raise InterruptedError('Server is stopping')
        try:
            with ev.lease(workspace.path / '.sample-user.lock'), authorized_operations(access.qm):
                progress('')
                with ev.lease(output / '.workflow.lock'):
                    record = ev.read_json(output / 'workflow.json')
                    runtime, selection = record['runtime'], record['scenario_experiment']
                    guest(runtime['backend']).call(runtime['backend']['app_vmid'], 'check',
                        token=selection['snapshot_token'], repo=selection['repo'], sha256=selection['sha256'])
                    record.update(started_at=now())
                    ev.write_json(output / 'workflow.json', record)
                from .service import run
                run(source, output, resume=True, progress=progress)
        except Exception as exc:
            with ev.lease(output / '.workflow.lock'):
                record = ev.read_json(output / 'workflow.json')
                record.update(status='cancelled' if isinstance(exc, SampleCancelled) else 'interrupted' if isinstance(exc, InterruptedError) else 'failed',
                              error=None if isinstance(exc, SampleCancelled) else clean(str(exc)), message=clean(str(exc)))
                ev.write_json(output / 'workflow.json', record)
        finally:
            with ev.lease(output / '.workflow.lock'):
                record = ev.read_json(output / 'workflow.json')
                record.update(ended_at=now(), updated_at=now())
                if record['status'] in ('completed', 'completed_with_errors'):
                    record.update(message='Scenario evaluation finished; open Results for scores and captured inputs', phase='finished')
                ev.write_json(output / 'workflow.json', record)
