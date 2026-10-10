import json
import hashlib
import os
from pathlib import Path
import pwd
import pytest

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import scenario_guest as guest, scenarios, samples, service
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve
from test_user_access import access, vm
from test_workflow import lab

XML = b'<Scenarios><Scenario name="Saved lab"><FlowState>{"chain":[{"id":"entry"}]}</FlowState></Scenario><Scenario name="Unresolved"/></Scenarios>'


def test_listing_snapshot_and_stale_selection(tmp_path):
    source = tmp_path / 'lab.xml'
    source.write_bytes(XML)
    (tmp_path / 'link.xml').symlink_to(source)
    (tmp_path / 'bad.xml').write_text('<broken')
    rows = guest.catalogue(dict(roots=[str(tmp_path)]))['items']
    assert [row['scenario'] for row in rows] == ['Saved lab', 'Unresolved']
    assert rows[0]['resolved_chain'] and not rows[1]['resolved_chain']
    assert len(guest.catalogue(dict(roots=[str(tmp_path)], query='saved'))['items']) == 1
    args = dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path), path=str(source),
                token='a' * 32, selection_id=rows[0]['id'], user=pwd.getpwuid(os.getuid()).pw_name)
    saved = guest.dispatch(args)
    source.write_bytes(XML + b' ')
    assert open(saved['snapshot_path'], 'rb').read() == XML
    assert guest.dispatch(dict(op='check', token='a'*32, repo=str(tmp_path), sha256=rows[0]['sha256']))
    with pytest.raises(ValueError, match='changed'):
        guest.dispatch(dict(args, token='b'*32))
    with pytest.raises(ValueError, match='outside'):
        guest.dispatch(dict(args, roots=[str(tmp_path / 'elsewhere')]))
    with open(saved['snapshot_path'], 'wb') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='changed'):
        guest.dispatch(dict(op='check', token='a'*32, repo=str(tmp_path), sha256=rows[0]['sha256']))


def test_catalogue_orders_xml_by_newest_timestamp(tmp_path):
    old=tmp_path/'old.xml'
    new=tmp_path/'new.xml'
    old.write_bytes(XML.replace(b'Saved lab',b'Older lab'))
    new.write_bytes(XML.replace(b'Saved lab',b'Newest lab'))
    os.utime(old,(1000,1000))
    os.utime(new,(2000,2000))
    rows=guest.catalogue(dict(roots=[str(tmp_path)]))['items']
    assert [Path(row['path']).name for row in rows]==['new.xml','new.xml','old.xml','old.xml']
    assert rows[0]['modified_epoch']==2000
    assert rows[-1]['modified_epoch']==1000
    assert rows[0]['modified_at'].endswith('+00:00')


def test_unresolved_scenario_cannot_snapshot(tmp_path):
    source = tmp_path / 'lab.xml'
    source.write_bytes(XML)
    row = guest.inspect(source)[1]
    with pytest.raises(ValueError, match='Flow chain'):
        guest.dispatch(dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path), path=str(source),
                            token='a'*32, selection_id=row['id']))


@pytest.mark.parametrize("real_workflow", [False, True])
def test_saved_scenario_create_launch_and_changed_snapshot(pve, lab, tmp_path, monkeypatch, real_workflow):
    monkeypatch.setattr(ev, 'GuestAgent', lambda backend: lab[2])
    pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=9402, participant=9403, core=None), user)
    cfg, runtime, _, _ = load(lab[0])
    manager = samples.SampleManager(runtime, tmp_path / 'runs')
    controller = scenarios.ScenarioExperiments(cfg, runtime, tmp_path / 'runs', manager)
    calls = []
    class Guest:
        changed = False
        def call(self, vmid, op, **data):
            calls.append((vmid, op, data))
            item = dict(id='a'*64, scenario='Saved lab', path='/opt/scenarioforge/uploads/lab.xml',
                        sha256=hashlib.sha256(XML).hexdigest(), resolved_chain=True, chain_length=1, bytes=len(XML))
            if op == 'list':
                return dict(items=[item], roots=data['roots'], truncated=False)
            if op == 'tasks':
                text=json.dumps({'tasks':[dict(id='collect-flags', family='flag-collection',
                    flag_nodes=['entry'], required_checks=['containers','services','ports','injects'])],
                    'suggested_tasks':[],'context':{}})
                return {'chunk':text,'total':len(text),'sha256':hashlib.sha256(text.encode()).hexdigest()}
            if op == 'snapshot':
                return dict(item, snapshot_path='/opt/scenarioforge/outputs/caf-orchestrator/'+data['token']+'/scenario.xml')
            if self.changed:
                raise ValueError('Saved scenario XML changed')
            return {}
    remote = Guest()
    monkeypatch.setattr(scenarios, 'guest', lambda backend: remote)
    launched = []
    def run(source, output, **kwargs):
        cfg, runtime, _, _ = load(source)
        launched.append((cfg, runtime))
        if real_workflow:
            from cyber_agent_flow_orchestrator import workflow
            agent, manifest = lab[2], lab[3]
            agent.marker = dict(state='complete', readiness_passed=True, archive='/exports/suite.zip',
                                package_hash=manifest['package_hash'], suite_id=cfg['scenarioforge']['suite_id'])
            return workflow.run(source, output, agent=agent, **kwargs)
        kwargs['progress']('Running agent evaluation')
        record = ev.read_json(output / 'workflow.json')
        record['status'] = 'completed'
        ev.write_json(output / 'workflow.json', record)
    monkeypatch.setattr(service, 'run', run)
    try:
        controller.catalogue(user)
        settings = dict(repetitions=2, max_turns=7, wall_seconds=45, tool_timeout=15, context_window=4096,max_tries_before_solution=4)
        reply = controller.create(user, 'a'*64, 'b'*32, '10.77.0.0/24', '', evaluation=settings)
        output = workspace.run_path(reply['run_id'])
        assert reply['status'] == 'ready' and not launched
        record = ev.read_json(output / 'workflow.json')
        assert record['runtime']['conditions'][0]['tools'] == ['nmap', 'curl', 'python3']
        assert record['runtime']['backend']['before_trial'] == []
        assert record['runtime']['backend']['route_allowed_targets'] is True
        assert record['runtime']['repetitions'] == 2
        assert all(record['runtime']['execution'][key] == value for key, value in settings.items() if key != 'repetitions')
        assert controller.create(user, 'a'*64, 'b'*32, '10.77.0.0/24', '') == reply
        controller.run_saved(user, reply['run_id'], 'c'*32)
        manager.jobs[user.username].result(timeout=10)
        assert ev.read_json(output / 'workflow.json')['status'] == 'completed'
        assert launched[0][0]['scenarioforge']['mode'] == 'execute'
        assert launched[0][0]['scenarioforge']['xml'] == record['scenario_experiment']['snapshot_path']
        assert launched[0][1]['execution']['network_policy']['allow'] == ['10.77.0.0/24']
        remote.changed = True
        again = controller.run_saved(user, reply['run_id'], 'd'*32)
        manager.jobs[user.username].result(timeout=10)
        assert ev.read_json(workspace.run_path(again['run_id']) / 'workflow.json')['status'] == 'failed'
        assert len(launched) == 1
        with pytest.raises(samples.SampleRequestError):
            controller.create(user, 'f'*64, 'e'*32, '10.77.0.0/24', '')
        assert all(vmid == 9402 for vmid, _, _ in calls)
    finally:
        manager.close()


@pytest.mark.parametrize("value", [{}, {"repetitions":1}, dict(repetitions=True,max_turns=3,wall_seconds=30,tool_timeout=10,context_window=1000), dict(repetitions=1,max_turns=0,wall_seconds=30,tool_timeout=10,context_window=1000)])
def test_invalid_evaluation_settings(value):
    with pytest.raises(samples.SampleRequestError):
        scenarios.evaluation_settings(value)


def test_scope_uses_saved_topology_and_ignores_access_networks(tmp_path):
    import json
    import xml.etree.ElementTree as ET
    scenario = ET.Element('Scenario', name='Scope')
    preview = dict(full_preview=dict(lan_subnets=['10.77.0.2/24', '10.77.0.0/24', '0.0.0.0/0', 'invalid'],
                                    ptp_subnets=['10.78.0.0/30'], hosts=[dict(ip4='10.77.0.5/24')]))
    ET.SubElement(scenario, 'PlanPreview').text = json.dumps(preview)
    ET.SubElement(scenario, 'HardwareInLoop', address='10.254.200.3/24')
    path = tmp_path / 'scope.xml'
    ET.ElementTree(scenario).write(path)
    assert guest.inspect(path)[0]['target_subnets'] == ['10.77.0.0/24', '10.78.0.0/30']
    scenario.remove(scenario.find('PlanPreview'))
    ET.SubElement(scenario, 'FlowState').text = json.dumps(dict(chain=[dict(ipv4='10.77.0.5')]))
    assert guest.target_subnets(scenario) == ['10.77.0.5/32']


def test_saved_preview_chain_ids_are_listed_and_snapshot_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'scenarioforge'))
    import json
    state = dict(chain_ids=['docker-1'], flag_assignments=[
        dict(node_id='docker-1', id='131', flag_value='flag-observed', type='flag-node-generator')])
    source = tmp_path / 'saved-preview.xml'
    source.write_text('<Scenarios><Scenario name="Preview lab"><ScenarioEditor><FlagSequencing><FlowState>'
                      + json.dumps(state) + '</FlowState></FlagSequencing></ScenarioEditor></Scenario></Scenarios>')
    row = guest.inspect(source)[0]
    assert row['resolved_chain'] and row['chain_length'] == 1
    import xml.etree.ElementTree as ET
    scenario = ET.parse(source).getroot().find('Scenario')
    tasks, context = guest._scenario_task_details(scenario, state)
    assert context['chain'][0]['id'] == 'docker-1'
    assert tasks[0]['verification_mode'] == 'judge'
    assert tasks[0]['challenge_plan']['steps'][0]['node_id'] == 'docker-1'
    saved = guest.dispatch(dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path), path=str(source),
        token='c' * 32, selection_id=row['id'], user=pwd.getpwuid(os.getuid()).pw_name))
    assert Path(saved['snapshot_path']).read_bytes() == source.read_bytes()


@pytest.mark.parametrize('flag', ['flow_enabled', 'topology_dirty'])
def test_disabled_or_dirty_sequence_is_not_listed_as_resolved(tmp_path, flag):
    import json
    state = dict(chain_ids=['docker-1'], chain=[dict(id='docker-1')])
    state[flag] = flag == 'topology_dirty'
    source = tmp_path / 'disabled.xml'
    source.write_text('<Scenario name="Disabled"><FlowState>' + json.dumps(state) + '</FlowState></Scenario>')
    assert guest.inspect(source)[0]['resolved_chain'] is False


@pytest.mark.parametrize('dirty', [False, True])
def test_fixed_tasks_can_disable_generators_but_cannot_use_dirty_topology(tmp_path, dirty):
    state = dict(flow_enabled=False, topology_dirty=dirty, chain=[dict(id='web', ipv4='10.77.0.10')],
                 evaluation_tasks=[dict(id='web-task', family='web', prompt='Read the title.',
                    verifier=dict(type='contains_all', expected=['Demo']), required_checks=['ports'])])
    path = tmp_path / 'fixed.xml'
    path.write_text('<Scenario name="Fixed"><FlowState>' + json.dumps(state) + '</FlowState></Scenario>')
    row = guest.inspect(path)[0]
    assert row['resolved_chain'] is (not dirty)
    if not dirty:
        captured = guest.dispatch(dict(op='snapshot', roots=[str(tmp_path)], repo=str(tmp_path),
            path=str(path), token='f'*32, selection_id=row['id'], user=pwd.getpwuid(os.getuid()).pw_name))
        assert Path(captured['snapshot_path']).read_bytes() == path.read_bytes()


def test_five_reviewed_bundles_are_selectable_without_running_generators(tmp_path, monkeypatch):
    import runpy
    importer = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scenarioforge/webapp/reproduction_bundle.py'))['import_scenario_file']
    bundles = sorted((Path(__file__).resolve().parents[1] / 'ScenarioForge-Bundles').glob('*.zip'))
    assert len(bundles) == 5
    for bundle in bundles:
        imported = importer(str(bundle), str(tmp_path / bundle.stem))
        rows = guest.inspect(Path(imported.xml_path))
        assert len(rows) == 1 and rows[0]['resolved_chain'], bundle.name


@pytest.mark.parametrize('limit',[0,1001,True,'6'])
def test_invalid_max_tries_before_solution(limit):
    settings = dict(repetitions=1,max_turns=9,wall_seconds=300,tool_timeout=30,context_window=8192,
                    max_tries_before_solution=limit)
    with pytest.raises(samples.SampleRequestError,match='max_tries_before_solution'):
        scenarios.evaluation_settings(settings)


def test_evaluation_settings_accept_legacy_clients_and_custom_solution_limit():
    settings = dict(repetitions=1,max_turns=9,wall_seconds=300,tool_timeout=30,context_window=8192)
    assert scenarios.evaluation_settings(settings) == settings
    settings['max_tries_before_solution'] = 7
    assert scenarios.evaluation_settings(settings)['max_tries_before_solution'] == 7
