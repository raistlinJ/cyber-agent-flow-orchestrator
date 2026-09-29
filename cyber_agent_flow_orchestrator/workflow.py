"""Durable host workflow; private suite data never goes to guest workers."""
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import uuid
import yaml

from cyber_agent_flow_eval import integration as ev
from .config import load


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def execute_command(sf, backend, token):
    destination = sf['output_root'].rstrip('/') + '/' + token
    return dict(id='deploy', vmid=backend['app_vmid'], user=sf['user'], cwd=sf['repo'],
                timeout_seconds=sf['timeout_seconds'],
                argv=[sf['python'], '-m', 'scenarioforge.cli', 'execute', '--xml', sf['xml'],
                      '--scenario', sf['scenario'], '--evaluation-export', '--evaluation-output-dir',
                      destination, '--suite-id', sf['suite_id'], '--eval-split', sf['split'],
                      *(['--evaluation-tasks', sf['tasks']] if 'tasks' in sf else [])],
                **({'environment_file': sf['environment_file']} if 'environment_file' in sf else {}))


def export_marker(log):
    markers = [line.split('EVALUATION_PACKAGE_JSON:', 1)[1].strip()
               for line in log.splitlines() if line.startswith('EVALUATION_PACKAGE_JSON:')]
    if len(markers) != 1:
        raise ValueError('Expected one ScenarioForge EVALUATION_PACKAGE_JSON marker; inspect deployment log')
    result = json.loads(markers[0])
    if result.get('state') != 'complete' or result.get('readiness_passed') is not True:
        raise ValueError('ScenarioForge export did not pass readiness')
    from cyber_agent_flow_eval.backends import guest_path
    guest_path(result.get('archive'), 'export archive')
    return result


class Workflow:
    def __init__(self, output, journal, agent=None, progress=print):
        self.progress = progress
        self.output = Path(output)
        self.journal = journal
        self.agent = agent or ev.GuestAgent(journal['runtime']['backend'])

    def save(self):
        ev.write_json(self.output / 'workflow.json', self.journal)

    def notify(self, message):
        if self.journal.get('scenario_experiment'):
            self.journal.update(message=message, updated_at=datetime.now(timezone.utc).isoformat())
            self.journal.setdefault('events', []).append(dict(at=self.journal['updated_at'], kind='scenario', message=message))
            self.journal['events'] = self.journal['events'][-100:]
            self.save()
        if self.progress is not None:
            self.progress(message)

    def inventory(self, paths):
        return {str(Path(p).relative_to(self.output)): sha(p) for p in paths}

    def check_files(self):
        for stage in self.journal['stages'].values():
            if stage['status'] == 'completed':
                for name, expected in stage.get('files', {}).items():
                    if sha(self.output / name) != expected:
                        raise ValueError('Frozen workflow file changed: ' + name)

    def recover_jobs(self):
        # Stop all journaled jobs, including starts whose QGA response was lost.
        for stage in self.journal['stages'].values():
            for attempt in stage.get('attempts', []):
                if not attempt.get('stopped'):
                    with ev.lease(f"/var/lock/cyber-agent-flow-eval-vm-{attempt['vmid']}.lock"):
                        self.agent.call(attempt['vmid'], 'stop', unit=attempt['unit'])
                    attempt['stopped'] = True
                    self.save()
                if stage['status'] != 'completed' and attempt.get('log_path') and attempt.get('host_log'):
                    try:
                        (self.output / attempt['host_log']).write_bytes(self.agent.get(attempt['vmid'], attempt['log_path']))
                    except Exception as exc:
                        attempt['log_recovery_error'] = str(exc)
                    self.save()

    def command(self, key, command, retry_steps):
        stage = self.journal['stages'].get(key)
        if stage and stage['status'] == 'completed':
            return (self.output / stage['log']).read_text()
        if stage and not retry_steps:
            raise ValueError(f'{key} has an interrupted/failed attempt. Inspect its log and guest state; '
                             'use --resume --retry-steps only when rerunning that command is appropriate.')
        stage = self.journal['stages'].setdefault(key, {'attempts': []})
        attempt = dict(vmid=command['vmid'], unit='caf-orchestrator-' + uuid.uuid4().hex, stopped=False,
                       argv=command['argv'], started_at=datetime.now(timezone.utc).isoformat())
        stage['attempts'].append(attempt)
        stage['status'] = 'running'
        log = self.output / 'logs' / f'{key}-{len(stage["attempts"]):04d}.log'
        log.parent.mkdir(exist_ok=True)
        stage['log'] = str(log.relative_to(self.output))
        attempt.update(log_path='/tmp/' + attempt['unit'] + '.log', host_log=stage['log'])
        self.save()
        self.notify(f'Running {key} on VM {command["vmid"]}')
        try:
            with ev.lease(f"/var/lock/cyber-agent-flow-eval-vm-{command['vmid']}.lock"):
                try:
                    result = self.agent.call(command['vmid'], 'hook', unit=attempt['unit'], log_path=attempt['log_path'],
                                             seconds=command['timeout_seconds'], timeout=command['timeout_seconds'] + 30,
                                             argv=command['argv'], **{k: command[k] for k in ('user', 'cwd', 'environment_file') if k in command})
                    attempt.update(result)
                    self.save()
                    log.write_bytes(self.agent.get(command['vmid'], result['log_path']))
                    if result['exitcode']:
                        raise ValueError(f'{key} exited {result["exitcode"]}; see {log}')
                finally:
                    self.agent.call(command['vmid'], 'stop', unit=attempt['unit'])
                    attempt['stopped'] = True
                    self.save()
            stage.update(status='completed', files=self.inventory([log]))
            self.save()
            return log.read_text()
        except BaseException as exc:
            stage.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            self.save()
            raise

    def freeze_runtime(self, runtime, files, cfg, identity):
        runtime = deepcopy(runtime)
        folder = self.output / 'inputs'
        folder.mkdir(exist_ok=True)
        captured = []
        for condition in runtime['conditions']:
            name = condition['id']
            remote = cfg['collect'].get(name, {})
            dest = folder / (name + '-catalog.json')
            dest.write_bytes(self.agent.get(runtime['backend']['participant_vmid'], remote['catalog'])
                             if 'catalog' in remote else files[condition['catalog']].encode())
            condition['catalog'] = str(dest)
            captured.append(dest)
            guidance = remote.get('guidance_files', condition['guidance_files'])
            condition['guidance_files'] = []
            for index, source in enumerate(guidance):
                dest = folder / f'{name}-guidance-{index}.md'
                dest.write_bytes(self.agent.get(runtime['backend']['participant_vmid'], source)
                                 if 'guidance_files' in remote else files[source].encode())
                condition['guidance_files'].append(str(dest))
                captured.append(dest)
        runtime['orchestration'] = {'workflow_id': cfg['id'], 'workflow_hash': identity}
        dest = self.output / 'runtime.yaml'
        dest.write_text(yaml.safe_dump(runtime, sort_keys=False))
        captured.append(dest)
        self.journal['stages']['freeze'] = {'status': 'completed', 'files': self.inventory(captured)}
        self.save()


def run(config, output, *, resume=False, retry_steps=False, retry_failed=False, agent=None, evaluator=ev.run, progress=print):
    if (retry_steps or retry_failed) and not resume:
        raise ValueError('Retry options require --resume')
    cfg, runtime, files, identity = load(config)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with ev.lease(output / '.workflow.lock'), ev.TargetReservation(runtime['execution']['target_lock']) as reservation:
        path = output / 'workflow.json'
        if path.exists():
            if not resume:
                raise ValueError('Workflow exists; use --resume or a new output directory')
            journal = ev.read_json(path)
            if journal['workflow_hash'] != identity:
                raise ValueError('Workflow, host artifacts, or source changed; use a new output directory')
        else:
            if resume or any(p.name != '.workflow.lock' for p in output.iterdir()):
                raise ValueError('New workflow needs an empty output; resume needs workflow.json')
            journal = dict(version=1, workflow_hash=identity, workflow=cfg, runtime=runtime,
                           token=cfg['id'] + '-' + uuid.uuid4().hex[:12], stages={}, status='running')
        wf = Workflow(output, journal, agent, progress=progress)
        wf.save()
        wf.check_files()
        wf.recover_jobs()
        try:
            if journal.get('scenario_experiment'):
                journal.update(status='preparing', phase='preparing')
                wf.save()
            for command in cfg['prepare']:
                wf.command('prepare-' + command['id'], command, retry_steps)
            sf = cfg['scenarioforge']
            if sf['mode'] == 'execute':
                marker = export_marker(wf.command('deploy', execute_command(sf, runtime['backend'], journal['token']), retry_steps))
                if marker.get('suite_id') != sf['suite_id']:
                    raise ValueError('ScenarioForge returned a different suite ID')
                archive = marker['archive']
            else:
                marker = None
                archive = sf['archive']
            wf.notify('Collecting the ScenarioForge evaluation package')
            package = output / 'suite'
            if 'fetch' not in journal['stages']:
                if not package.exists():
                    ev.ProxmoxBackend(runtime['backend'], runtime['engine'], agent=wf.agent).fetch_suite(archive, package)
                _, snapshot = ev.load_suite(package)
                if marker and snapshot['package_hash'] != marker['package_hash']:
                    raise ValueError('Downloaded suite differs from deployment export')
                journal['stages']['fetch'] = {'status': 'completed', 'files': wf.inventory(p for p in package.rglob('*') if p.is_file())}
                wf.save()
            _, snapshot = ev.load_suite(package)
            ev.require_ready(snapshot, cfg['max_readiness_age_seconds'])
            if journal.get('sample_id') and journal.get('scenario_experiment'):
                import ipaddress
                import xml.etree.ElementTree as ET
                tree = ET.parse(package / 'evaluator/scenario.xml')
                state = json.loads(tree.find('.//FlowState').text)
                addresses = [ipaddress.ip_interface(node['ipv4']).ip for node in state['chain']]
                if not addresses or any(not ip.is_private or ip.is_loopback for ip in addresses):
                    raise ValueError('Demo export must identify private deployed lab hosts')
                runtime['execution']['network_policy']['allow'] = sorted({str(ip) + '/32' for ip in addresses})
                journal['runtime'] = deepcopy(runtime)
                wf.save()
            wf.notify('Capturing scenario reproduction files')
            if 'reproduction' not in journal['stages']:
                from .run_artifacts import validate_reproduction
                from .auth import private_file
                if sf.get('reproduction_archive'):
                    content = wf.agent.get(runtime['backend']['app_vmid'], sf['reproduction_archive'])
                else:
                    from .reproduction_capture import capture
                    content = capture((package / 'evaluator/scenario.xml').read_bytes(), cfg, runtime, wf.agent)
                validate_reproduction(content, (package / 'evaluator/scenario.xml').read_bytes(),
                                      runtime['backend']['max_transfer_bytes'])
                destination = output / 'reproduction/scenarioforge-reproduction.zip'
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                private_file(destination, content, replace=destination.exists())
                journal['stages']['reproduction'] = {'status': 'completed', 'files': wf.inventory([destination])}
                wf.save()
            for command in cfg['artifacts']:
                wf.command('artifact-' + command['id'], command, retry_steps)
            if 'freeze' not in journal['stages']:
                with ev.lease(f"/var/lock/cyber-agent-flow-eval-vm-{runtime['backend']['participant_vmid']}.lock"):
                    wf.freeze_runtime(runtime, files, cfg, identity)
            wf.notify('Preparing the agent evaluation settings')
            study = output / 'study.yaml'
            if 'import' not in journal['stages']:
                # Recreate only a derived config if interrupted before journaling.
                study.unlink(missing_ok=True)
                ev.import_config(package, output / 'runtime.yaml', study, cfg['max_readiness_age_seconds'])
                journal['stages']['import'] = {'status': 'completed', 'files': wf.inventory([study])}
                wf.save()
            dataset = output / 'evaluation'
            journal['status'] = 'evaluating'
            if journal.get('scenario_experiment'):
                journal['phase'] = 'evaluating'
            wf.save()
            wf.notify('Running agent evaluation')
            rows = evaluator(study, dataset, resume=(dataset / 'manifest.json').exists(),
                             retry_failed=retry_failed, reservation=reservation, progress=wf.notify)
            latest = {}
            for row in rows:
                latest[row['trial_id']] = row
            ok = all(row['status'] == 'completed' for row in latest.values())
            journal['status'] = 'completed' if ok else 'completed_with_errors'
            journal['trial_count'] = len(latest)
            wf.save()
            return 0 if ok else 1
        except BaseException as exc:
            journal.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            wf.save()
            raise


def recover(output):
    """Use recorded configuration even when source inputs are no longer present."""
    output = Path(output).resolve()
    with ev.lease(output / '.workflow.lock'):
        journal = ev.read_json(output / 'workflow.json')
        with ev.TargetReservation(journal['runtime']['execution']['target_lock']):
            Workflow(output, journal).recover_jobs()
            ev.recover(journal['runtime'], output / 'evaluation')
            if journal.get('sample_fixture'):
                from .samples import stop_fixture
                stop_fixture(output, journal)
