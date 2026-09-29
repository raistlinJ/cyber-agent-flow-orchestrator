import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import service, workflow, run_artifacts
from cyber_agent_flow_orchestrator.reproduction_capture import GUEST, capture
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_workflow import lab
from test_pve_auth import pve, make_auth
from test_user_access import Probe, vm
from test_web import request
from https_fixture import secure_server, PASSWORD


def test_results_expose_frozen_prompts_tools_settings_and_checkpoints(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    manifest = ev.read_json(output / 'evaluation/manifest.json')
    attempt = next((output / 'evaluation/trials').glob('*/attempt-*'))
    ev.write_json(attempt / 'guest-output/checkpoint.json', [
        {'role': 'system', 'content': 'Exact recorded system prompt'}])
    ev.write_json(attempt / 'progress.json', {'checkpoints': [{'seconds': 15, 'flags_observed': 1}]})
    calls = len(agent.calls)
    result = service.results(output)
    inputs = result['run_configuration']
    assert inputs['tasks'][0]['prompt'] == manifest['spec']['tasks'][0]['prompt']
    assert 'verifier' not in inputs['tasks'][0]
    assert inputs['conditions'] == manifest['spec']['conditions']
    assert inputs['model'] == manifest['spec']['model']
    assert inputs['execution'] == manifest['spec']['execution']
    assert inputs['system_prompts'][0]['text'] == 'Exact recorded system prompt'
    assert any(row.get('flag_progress', {}).get('checkpoints') for row in result['evaluation']['attempts'])
    assert inputs['scenarioforge']['xml_available']
    assert len(agent.calls) == calls
    assert inputs['provenance']['spec_hash'] == manifest['spec_hash']


def test_download_bundle_includes_exact_inputs_evidence_and_reimport(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    for artifact in ['scenario-reproduction', 'scenario-evaluation', 'run-bundle']:
        with run_artifacts.download(output, artifact) as (stream, mime, name):
            assert mime == 'application/zip'
            content = stream.read()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if artifact == 'scenario-reproduction':
                xml = archive.read('scenario.xml')
                assert xml == (output / 'suite/evaluator/scenario.xml').read_bytes()
                manifest = json.loads(archive.read(run_artifacts.REPRODUCTION))
                assert manifest['scenario']['sha256'] == hashlib.sha256(xml).hexdigest()
            elif artifact == 'scenario-evaluation':
                assert archive.read('manifest.json') == (output / 'suite/manifest.json').read_bytes()
            else:
                assert archive.read('study.yaml') == (output / 'study.yaml').read_bytes()
                assert 'evaluation/manifest.json' in archive.namelist()
                assert 'scenarioforge-reproduction.zip' in archive.namelist()
                assert not any(name.endswith('.lock') for name in archive.namelist())


def test_legacy_reimport_uses_saved_xml_and_reports_missing_sources(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    (output / 'reproduction/scenarioforge-reproduction.zip').unlink()
    xml = b'<Scenarios><Scenario name="Saved"><FlowState>{"seed":42,"artifacts_dir":"/tmp/vulns/flag_generators_runs/missing"}</FlowState></Scenario></Scenarios>'
    (output / 'suite/evaluator/scenario.xml').write_bytes(xml)
    with run_artifacts.download(output, 'scenario-reproduction') as (stream, _, _):
        with zipfile.ZipFile(stream) as archive:
            manifest = json.loads(archive.read(run_artifacts.REPRODUCTION))
            assert manifest['seed'] == 42
            assert manifest['artifact_sources'][0]['bundled'] is False
            assert archive.read('scenario.xml') == xml


def test_reproduction_archive_rejects_wrong_scenario(lab):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    content = (output / 'reproduction/scenarioforge-reproduction.zip').read_bytes()
    with pytest.raises(ValueError, match='does not match'):
        run_artifacts.validate_reproduction(content, b'<Scenarios/>', 10**7)


def test_download_rejects_links_traversal_and_active_bundle(lab, tmp_path):
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    private = tmp_path / 'private'
    private.write_text('DO NOT RETURN')
    (output / 'inputs/linked.json').symlink_to(private)
    (output / 'evaluation/linked').symlink_to(tmp_path, target_is_directory=True)
    inventory = run_artifacts.files(output)
    assert not any('linked' in item['path'] for item in inventory)
    with pytest.raises(ValueError):
        with run_artifacts.download(output, '../../private'):
            pass
    with pytest.raises(OSError):
        with run_artifacts.open_saved(output, 'inputs/linked.json'):
            pass
    with ev.lease(output / '.workflow.lock'):
        with pytest.raises(ValueError, match='finish'):
            with run_artifacts.download(output, 'run-bundle'):
                pass


def test_captured_bundle_imports_with_scenarioforge(lab, tmp_path):
    importer = Path(__file__).resolve().parents[2] / 'scenarioforge/webapp/reproduction_bundle.py'
    if not importer.is_file():
        pytest.skip('ScenarioForge checkout unavailable')
    module_spec = importlib.util.spec_from_file_location('caf_test_sf_importer', importer)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    config, output, agent, _ = lab
    workflow.run(config, output, agent=agent)
    destination = tmp_path / 'imported'
    destination.mkdir()
    result = module.import_scenario_file(str(output / 'reproduction/scenarioforge-reproduction.zip'), str(destination))
    assert result.kind == 'reproduction-bundle'
    assert Path(result.xml_path).exists()


def test_guest_capture_includes_referenced_files_and_hashes(tmp_path):
    repo = tmp_path / 'scenarioforge'
    artifacts = repo / 'outputs/generated'
    artifacts.mkdir(parents=True)
    (artifacts / 'setup.sh').write_text('echo demo')
    (artifacts / 'escape').symlink_to('/etc/passwd')
    result = subprocess.run([sys.executable, '-c', GUEST,
        json.dumps(dict(source=str(artifacts), repo=str(repo), limit=1024*1024))],
        capture_output=True, text=True, check=True)
    record = json.loads(result.stdout)
    package = Path(record['path'])
    try:
        with zipfile.ZipFile(package) as archive:
            records = json.loads(archive.read('files.json'))
            assert [entry['path'] for entry in records] == ['setup.sh']
            assert archive.read('files/setup.sh') == b'echo demo'
    finally:
        package.unlink()


def test_https_downloads_are_owner_scoped_and_streamed(pve, lab, tmp_path):
    pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403)]
    pve[0]['resources']['bob@pve'] = [vm(201)]
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        output = Workspace(dash.root, 'operator@pve').run_path('private-run')
        workflow.run(lab[0], output, agent=lab[2])
        with secure_server(dash, tmp_path / 'web', auth=make_auth(pve)) as server:
            def login(name):
                code, headers, _ = request(server, '/api/login', {'username': name, 'password': PASSWORD})
                assert code == 200
                return headers['Set-Cookie'].split(';', 1)[0]
            alice, bob = login('operator@pve'), login('bob@pve')
            endpoint = '/api/runs/private-run/artifact?id=scenario-reproduction'
            code, headers, body = request(server, endpoint, cookie=alice)
            assert code == 200 and headers['Content-Type'] == 'application/zip'
            assert zipfile.is_zipfile(io.BytesIO(body))
            assert request(server, endpoint, cookie=bob)[0] == 404
            assert request(server, endpoint)[0] == 401
            assert request(server, '/api/runs/private-run/artifact?id=../../etc/passwd', cookie=alice)[0] == 404
    finally:
        dash.close()


def test_automatic_capture_roundtrip_preserves_generated_artifacts(tmp_path):
    repo = tmp_path / 'sf'
    directory = repo / 'outputs/flag_generators_runs/demo'
    directory.mkdir(parents=True)
    (directory / 'flag.txt').write_text('FLAG{saved}')
    xml = ('<Scenarios><Scenario name="Demo"><FlowState>' + json.dumps({
        'seed': 17, 'artifacts_dir': str(directory), 'chain': [{'id': 'demo'}]}) +
        '</FlowState></Scenario></Scenarios>').encode()
    class LocalAgent:
        script = ''
        def call(self, vmid, op, **data):
            if op == 'unlink':
                Path(data['path']).unlink(missing_ok=True)
                return {}
            result = subprocess.run([sys.executable, '-c', self.script, json.dumps(data)],
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        def get(self, vmid, path):
            return Path(path).read_bytes()
    content = capture(xml, {'scenarioforge': {'repo': str(repo)}},
                      {'backend': {'app_vmid': 101, 'max_transfer_bytes': 1024*1024}}, LocalAgent())
    manifest = run_artifacts.validate_reproduction(content, xml, 1024*1024)
    assert manifest['fidelity'] == 'portable-artifacts'
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.read('artifacts/001/flag.txt') == b'FLAG{saved}'
    importer = Path(__file__).resolve().parents[2] / 'scenarioforge/webapp/reproduction_bundle.py'
    if importer.is_file():
        module_spec = importlib.util.spec_from_file_location('caf_test_sf_roundtrip', importer)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        module_spec.loader.exec_module(module)
        archive_path = tmp_path / 'reproduction.zip'
        archive_path.write_bytes(content)
        destination = tmp_path / 'import'
        destination.mkdir()
        imported = module.import_scenario_file(str(archive_path), str(destination))
        assert imported.bundled_artifact_sources == 1
        assert next(destination.rglob('flag.txt')).read_text() == 'FLAG{saved}'
        import xml.etree.ElementTree as ET
        restored_flow = json.loads(ET.parse(imported.xml_path).find('.//FlowState').text)
        assert restored_flow['artifacts_dir'] == '/tmp/vulns/flag_generators_runs/demo'
        assert restored_flow['reproduction_artifact_sources'][0]['source_path'] == str(directory)


def test_explicit_reproduction_archive_is_frozen_and_hash_checked(lab):
    config, output, agent, _ = lab
    package_xml = (config.parent / 'package/evaluator/scenario.xml').read_bytes()
    content = capture(package_xml, {'scenarioforge': {}},
                      {'backend': {'app_vmid': 9402, 'max_transfer_bytes': 1024*1024}}, agent)
    agent.files['/exports/reproduction.zip'] = content
    cfg = yaml.safe_load(config.read_text())
    cfg['scenarioforge']['reproduction_archive'] = '/exports/reproduction.zip'
    config.write_text(yaml.safe_dump(cfg))
    workflow.run(config, output, agent=agent)
    assert (output / 'reproduction/scenarioforge-reproduction.zip').read_bytes() == content
    (output / 'reproduction/scenarioforge-reproduction.zip').write_bytes(b'changed')
    with pytest.raises(ValueError, match='Frozen workflow file changed'):
        workflow.run(config, output, agent=agent, resume=True)


def test_capture_reports_missing_sources_and_propagates_revocation():
    from cyber_agent_flow_orchestrator.access import AccessDenied
    xml = b'<Scenarios><FlowState>{"artifacts_dir":"/tmp/vulns/flag_generators_runs/missing"}</FlowState></Scenarios>'
    runtime = {'backend': {'app_vmid': 101, 'max_transfer_bytes': 1024*1024}}
    class MissingAgent:
        def call(self, *args, **kwargs):
            return {'available': False}
    content = capture(xml, {'scenarioforge': {}}, runtime, MissingAgent())
    manifest = run_artifacts.validate_reproduction(content, xml, 1024*1024)
    assert manifest['artifact_sources'][0]['bundled'] is False
    assert manifest['fidelity'] == 'xml-replay'
    class RevokedAgent:
        def call(self, *args, **kwargs):
            raise AccessDenied('revoked')
    with pytest.raises(AccessDenied):
        capture(xml, {'scenarioforge': {}}, runtime, RevokedAgent())
