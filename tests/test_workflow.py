from contextlib import nullcontext
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import zipfile

import pytest
import yaml

from cyber_agent_flow_eval import integration as ev, backends
from cyber_agent_flow_eval.scenarioforge import encoded, sha256
from cyber_agent_flow_orchestrator import workflow
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.__main__ import main


class Agent:
    def __init__(self):
        self.calls = []
        self.files = {}
        self.fail = False
        self.marker = None
        self.stop_fail = False

    def call(self, vmid, op, **data):
        self.calls.append((vmid, op, data))
        if op == 'preflight':
            return {'ready':True,'stopped':[]}
        if op == 'stop':
            if self.stop_fail:
                raise ValueError('guest unavailable')
            return {}
        assert op == 'hook'
        self.files[data['log_path']] = (('EVALUATION_PACKAGE_JSON: ' + json.dumps(self.marker))
                                       if self.marker and 'scenarioforge.cli' in data['argv'] else 'prepared').encode()
        return {'exitcode': int(self.fail), 'log_path': data['log_path']}

    def get(self, vmid, path):
        return self.files[path]


class TrialBackend:
    def lock(self): return nullcontext()
    def identities(self): return {'engine': 'fixture', 'runtime': {}}
    def recover(self, output): pass
    def before_trial(self, directory): pass
    def launch(self, directory, seconds):
        data = ev.read_json(directory / 'input.json')
        for hidden in ('FLAG{fixture', 'verifier', 'orchestration', 'attack-graph'):
            assert hidden not in json.dumps(data)
        return {'status': 'completed', 'final_answer': '{"flags":{"entry":"FLAG{fixture-entry}"}}'}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    # Real package validation/import/scoring; fake only guest I/O and agent run.
    package = tmp_path / 'package'
    shutil.copytree(Path(__file__).parent / 'fixtures/scenarioforge_suite', package)
    manifest = ev.read_json(package / 'manifest.json')
    manifest['scenario'].update(core_session_id=9, core_host='fixture-only.invalid')
    report = dict(status='complete', ok=True, overall='pass', scenario='Evaluation fixture',
                  session_id=9, core_host='fixture-only.invalid', session_confirmed=True,
                  xml_sha256=manifest['scenario']['xml_sha256'], checked_at=datetime.now(timezone.utc).isoformat(),
                  checks=[{'key': k, 'status': 'pass', 'items': []} for k in ['containers', 'services', 'ports', 'injects']])
    (package / 'evaluator/readiness.json').write_bytes(encoded(report))
    for name in manifest['files']:
        manifest['files'][name] = sha256((package / name).read_bytes())
    manifest.pop('package_hash')
    manifest['package_hash'] = sha256(encoded(manifest))
    (package / 'manifest.json').write_bytes(encoded(manifest))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        for p in package.rglob('*'):
            if p.is_file(): z.write(p, str(p.relative_to(package)))
    agent = Agent()
    agent.files['/exports/suite.zip'] = archive.getvalue()
    agent.files['/artifacts/added.json'] = b'{"tools":[{"name":"helper","command":"true","allow_args":false}]}'
    (tmp_path / 'baseline.json').write_text('{"tools":[]}')
    runtime = dict(version=1, id='template', backend=dict(type='proxmox', participant_vmid=9403, app_vmid=9402, user='participant'),
                   engine={'path': '/opt/cyber-agent-flow', 'python': '/opt/cyber-agent-flow/venv/bin/python'},
                   model={'provider': 'ollama_direct', 'name': 'fixture', 'url': 'http://localhost:11434'},
                   execution=dict(wall_seconds=10, max_turns=2, target_lock='target.lock',
                                  network_policy={'allow': ['10.77.0.0/24'], 'disallow': []}), repetitions=1,
                   conditions=[{'id': 'baseline', 'catalog': 'baseline.json', 'tools': []},
                               {'id': 'added', 'catalog': 'collected.json', 'tools': ['helper']}])
    (tmp_path / 'runtime.yaml').write_text(yaml.safe_dump(runtime))
    cfg = dict(version=1, id='fixture', runtime='runtime.yaml', scenarioforge={'mode': 'reuse_export', 'archive': '/exports/suite.zip'},
               artifacts=[dict(id='generate', vmid=9403, argv=['/opt/generate'], timeout_seconds=30, user='participant')],
               collect={'added': {'catalog': '/artifacts/added.json'}})
    config = tmp_path / 'workflow.yaml'
    config.write_text(yaml.safe_dump(cfg))
    real_lease = ev.lease
    monkeypatch.setattr(ev, 'lease', lambda p: real_lease(tmp_path / Path(p).name) if str(p).startswith('/var/lock/') else real_lease(p))
    monkeypatch.setattr(backends, 'create_backend', lambda spec: TrialBackend())
    return config, tmp_path / 'run', agent, manifest


def test_full_workflow_preserves_baseline_provenance_and_resume(lab):
    config, output, agent, _ = lab
    assert workflow.run(config, output, agent=agent) == 0
    spec = ev.resolve(output / 'study.yaml')
    assert spec['conditions'][0]['catalog_snapshot'] == {'tools': []}
    assert spec['conditions'][1]['tools'] == ['helper']
    rows = ev.export(output / 'evaluation')
    assert len(rows) == 2 and all(r['score'] == .5 for r in rows)
    assert rows[0]['orchestration']['workflow_id'] == 'fixture'
    assert workflow.run(config, output, agent=agent, resume=True) == 0
    assert len([c for c in agent.calls if c[1] == 'hook']) == 1
    assert len(ev.export(output / 'evaluation')) == 2
    (output / 'inputs/added-catalog.json').write_text('{"tools":[]}')
    with pytest.raises(ValueError, match='Frozen workflow file changed'):
        workflow.run(config, output, agent=agent, resume=True)


def test_deploy_uses_real_sf_flags_and_reuses_export(lab):
    config, output, agent, manifest = lab
    cfg = yaml.safe_load(config.read_text())
    cfg['scenarioforge'] = dict(mode='execute', xml='/scenarios/lab.xml', scenario='Evaluation fixture',
                               suite_id=manifest['id'], output_root='/exports')
    config.write_text(yaml.safe_dump(cfg))
    agent.marker = dict(state='complete', readiness_passed=True, archive='/exports/suite.zip',
                        package_hash=manifest['package_hash'], suite_id=manifest['id'])
    workflow.run(config, output, agent=agent)
    deploy = next(c[2] for c in agent.calls if c[1] == 'hook' and 'scenarioforge.cli' in c[2]['argv'])
    assert '--evaluation-export' in deploy['argv'] and '--xml' in deploy['argv']
    assert deploy['user'] == 'scenarioforge' and deploy['cwd'] == '/opt/scenarioforge'
    workflow.run(config, output, agent=agent, resume=True)
    assert len([c for c in agent.calls if c[1] == 'hook']) == 2


def test_failed_mutating_step_needs_explicit_retry(lab):
    config, output, agent, _ = lab
    agent.fail = True
    with pytest.raises(ValueError, match='exited 1'):
        workflow.run(config, output, agent=agent)
    agent.fail = False
    with pytest.raises(ValueError, match='retry-steps'):
        workflow.run(config, output, agent=agent, resume=True)
    assert not (output / 'evaluation').exists()
    workflow.run(config, output, agent=agent, resume=True, retry_steps=True)
    journal = ev.read_json(output / 'workflow.json')
    assert len(journal['stages']['artifact-generate']['attempts']) == 2
    assert len(list((output / 'logs').glob('*.log'))) == 2


def test_target_lock_blocks_all_guest_mutation(lab):
    config, output, agent, _ = lab
    _, runtime, _, _ = load(config)
    with ev.TargetReservation(runtime['execution']['target_lock']):
        with pytest.raises(ValueError, match='Already locked'):
            workflow.run(config, output, agent=agent)
    assert not [call for call in agent.calls if call[1]!='preflight']


def test_cleanup_failure_blocks_eval_and_is_recoverable(lab):
    config, output, agent, _ = lab
    agent.stop_fail = True
    with pytest.raises(ValueError, match='guest unavailable'):
        workflow.run(config, output, agent=agent)
    assert not (output / 'evaluation').exists()
    journal = ev.read_json(output / 'workflow.json')
    assert not journal['stages']['artifact-generate']['attempts'][0]['stopped']
    agent.stop_fail = False
    workflow.Workflow(output, journal, agent).recover_jobs()
    assert journal['stages']['artifact-generate']['attempts'][0]['stopped']


def test_config_change_refuses_resume_and_plan_has_no_guest_actions(lab, capsys):
    config, output, agent, _ = lab
    assert main(['plan', str(config)]) == 0
    assert 'exported tasks' in capsys.readouterr().out
    assert not [call for call in agent.calls if call[1]!='preflight']
    workflow.run(config, output, agent=agent)
    cfg = yaml.safe_load(config.read_text())
    cfg['id'] = 'changed'
    config.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match='changed'):
        workflow.run(config, output, agent=agent, resume=True)


@pytest.mark.parametrize('log', ['', 'EVALUATION_PACKAGE_JSON: {}', 'EVALUATION_PACKAGE_JSON: {}\nEVALUATION_PACKAGE_JSON: {}'])
def test_missing_ambiguous_or_unready_export_rejected(log):
    with pytest.raises(ValueError):
        workflow.export_marker(log)


def test_frozen_artifact_change_blocks_resume(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    (config.parent / 'baseline.json').write_text('{"tools": [], "changed": true}')
    with pytest.raises(ValueError, match='changed'):
        workflow.run(config, output, agent=agent, resume=True)


def test_export_hash_mismatch_prevents_artifact_generation(lab):
    config, output, agent, manifest = lab
    cfg = yaml.safe_load(config.read_text())
    cfg['scenarioforge'] = dict(mode='execute', xml='/scenarios/lab.xml', scenario='Evaluation fixture',
                               suite_id=manifest['id'], output_root='/exports')
    config.write_text(yaml.safe_dump(cfg))
    agent.marker = dict(state='complete', readiness_passed=True, archive='/exports/suite.zip',
                        package_hash='0' * 64, suite_id=manifest['id'])
    with pytest.raises(ValueError, match='differs from deployment'):
        workflow.run(config, output, agent=agent)
    assert len([c for c in agent.calls if c[1] == 'hook']) == 1
    assert not (output / 'evaluation').exists()


def test_readiness_gate_precedes_generation(lab, monkeypatch):
    config, output, agent, _ = lab
    def expired(*args):
        raise ValueError('readiness expired')
    monkeypatch.setattr(ev, 'require_ready', expired)
    with pytest.raises(ValueError, match='readiness expired'):
        workflow.run(config, output, agent=agent)
    assert not [call for call in agent.calls if call[1]!='preflight']
    assert not (output / 'evaluation').exists()


def test_generation_test_failure_never_starts_evaluation(lab):
    config, output, agent, _ = lab
    cfg = yaml.safe_load(config.read_text())
    cfg['artifacts'].append(dict(id='test', vmid=9403, argv=['/opt/test-helper'], timeout_seconds=30))
    config.write_text(yaml.safe_dump(cfg))
    original_call = agent.call
    def call(vmid, op, **data):
        if op == 'hook' and data['argv'][0] == '/opt/test-helper':
            agent.fail = True
        return original_call(vmid, op, **data)
    agent.call = call
    with pytest.raises(ValueError, match='artifact-test exited 1'):
        workflow.run(config, output, agent=agent)
    assert not (output / 'evaluation').exists()
    journal = ev.read_json(output / 'workflow.json')
    assert journal['stages']['artifact-generate']['status'] == 'completed'


def test_recover_uses_journal_without_original_config(lab, monkeypatch):
    config, output, agent, _ = lab
    agent.stop_fail = True
    with pytest.raises(ValueError):
        workflow.run(config, output, agent=agent)
    config.unlink()
    agent.stop_fail = False
    monkeypatch.setattr(ev, 'GuestAgent', lambda runtime: agent)
    workflow.recover(output)
    journal = ev.read_json(output / 'workflow.json')
    assert journal['stages']['artifact-generate']['attempts'][0]['stopped']
    assert not (output / 'evaluation').exists()
    assert len([c for c in agent.calls if c[1] == 'hook']) == 1


@pytest.mark.parametrize('name', ['01-reuse-export.yaml', '02-deploy-evaluate.yaml', '03-generate-test-evaluate.yaml'])
def test_documented_examples_validate_without_guest_access(name):
    load(Path(__file__).parents[1] / 'examples' / name)
