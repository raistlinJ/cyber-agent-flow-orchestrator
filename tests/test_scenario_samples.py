"""Full-path sample integration: only guest transport and model execution are simulated."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import scenarios, service, demo_prepare
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve
from test_user_access import access, vm, Probe
from test_workflow import lab


@pytest.mark.parametrize('sample_id,trials', [('smoke',1),('tools-vs-helper',6)])
def test_samples_deploy_export_evaluate_and_capture(pve,lab,tmp_path,monkeypatch,sample_id,trials):
    pve[0]['resources']['operator@pve']=[vm(9402),vm(9403),vm(9404)]
    user=access(pve)
    dashboard=UserDashboard(lab[0],tmp_path/'runs',2,lambda b,a:Probe(b,a,[]))
    workspace=Workspace(tmp_path/'runs',user.username)
    workspace.save_roles(dict(scenarioforge=9402,participant=9403,core=9404),user)
    agent,manifest=lab[2],lab[3]
    agent.put=lambda vmid,path,content: agent.files.update({path:content})
    monkeypatch.setattr(ev,'GuestAgent',lambda backend:agent)
    class Remote:
        def call(self,vmid,op,**data):
            item=dict(id='a'*64,scenario='Fixed demo',path='/uploads/demo.xml',sha256='b'*64,
                      resolved_chain=False,chain_length=0,bytes=100)
            if op=='upload-start': return {'path':'/uploads/source'}
            if op=='upload-import': return dict(items=[item],path=item['path'],kind='reproduction-bundle',fidelity='portable-artifacts')
            if op=='snapshot':
                assert data['allow_unresolved']
                return dict(item,snapshot_path='/saved/demo.xml')
            assert op=='check'
            return {}
    monkeypatch.setattr(scenarios,'guest',lambda backend:Remote())
    try:
        result=dashboard.experiment(user,'create',dict(sample_id=sample_id,request_id='a'*32))
        output=workspace.run_path(result['run_id'])
        saved=ev.read_json(output/'workflow.json')
        assert saved['workflow']['scenarioforge']['mode']=='execute'
        assert saved['workflow']['prepare'][0]['id']=='fixed-demo-xml'
        assert saved['sample_id']==sample_id
        assert not agent.calls  # Import transfers only; deployment waits for Run.
        agent.marker=dict(state='complete',readiness_passed=True,archive='/exports/suite.zip',
                          package_hash=manifest['package_hash'],suite_id=saved['workflow']['scenarioforge']['suite_id'])
        dashboard.experiment(user,'run',dict(run_id=result['run_id'],request_id='b'*32))
        dashboard.samples.jobs[user.username].result(timeout=15)
        report=service.results(output)
        assert report['workflow']['recorded_status']=='completed',ev.read_json(output/'workflow.json')
        assert report['evaluation']['planned_trials']==trials
        assert report['run_configuration']['scenarioforge']['used']
        assert report['run_configuration']['scenarioforge']['xml_available']
        assert (output/'inputs/uploaded-source.zip').is_file()
        assert (output/'reproduction/scenarioforge-reproduction.zip').is_file()
        deploy=[data for _,op,data in agent.calls if op=='hook' and 'scenarioforge.cli' in data['argv']]
        assert len(deploy)==1 and '--evaluation-export' in deploy[0]['argv']
        spec=ev.resolve(output/'study.yaml')
        assert spec['execution']['network_policy']['allow']==['10.77.0.10/32','10.77.0.20/32']
        assert len(spec['conditions'])==(1 if sample_id=='smoke' else 2)
    finally:
        dashboard.close()


@pytest.mark.parametrize('sample_id',['smoke','tools-vs-helper'])
def test_preparation_uses_resolved_host_and_fresh_verifier(tmp_path,sample_id):
    assets=tmp_path/'imported'
    (assets/'site/deeper').mkdir(parents=True)
    (assets/'docker-compose.yml').write_text('services: {}')
    (assets/'site/index.html').write_text('<a href="/first.html">Start</a>')
    source=tmp_path/'source.xml'
    root=ET.Element('Scenarios')
    scene=ET.SubElement(root,'Scenario',name='Fixed demo')
    editor=ET.SubElement(scene,'ScenarioEditor')
    sec=ET.SubElement(editor,'section',name='Vulnerabilities')
    ET.SubElement(sec,'item',v_path='/old/docker-compose.yml')
    node=ET.SubElement(ET.SubElement(editor,'FlagSequencing'),'FlowState')
    node.text=json.dumps(dict(chain=[],reproduction_artifact_sources=[dict(restored_path=str(assets),target_path='/old')]))
    ET.ElementTree(root).write(source)
    class Backend:
        def _core_backend_defaults(self,**kwargs): return {'host':'fixture.invalid'}
        def _build_scenarios_xml(self,data):
            tree=ET.ElementTree(ET.Element('Scenarios'))
            ET.SubElement(tree.getroot(),'CoreConnection',host=data['core']['host'])
            return tree
        def _planner_persist_flow_plan(self,**kwargs):
            tree=ET.parse(kwargs['xml_path'])
            preview=ET.SubElement(tree.getroot().find('.//ScenarioEditor'),'PlanPreview')
            preview.text=json.dumps({'full_preview':{'hosts':[dict(node_id=7,name='web',ip4='10.77.0.7/24',
                vulnerabilities=['caf-demo-'+sample_id])]}})
            tree.write(kwargs['xml_path'])
            return {'persisted':True}
    output=tmp_path/'run/scenario.xml'
    options=dict(source=str(source),destination=str(output),sample_id=sample_id,scenario='Fixed demo')
    demo_prepare.prepare(options,Backend())
    flow=json.loads(ET.parse(output).find('.//FlowState').text)
    task=flow['evaluation_tasks'][0]
    assert flow['chain'][0]['ipv4']=='10.77.0.7'
    assert 'http://10.77.0.7/' in task['prompt']
    expected=task['verifier']['expected']
    assert '10.77.0.7' not in json.dumps(expected)
    assert all(value not in task['prompt'] for value in ([expected['service_token']] if sample_id=='smoke' else expected['flags']))
    demo_prepare.prepare(options,Backend())
    updated=json.loads(ET.parse(output).find('.//FlowState').text)['evaluation_tasks'][0]['verifier']['expected']
    assert expected!=updated
