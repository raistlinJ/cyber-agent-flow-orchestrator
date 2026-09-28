from pathlib import Path

import pytest
import yaml

from cyber_agent_flow_orchestrator.bootstrap import prepare
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.provision import import_profile, read_provision
from cyber_agent_flow_orchestrator.__main__ import main
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve
from test_user_access import access, vm, Probe, observed


def source(tmp_path, extra=''):
    path = tmp_path / 'lab.conf'
    path.write_text('core_vmid=8101\napp_vmid=8102\nparticipant_vmid=8103\n' + extra)
    return path


def test_literal_parser_matches_provisioner_format_and_drops_secrets(tmp_path):
    path = source(tmp_path, '# Comment\nllm_model="lab-model"\nllm_provider_type=openai\n'
                  "llm_provider_url='https://model.lab/v1'\napp_password=$(touch secret)\n"
                  'web_admin_password=do-not-copy\nmanagement_bridge=sfmgmt0\n')
    assert read_provision(path) == dict(core_vmid=8101, app_vmid=8102, participant_vmid=8103,
        llm_model='lab-model', llm_provider_type='openai', llm_provider_url='https://model.lab/v1')
    assert not (tmp_path / 'secret').exists()
    path.write_text('# Defaults, matching installer\n')
    assert read_provision(path) == dict(core_vmid=9401, app_vmid=9402, participant_vmid=9403)


@pytest.mark.parametrize('extra', ['app_vmid=abc', 'core_vmid=99', 'app_vmid=8103',
    'llm_provider_type=unknown', 'llm_provider_url=https://user:SECRET@model.lab',
    'llm_provider_url=https://model.lab?token=SECRET', 'llm_provider_url=$(touch SECRET)',
    'app_password="SECRET', 'export app_password=SECRET'])
def test_invalid_input_rejected_without_exposing_values(tmp_path, extra):
    with pytest.raises(ValueError) as exc:
        read_provision(source(tmp_path, extra))
    assert 'SECRET' not in str(exc.value)


def test_profile_maps_vmids_hooks_model_lock_and_preserves_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workflow, web = prepare()
    cfg = yaml.safe_load(Path(workflow).read_text())
    cfg['prepare'] = [dict(id='reset', vmid=9401, argv=['/opt/reset'], timeout_seconds=30)]
    Path(workflow).write_text(yaml.safe_dump(cfg))
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    path = source(tmp_path, 'llm_provider_url=http://model.lab:11434\nllm_model=lab-model\n'
                  'app_password=PRIVATE_PASSWORD\nparticipant_cidr=10.99.1.10/24\n')
    profile = import_profile(path, workflow)
    cfg, runtime, _, _ = load(profile)
    assert runtime['backend']['app_vmid'] == 8102 and runtime['backend']['participant_vmid'] == 8103
    assert cfg['monitoring']['core_vmid'] == 8101
    assert cfg['prepare'][0]['vmid'] == 8101
    assert runtime['model']['url'] == 'http://model.lab:11434'
    assert runtime['model']['name'] == 'lab-model'
    assert runtime['execution']['target_lock'] == str(Path('/var/lock/caf-lab-8101.lock').resolve())
    assert runtime['execution']['network_policy'] == load(workflow)[1]['execution']['network_policy']
    assert all(p.read_bytes() == content for p, content in before.items())
    assert all('PRIVATE_PASSWORD' not in p.read_text() for p in Path(profile).parent.glob('*.yaml'))
    # An identical import preserves manual profile edits; a changed import gets a new profile.
    runtime_path = Path(profile).with_name('runtime.yaml')
    edited = yaml.safe_load(runtime_path.read_text())
    edited['execution']['max_turns'] = 7
    runtime_path.write_text(yaml.safe_dump(edited))
    assert import_profile(path, workflow) == profile
    assert load(profile)[1]['execution']['max_turns'] == 7
    path.write_text(path.read_text().replace('lab-model', 'another-model'))
    assert import_profile(path, workflow) != profile


def test_cli_import_and_implicit_serve_keep_web_settings_and_existing_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provision = source(tmp_path)
    from cyber_agent_flow_orchestrator import web
    calls = []
    monkeypatch.setattr(web, 'serve', lambda *a, **kw: calls.append((a, kw)) or 0)
    assert main(['--provision-config', str(provision)]) == 0
    cfg, runtime, _, _ = load(calls[-1][0][0])
    assert runtime['backend']['app_vmid'] == 8102
    web_path = Path(calls[-1][1]['web_config'])
    before = web_path.read_bytes()
    assert main(['import-provision', str(provision), '--output', 'lab-profile']) == 0
    assert load('lab-profile/workflow.yaml')[1]['backend']['participant_vmid'] == 8103
    assert main(['import-provision', str(provision), '--output', 'lab-profile']) == 2
    assert web_path.read_bytes() == before


def test_imported_roles_are_scoped_and_never_replace_saved_choices(pve, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workflow, _ = prepare()
    profile = import_profile(source(tmp_path), workflow)
    pve[0]['resources']['operator@pve'] = [vm(8102), vm(8103)]
    pve[0]['resources']['bob@pve'] = [vm(9102)]
    alice, bob = access(pve), access(pve, 'bob@pve')
    calls = []
    dash = UserDashboard(profile, tmp_path / 'runs', 2, lambda b, a: Probe(b, a, calls))
    try:
        assert dash.read(alice)['roles'] == dict(scenarioforge=8102, participant=8103, core=None)
        assert dash.read(bob)['roles'] == dict(scenarioforge=None, participant=None, core=None)
        assert {row['vmid'] for row in observed(dash, alice)['vms']} == {8102, 8103, None}
        dash.select(alice, dict(scenarioforge=None, participant=None, core=None))
        assert all(v is None for v in dash.read(alice)['roles'].values())
        assert Workspace(dash.root, alice.username).roles() == dict.fromkeys(('scenarioforge', 'participant', 'core'))
        pve[0]['resources']['bob@pve'] = [vm(8101)]
        assert dash.read(bob)['roles']['core'] is None  # Saved choices remain authoritative.
        assert not any(vmid == 8101 for _, _, vmid in calls)
    finally:
        dash.close()
