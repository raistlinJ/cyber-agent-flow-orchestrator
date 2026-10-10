"""Saved ScenarioForge selector browser check; guest access is simulated."""
from pathlib import Path
import shutil
import json
import hashlib
import sys
import tempfile
import time
import pytest
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator import scenarios
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm


def main():
    task_definitions=[dict(id='read-token',family='http',prompt='Read the token from http://10.77.0.10/.',verifier=dict(type='json_equals',expected={'token':'expected-token'}),required_checks=['containers','ports'],progressive_hints=['Fetch the starting URL with curl.'])]
    with tempfile.TemporaryDirectory() as temp, pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        class Guest:
            content = b''
            def put(self, vmid, path, content):
                Guest.content = content
            def call(self, vmid, op, **data):
                item = dict(id='a'*64, scenario='Demo saved scenario', path='/opt/scenarioforge/uploads/demo.xml',
                            sha256='b'*64, bytes=1024, resolved_chain=True, chain_length=3,target_subnets=['10.77.0.0/24'],modified_epoch=1000,modified_at='1970-01-01T00:16:40+00:00')
                if op == 'tasks':
                    if data['selection_id']=='d'*64:
                        draft=dict(id='collect-flags',family='flag-collection',prompt='Collect the flag from 9: target (10.77.0.20).',flag_nodes=['9'],required_checks=['containers','services','ports'],progressive_hints=['Inspect the target web service.'])
                        details=dict(tasks=None,suggested_tasks=[draft],context=dict(
                            scenario='Generated Flow',flag_nodes=['9'],progressive_hints=draft['progressive_hints'],suggested_checks=draft['required_checks'],
                            sources=dict(tasks='FlowState chain and flag assignments',prompt='resolved Flow targets',success='fresh flags generated for Flow nodes',readiness='deployed Flow topology',hints='saved public Flow hint fields'),
                            chain=[dict(position=1,id='9',name='target',ipv4='10.77.0.20',is_vuln=True,generator='web-flag',has_flag=True,hint_count=1)]))
                    else:
                        details=dict(tasks=task_definitions,suggested_tasks=[],context=dict(
                            scenario='Demo saved scenario',flag_nodes=[],progressive_hints=[],suggested_checks=['containers','ports'],
                            sources=dict(tasks='FlowState.evaluation_tasks',prompt='saved task prompt',success='saved verifier',readiness='saved task checks',hints='saved public Flow hint fields'),
                            chain=[dict(position=1,id='7',name='web',ipv4='10.77.0.10',is_vuln=True,generator='',has_flag=False,hint_count=0)]))
                    content=json.dumps(details)
                    return dict(chunk=content[data['offset']:data['offset']+4096],total=len(content),sha256=hashlib.sha256(content.encode()).hexdigest())
                if op == 'list':
                    return dict(items=[item, dict(item, id='c'*64, scenario='Unresolved', resolved_chain=False,modified_epoch=2000,modified_at='1970-01-01T00:33:20+00:00'),
                                            dict(item, id='d'*64, scenario='Generated Flow', path='/opt/scenarioforge/uploads/generated.xml', chain_length=1,modified_epoch=3000,modified_at='1970-01-01T00:50:00+00:00')],
                                roots=data['roots'], truncated=False)
                if op == 'upload-start':
                    return {'path':'/opt/scenarioforge/uploads/source'}
                if op == 'upload-import':
                    time.sleep(1)
                    bundle = Guest.content.startswith(b'PK')
                    return dict(items=[dict(item, resolved_chain=not bundle)],path=item['path'],
                                kind='reproduction-bundle' if bundle else 'xml',fidelity='portable-artifacts' if bundle else 'definition')
                assert op == 'snapshot'
                return dict(item, snapshot_path='/opt/scenarioforge/outputs/caf-orchestrator/'+data['token']+'/scenario.xml')
        patch.setattr(scenarios, 'guest', lambda backend: Guest())
        from cyber_agent_flow_eval import integration as ev
        patch.setattr(ev, 'GuestAgent', lambda backend: Guest())
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403), vm(9404)]
            dashboard = UserDashboard(root / 'examples/01-reuse-export.yaml', root / 'runs', 2, lambda b,a: Probe(b,a,[]))
            try:
                with secure_server(dashboard, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as pw:
                    browser = pw.chromium.launch(channel='chrome', headless=True)
                    page = browser.new_page(ignore_https_errors=True, viewport={'width':1280,'height':1000})
                    errors=[]
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.route('**/api/model-network-scope',lambda route:route.fulfill(json=dict(provider_host='models.example',excluded_targets=['192.0.2.8','192.0.2.1'],warnings=[])))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password', exact=True).fill(PASSWORD)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.locator('[data-route=setup]').click()
                    page.locator('#role-scenarioforge').select_option('9402')
                    page.locator('#role-participant').select_option('9403')
                    page.get_by_role('button', name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click()
                    page.locator('#new-experiment').click()
                    page.get_by_role('tab',name="Experiment",exact=True).click()
                    page.get_by_label('Experiment type', exact=True).select_option('smoke')
                    expect(page.locator('#experiment-sample option').nth(0)).to_have_text('Sample - Model smoke test')
                    expect(page.locator('#experiment-sample option').nth(1)).to_have_text('Sample - Tools vs added helper')
                    page.get_by_role('tab',name="ScenarioForge",exact=True).click()
                    expect(page.locator('#experiment-panel-overview a[download]')).to_have_count(0)
                    expect(page.locator('#sample-scenario-package')).to_be_visible()
                    expect(page.locator('#sample-scenario-xml')).to_have_attribute('href','/demo-smoke.xml')
                    expect(page.locator('#sample-scenario-bundle')).to_have_attribute('href','/demo-smoke.zip')
                    expect(page.locator('#sample-scenario-info')).to_be_visible()
                    expect(page.locator('#experiment-panel-scenario #sample-prompt')).to_have_count(0)
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    expect(page.locator('#sample-prompt')).to_be_visible()
                    expect(page.locator('#sample-prompt')).to_be_disabled()
                    expect(page.locator('#sample-task-id')).to_have_value('smoke')
                    expect(page.locator('#provide-progressive-hints')).not_to_be_checked()
                    expect(page.locator('#provide-progressive-hints')).to_be_enabled()
                    page.locator('#provide-progressive-hints').check()
                    page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                    expect(page.locator('#sample-sf-use')).to_be_disabled()
                    expect(page.locator('#sample-sf-use')).to_have_value('Required · deploy, readiness check and evaluation export')
                    expect(page.locator('#sample-prompt')).to_have_value('Fetch http://<deployed-host>/ and read the service token from its response body. Return only JSON with service_token set to the exact observed token.')
                    expect(page.locator('#eval-max_turns')).to_be_enabled()
                    expect(page.locator('#eval-max_turns')).to_have_value('6')
                    page.get_by_role('tab',name="Experiment",exact=True).click()
                    page.get_by_label('Experiment type', exact=True).select_option('tools-vs-helper')
                    page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                    expect(page.locator('#sample-scenario-package')).to_be_visible()
                    expect(page.locator('#scenario-caf-scope')).to_be_hidden()
                    expect(page.locator('#sample-scenario-xml')).to_have_attribute('href','/demo-tools-vs-helper.xml')
                    expect(page.locator('#sample-scenario-bundle')).to_have_attribute('href','/demo-tools-vs-helper.zip')
                    expect(page.locator('#eval-repetitions')).to_have_value('3')
                    expect(page.locator('#sample-sf-companion')).to_have_value('demo-tools-vs-helper.xml · fixed sample XML')
                    expect(page.locator('#caf-settings-summary')).to_have_count(0)
                    page.get_by_role('tab',name="Experiment",exact=True).click()
                    page.get_by_label('Experiment type', exact=True).select_option('scenarioforge-xml')
                    expect(page.locator('#sample-scenario-info')).to_be_hidden()
                    expect(page.locator('#eval-max_turns')).to_be_enabled()
                    page.get_by_role('tab',name="Evaluation",exact=True).click()
                    page.locator('#eval-max_turns').fill('9')
                    page.get_by_role('tab',name="Evaluation",exact=True).click()
                    page.locator('#eval-repetitions').fill('2')
                    page.get_by_role('tab',name="ScenarioForge",exact=True).click()
                    expect(page.locator('#scenario-upload-options')).to_be_hidden()
                    expect(page.locator('#scenario-find-options')).to_be_visible()
                    expect(page.locator('#scenario-query')).to_have_count(0)
                    page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                    expect(page.locator('#scenario-caf-scope')).to_be_visible()
                    expect(page.locator('#scenario-allowed')).to_be_visible()
                    expect(page.locator('#scenario-disallowed')).to_be_visible()
                    page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                    for width in [1440,390]:
                        page.set_viewport_size({'width':width,'height':1080 if width==1440 else 844})
                        assert page.locator('.scenario-source-choice').evaluate('''node=>{
                            const labels=[...node.querySelectorAll('label')];
                            const boxes=labels.map(label=>label.getBoundingClientRect());
                            const radios=[...node.querySelectorAll('input')];
                            return radios.every(r=>r.getBoundingClientRect().width<=20)
                                && boxes.every(b=>b.right<=innerWidth)
                                && (boxes[0].right<=boxes[1].left || boxes[0].bottom<=boxes[1].top);
                        }''')
                        page.screenshot(path=f'/tmp/caf-scenario-source-{width}.png')
                    page.set_viewport_size({'width':1440,'height':1080})
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.route('**/api/scenarios/list',lambda route:route.fulfill(status=504,content_type='text/plain',body='Dashboard request timed out'))
                    page.locator('#load-scenarios').click()
                    expect(page.locator('#scenario-selection-info')).to_contain_text('Dashboard request timed out',timeout=30000)
                    expect(page.locator('#scenario-selection-info')).not_to_contain_text('Unexpected token')
                    expect(page.locator('#loading-modal')).not_to_be_visible()
                    page.unroute('**/api/scenarios/list')
                    page.locator('#load-scenarios').click()
                    expect(page.locator('#scenario-selection option')).to_have_count(4, timeout=30000)
                    expect(page.locator('#scenario-selection option').nth(1)).to_have_attribute('value','d'*64)
                    assert page.locator('#scenario-selection option').nth(2).is_disabled()
                    expect(page.locator('#scenario-selection option').nth(3)).to_have_attribute('value','a'*64)
                    page.get_by_role('tab',name="ScenarioForge",exact=True).click()
                    page.locator('#scenario-selection').select_option('a'*64)
                    expect(page.locator('#scenario-selection-info')).to_contain_text('demo.xml')
                    page.get_by_role('tab',name="Experiment",exact=True).click()
                    page.get_by_role('tab',name="ScenarioForge",exact=True).click()
                    page.get_by_label('Upload XML or bundle',exact=True).check()
                    expect(page.locator('#scenario-find-options')).to_be_hidden()
                    expect(page.locator('#scenario-upload-options')).to_be_visible()
                    expect(page.locator('#scenario-selection')).to_have_value('')
                    expect(page.locator('#upload-scenario')).to_be_disabled()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#scenario-file').set_input_files({'name':'uploaded.xml','mimeType':'application/xml','buffer':b'<Scenarios><Scenario name="Uploaded"/></Scenarios>'+b' '*12000})
                    expect(page.locator('#upload-scenario')).to_be_enabled()
                    page.get_by_role('tab',name="ScenarioForge",exact=True).click()
                    page.locator('#upload-scenario').click()
                    expect(page.locator('#loading-modal')).to_be_visible()
                    expect(page.locator('#scenario-upload-info')).to_contain_text('definition',timeout=30000)
                    expect(page.locator('#scenario-selection')).to_have_value('a'*64)
                    expect(page.locator('#upload-scenario')).to_be_disabled()
                    page.locator('#scenario-file').set_input_files({'name':'different.xml','mimeType':'application/xml','buffer':b'<Scenarios><Scenario name="Different"/></Scenarios>'+b' '*12000})
                    expect(page.locator('#upload-scenario')).to_be_enabled()
                    page.get_by_label('Find on ScenarioForge VM',exact=True).check()
                    expect(page.locator('#scenario-upload-options')).to_be_hidden()
                    page.locator('#load-scenarios').click()
                    expect(page.locator('#scenario-selection option')).to_have_count(4,timeout=30000)
                    page.locator('#scenario-selection').select_option('a'*64)
                    page.get_by_role('tab',name="Cyber-agent-flow",exact=True).click()
                    expect(page.locator('#scenario-allowed')).to_have_value('10.77.0.0/24')
                    expect(page.locator('#scenario-disallowed')).to_have_value('10.78.0.254/32, 192.0.2.8, 192.0.2.1')
                    page.locator('#scenario-allowed').fill('')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#scenario-allowed').fill('10.77.0.0/24')
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.locator('#eval-repetitions').fill('0')
                    page.get_by_role('tab',name='Experiment',exact=True).click()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.locator('#eval-repetitions').fill('2')
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.locator('#load-scenario-tasks').click()
                    expect(page.locator('#task-source-note')).to_contain_text('Loaded 1 task')
                    expect(page.locator('#task-scenario-context')).to_contain_text('1 Flow step')
                    expect(page.locator('#task-scenario-context')).to_contain_text('web · 10.77.0.10')
                    expect(page.locator('#task-0-prompt')).to_be_disabled()
                    expect(page.locator('#task-0-prompt')).to_have_value(task_definitions[0]['prompt'])
                    page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                    page.locator('#scenario-selection').select_option('d'*64)
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.locator('#load-scenario-tasks').click()
                    expect(page.locator('#task-source-note')).to_contain_text('Built 1 editable draft')
                    expect(page.locator('#task-scenario-context')).to_contain_text('1 generated-flag target')
                    expect(page.locator('#task-0-criteria')).to_have_value(json.dumps({'flag_nodes':['9']},indent=2))
                    expect(page.locator('#edit-scenario-tasks')).to_have_text('Use and edit scenario draft')
                    page.screenshot(path='/tmp/caf-scenario-derived-task.png')
                    page.locator('#edit-scenario-tasks').click()
                    expect(page.locator('#task-0-prompt')).to_be_enabled()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                    page.locator('#scenario-selection').select_option('a'*64)
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.locator('#load-scenario-tasks').click()
                    expect(page.locator('#task-source-note')).to_contain_text('Loaded 1 task')
                    page.locator('#task-source').select_option('scenario')
                    page.locator('#edit-scenario-tasks').click()
                    expect(page.locator('#task-0-prompt')).to_be_enabled()
                    page.locator('#task-0-prompt').fill('')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.get_by_role('tab',name='Experiment',exact=True).click()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.locator('#task-0-prompt').fill('Edited task prompt')
                    page.locator('#add-task').click()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('[data-task-row="1"]').get_by_role('button',name='Remove task',exact=True).click()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    imported=[dict(task_definitions[0],prompt='Review the HTTP service. '+('Detailed instructions. '*500)),dict(id='read-title',family='http',prompt='Read the page title.',verifier=dict(type='contains_all',expected=['Demo']),required_checks=['services'],split='test')]
                    page.locator('#task-import').set_input_files({'name':'tasks.json','mimeType':'application/json','buffer':json.dumps(imported).encode()})
                    expect(page.locator('[data-task-row]')).to_have_count(2)
                    expect(page.locator('#task-1-id')).to_have_value('read-title')
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.locator('#task-1-id').fill('read-token')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#task-1-id').fill('read-title')
                    page.locator('#task-1-criteria').fill('{broken')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#task-1-criteria').fill(json.dumps(imported[1]['verifier']))
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path='/tmp/caf-evaluation-tasks-mobile.png')
                    page.set_viewport_size({'width':1280,'height':1000})
                    page.screenshot(path='/tmp/caf-evaluation-tasks.png')
                    page.screenshot(path='/tmp/caf-scenario-selector.png')
                    page.get_by_role('button', name='Create experiment', exact=True).click()
                    expect(page.locator('#experiment-dialog')).to_be_hidden(timeout=30000)
                    row=page.locator('#runs tr').filter(has_text='Demo saved scenario')
                    expect(row).to_contain_text('ready')
                    expect(row.get_by_role('button',name='Deploy and run',exact=True)).to_be_enabled()
                    journals=list((root/'runs').rglob('workflow.json'))
                    saved=json.loads(journals[0].read_text())
                    assert saved['runtime']['execution']['network_policy'] == dict(allow=['10.77.0.0/24'],disallow=['10.78.0.254/32','192.0.2.8/32','192.0.2.1/32'])
                    assert saved['runtime']['execution']['provide_progressive_hints'] is True
                    assert saved['runtime']['execution']['max_turns'] == 9
                    assert saved['runtime']['repetitions'] == 2
                    expected_tasks=[dict(t,verification_mode='exact',split=t.get('split','development')) for t in imported]
                    assert saved['scenario_experiment']['evaluation_tasks'] == expected_tasks
                    assert json.loads((journals[0].parent/'inputs/evaluation-tasks.json').read_text()) == expected_tasks
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print('Scenario selector browser check passed')


if __name__ == '__main__':
    main()
