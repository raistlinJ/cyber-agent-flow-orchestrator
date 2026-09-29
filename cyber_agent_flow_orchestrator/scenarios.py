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


EVALUATION_LIMITS = dict(repetitions=(1, 100), max_turns=(1, 1000), wall_seconds=(1, 86400),
                         tool_timeout=(1, 3600), context_window=(1, 1000000))


def evaluation_settings(value):
    if not isinstance(value, dict) or set(value) != set(EVALUATION_LIMITS):
        raise SampleRequestError('Supply repetitions, max_turns, wall_seconds, tool_timeout and context_window')
    for name, (minimum, maximum) in EVALUATION_LIMITS.items():
        if type(value[name]) is not int or not minimum <= value[name] <= maximum:
            raise SampleRequestError(f'{name} must be an integer from {minimum} to {maximum}')
    return dict(value)


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
        for item in result['items']:
            token = Path(item['path']).parent.name.removeprefix('caf-upload-')
            if re.fullmatch('[0-9a-f]{32}', token):
                metadata = workspace.path / 'uploads' / token / 'metadata.json'
                if metadata.is_file() and not metadata.is_symlink():
                    saved = ev.read_json(metadata)
                    if saved['path'] == item['path']:
                        item['upload'] = saved
        path = workspace.path / 'scenario-catalogue.json'
        private_file(path, json.dumps(dict(result, vmid=vmid, repo=repo, roots=roots)).encode(), replace=path.exists())
        return dict(result, vmid=vmid)

    def upload(self, access, content):
        import hashlib
        import uuid
        from .scenario_upload import validate
        try:
            validate(content)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise SampleRequestError(str(exc)) from None
        workspace = Workspace(self.root, access.username)
        vmid = workspace.roles()['scenarioforge']
        if vmid is None:
            raise SampleRequestError('Select and save a ScenarioForge VM first')
        access.require_vm(vmid)
        repo, roots = self.settings()
        token = uuid.uuid4().hex
        with self.samples.lock, submission_lock(workspace), authorized_operations(access.qm):
            remote = guest(self.runtime['backend'])
            prepared = remote.call(vmid, 'upload-start', repo=repo, token=token)
            ev.GuestAgent(self.runtime['backend']).put(vmid, prepared['path'], content)
            try:
                result = remote.call(vmid, 'upload-import', repo=repo, token=token,
                    sha256=hashlib.sha256(content).hexdigest(), user=self.cfg['scenarioforge'].get('user', 'scenarioforge'),
                    timeout=120)
            except ValueError as exc:
                raise SampleRequestError(clean(str(exc))) from None
            access.current()
            directory = private_directory(workspace.path / 'uploads' / token)
            metadata = dict(token=token, sha256=hashlib.sha256(content).hexdigest(),
                            kind=result['kind'], fidelity=result['fidelity'], path=result['path'])
            private_file(directory / 'source', content)
            private_file(directory / 'metadata.json', json.dumps(metadata).encode())
            for item in result['items']:
                item['upload'] = metadata
            # Store exactly the imported choices so creation never trusts browser paths.
            roots = list(dict.fromkeys([*roots, result['path']]))
            path = workspace.path / 'scenario-catalogue.json'
            private_file(path, json.dumps(dict(result, vmid=vmid, repo=repo, roots=roots)).encode(), replace=path.exists())
        return dict(result, vmid=vmid, roots=roots, truncated=False)

    def create(self, access, selection_id, request_id, allowed_targets, disallowed_targets, evaluation=None, _sample_id=None):
        if not re.fullmatch('[0-9a-f]{32}', request_id) or not re.fullmatch('[0-9a-f]{64}', selection_id):
            raise SampleRequestError('Choose a listed scenario and supply a valid request ID')
        policy = dict(allow=networks(allowed_targets, True), disallow=networks(disallowed_targets))
        overrides = evaluation_settings(evaluation) if evaluation is not None else None
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
            if not item or (not item['resolved_chain'] and _sample_id is None):
                raise SampleRequestError('Choose a scenario with a saved resolved Flow chain')
            sf = self.cfg['scenarioforge']
            with authorized_operations(access.qm):
                captured = guest(self.runtime['backend']).call(roles['scenarioforge'], 'snapshot',
                    roots=catalogue['roots'], repo=catalogue['repo'], path=item['path'],
                    selection_id=selection_id, token=request_id, user=sf.get('user', 'scenarioforge'),
                    allow_unresolved=_sample_id is not None, timeout=40)
            captured.update(snapshot_token=request_id, repo=catalogue['repo'])
            if item.get('upload'):
                captured['upload'] = item['upload']
            runtime = deepcopy(self.runtime)
            runtime['execution']['network_policy'] = policy
            runtime['backend']['before_trial'] = []
            runtime['repetitions'] = 1
            if overrides:
                runtime['repetitions'] = overrides['repetitions']
                runtime['execution'].update({key: value for key, value in overrides.items() if key != 'repetitions'})
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
            if _sample_id is not None:
                self.configure_sample(_sample_id, cfg, runtime, captured, roles, request_id)
            source = workspace.materialize(cfg, runtime, access, run_id)
            saved_runtime = yaml.safe_load((source.parent / 'runtime.yaml').read_text())
            private_file(source.parent / 'baseline.json', baseline.read_bytes())
            saved_runtime['conditions'][0]['catalog'] = str(source.parent / 'baseline.json')
            if len(saved_runtime['conditions']) > 1:
                helper = baseline.with_name('with-http-helper.json')
                private_file(source.parent / 'with-http-helper.json', helper.read_bytes())
                saved_runtime['conditions'][1]['catalog'] = str(source.parent / 'with-http-helper.json')
            private_file(source.parent / 'runtime.yaml', yaml.safe_dump(saved_runtime).encode(), replace=True)
            cfg, runtime, _, identity = load(source)
            private_directory(output)
            private_directory(output / 'inputs')
            private_file(output / 'inputs/source-selection.json', json.dumps(captured).encode())
            self.capture_upload(workspace, captured, output)
            ev.write_json(output / 'workflow.json', self.record(cfg, runtime, identity, captured, request_id))
        return dict(run_id=run_id, status='ready')

    def create_sample(self, access, sample_id, request_id):
        from .samples import CATALOG
        if sample_id not in self.samples.enabled or not re.fullmatch('[0-9a-f]{32}', request_id):
            raise SampleRequestError('Choose an enabled sample and valid request ID')
        workspace = Workspace(self.root, access.username)
        access.current()
        roles = workspace.roles()
        if any(roles[role] is None for role in ('scenarioforge', 'participant', 'core')):
            raise SampleRequestError('Select and save ScenarioForge, participant and CoreVM roles before creating a sample')
        access.require_vms(roles.values())
        existing = workspace.run_path('scenario-' + request_id)
        if existing.exists():
            record = ev.read_json(existing / 'workflow.json')
            if record.get('sample_id') != sample_id:
                raise SampleRequestError('Request ID belongs to another experiment')
            return dict(run_id=existing.name, status=record['status'])
        bundle = Path(__file__).with_name('static') / ('demo-' + sample_id + '.zip')
        imported = self.upload(access, bundle.read_bytes())
        item = imported['items'][0]
        return self.create(access, item['id'], request_id,
            ','.join(self.runtime['execution']['network_policy']['allow']),
            ','.join(self.runtime['execution']['network_policy']['disallow']), _sample_id=sample_id)

    def configure_sample(self, sample_id, cfg, runtime, captured, roles, token):
        from .samples import CATALOG
        item = CATALOG[sample_id]
        captured['sample_id'] = sample_id
        runtime['repetitions'] = 1 if sample_id == 'smoke' else 3
        runtime['execution'].update(max_turns=item['max_turns'], wall_seconds=item['wall_seconds'], tool_timeout=30)
        if sample_id == 'tools-vs-helper':
            runtime['conditions'].append(dict(id='added-helper',
                catalog=str(Path(__file__).with_name('sample_data') / 'with-http-helper.json'),
                tools=['nmap', 'curl', 'python3', 'http_flag_walk'], guidance_files=[]))
        sf = cfg['scenarioforge']
        destination = sf['repo'] + '/outputs/caf-demo-runs/' + token + '/scenario.xml'
        options = dict(sample_id=sample_id, source=captured['snapshot_path'],
                       destination=destination, scenario=captured['scenario'],
                       artifacts=sf['repo'] + '/outputs/flag_generators_runs/caf-demo-' + token)
        sf['xml'] = destination
        cfg['prepare'] = [dict(id='fixed-demo-xml', vmid=roles['scenarioforge'],
            argv=[sf['python'], '-c', Path(__file__).with_name('demo_prepare.py').read_text(), json.dumps(options)],
            timeout_seconds=sf['timeout_seconds'], user=sf['user'], cwd=sf['repo'],
            **({'environment_file': sf['environment_file']} if sf.get('environment_file') else {}))]

    def capture_upload(self, workspace, selection, output):
        import hashlib
        upload = selection.get('upload')
        if not upload:
            return
        if not re.fullmatch('[0-9a-f]{32}', upload['token']):
            raise SampleRequestError('Invalid saved upload')
        from .run_artifacts import open_saved
        with open_saved(workspace.path, 'uploads/' + upload['token'] + '/source') as stream:
            content = stream.read()
        if hashlib.sha256(content).hexdigest() != upload['sha256']:
            raise SampleRequestError('Saved uploaded source changed')
        extension = '.zip' if upload['kind'] == 'reproduction-bundle' else '.xml'
        private_file(output / ('inputs/uploaded-source' + extension), content)

    def record(self, cfg, runtime, identity, captured, token):
        return dict(version=1, workflow=cfg, runtime=runtime, workflow_hash=identity,
                    token='scenario-' + token, scenario_experiment=captured, sample_id=captured.get('sample_id'), status='ready', phase='ready',
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
                if len(saved_runtime['conditions']) > 1:
                    saved_runtime['conditions'][1]['catalog'] = str(destination / 'with-http-helper.json')
                private_file(destination / 'runtime.yaml', yaml.safe_dump(saved_runtime).encode(), replace=True)
                source = destination / 'workflow.yaml'
                cfg, runtime, _, identity = load(source)
                output, run_id = new_output, new_id
                private_directory(output)
                private_directory(output / 'inputs')
                private_file(output / 'inputs/source-selection.json', json.dumps(record['scenario_experiment']).encode())
                self.capture_upload(workspace, record['scenario_experiment'], output)
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
                    record.update(started_at=now(), status='preparing', phase='verifying', message='Verifying the saved scenario XML on ScenarioForge')
                    from .workflow import Workflow
                    from .workflow_progress import step
                    observer = Workflow(output, record, agent=guest(runtime['backend']), progress=None)
                    with step(observer, 'snapshot'):
                        observer.agent.call(runtime['backend']['app_vmid'], 'check',
                            token=selection['snapshot_token'], repo=selection['repo'], sha256=selection['sha256'])
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
