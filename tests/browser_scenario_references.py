"""Reference popups through authenticated HTTPS and simulated VM exports."""
from pathlib import Path
import base64
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
import pytest
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm


def main():
    with tempfile.TemporaryDirectory() as temp, pytest.MonkeyPatch.context() as patch:
        root=Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples',root/'examples')
        item=dict(id='a'*64, scenario='Reference lab',path='/opt/scenarioforge/uploads/reference.xml',sha256='b'*64,
                  bytes=1024,resolved_chain=True,chain_length=2,target_subnets=['10.77.0.0/24'],modified_epoch=1000,modified_at='2026-10-08T10:00:00+00:00')
        task=dict(id='inspect-service',family='web',prompt='Inspect the target service and report the observed result.',
            verifier=dict(type='contains_all',expected=['observed']),required_checks=['services'],verification_mode='both',
            rubric=dict(version=1,criteria=[dict(id='service',requirement='Inspect the target service.',evidence='Successful tool response.',essential=True,weight=1)]),
            challenge_plan=dict(version=1,steps=[dict(id='step-001',node_id='2',title='Target service',criterion_ids=['service'],requires=[],
                hints=['Inspect the HTTP service.', 'Read its response body.'],solution='Retrieve and report the service result.')]))
        calls=[]
        class Guest:
            def call(self,vmid,op,**data):
                calls.append((vmid,op))
                if op == 'model_config' and data.get('action') == 'tools':
                    tools=[dict(name=name,command=name,description=name,risk='standard',origin='catalog',category='Catalog tools') for name in ('nmap','curl','python3')]
                    return dict(catalog={'tools':[dict(name=item['name'],command=item['command'],description=item['description']) for item in tools]},tools=tools,assets=[],source_path='/opt/cyber-agent-flow/kali_tools.default.json')
                if op=='list':return dict(items=[item],truncated=False)
                if op=='tasks':
                    details=dict(tasks=[task],suggested_tasks=[],context=dict(scenario='Reference lab',flag_nodes=[],progressive_hints=[],sources=dict(tasks='FlowState.evaluation_tasks',prompt='saved task',success='saved verifier',readiness='saved checks',hints='none'),chain=[dict(position=1,id='2',name='Target',ipv4='10.77.0.10',is_vuln=True,generator='',has_flag=False,hint_count=0)]))
                    content=json.dumps(details);return dict(chunk=content[data['offset']:data['offset']+4096],total=len(content),sha256=hashlib.sha256(content.encode()).hexdigest())
                assert op=='references'
                payload=dict(kind=data['kind'],scenario=item['scenario'],source=item['path'],xml_sha256=item['sha256'])
                if data['kind']=='attack-graph':payload.update(graph=dict(nodes=[dict(id='1',name='Start'),dict(id='2',name='Target')],edges=[dict(source='1',target='2')]),dot='digraph {1->2}')
                elif data['kind']=='participant-guide':payload.update(markdown='# Saved participant guide\n\nInspect the target service.\n\n- Read the response\n\n<script>window.UNSAFE_GUIDE=true</script>',reference_origin='uploaded-bundle')
                else:payload['html']='<!doctype html><html><head><style>h1{color:rgb(0, 128, 0)}</style></head><body><h1>ScenarioForge exported guide</h1><p>Inspect the target service.</p><script>window.UNSAFE_GUIDE=true</script></body></html>'
                encoded=base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
                return dict(chunk=encoded[data['offset']:data['offset']+16384],total=len(encoded),sha256=hashlib.sha256(encoded.encode()).hexdigest())
        patch.setattr(ev,'GuestAgent',lambda backend,authorize=None:Guest())
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve']=[vm(9402),vm(9403),vm(9404)]
            dashboard=UserDashboard(root/'examples/01-reuse-export.yaml',root/'runs',2,lambda b,a:Probe(b,a,[]))
            try:
                with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server,sync_playwright() as playwright:
                    browser=playwright.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(ignore_https_errors=True,viewport=dict(width=1440,height=1080))
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.route('**/api/model-network-scope',lambda route:route.fulfill(json=dict(provider_host='models.example',excluded_targets=['192.0.2.8'],warnings=[])))
                    page.goto(server['origin']);page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD);page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click();page.locator('#role-scenarioforge').select_option('9402');page.locator('#role-participant').select_option('9403');page.locator('#role-core').select_option('9404');page.get_by_role('button',name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click();page.locator('#new-experiment').click();page.locator('#experiment-sample').select_option('scenarioforge-xml');page.locator('[data-wizard-step=scenario]').click()
                    expect(page.locator('#scenario-options [data-scenario-reference]')).to_have_count(0)
                    expect(page.locator('#scenario-reference-choice')).to_be_disabled()
                    expect(page.locator('.scenario-reference-actions summary')).to_have_text('View/Download Scenario Info')
                    page.locator('#load-scenarios').click();expect(page.locator('#scenario-selection option')).to_have_count(2,timeout=30000);page.locator('#scenario-selection').select_option(item['id'])
                    page.locator('[data-wizard-step=tasks]').click()
                    expect(page.locator('#task-load-state')).to_have_text('Ready',timeout=30000)
                    card=page.locator('.task-review-card')
                    expect(card.get_by_role('tab',name='Verification',exact=True)).to_have_attribute('aria-selected','true')
                    expect(card.locator('.task-criteria-list')).to_be_visible()
                    card.get_by_role('tab',name='Readiness',exact=True).click()
                    expect(card.locator('.task-readiness-list')).to_have_text('services')
                    expect(card.locator('.task-criteria-list')).to_be_hidden()
                    card.get_by_role('tab',name='Progressive hints',exact=True).click()
                    expect(card.locator('#task-review-0-panel-hints')).to_contain_text('2 challenge hints across 1 step when enabled')
                    expect(card.locator('.task-hint-list li')).to_have_text(['Inspect the HTTP service.', 'Read its response body.'])
                    expect(card.locator('#task-review-0-panel-hints h5')).to_have_text('Target service')
                    card.get_by_role('tab',name='Progressive hints',exact=True).press('Home')
                    expect(card.get_by_role('tab',name='Verification',exact=True)).to_be_focused()
                    expect(card.locator('.task-criteria-list')).to_be_visible()
                    card.get_by_role('tab',name='Verification',exact=True).press('End')
                    expect(card.get_by_role('tab',name='Progressive hints',exact=True)).to_be_focused()
                    expect(card.locator('.task-hint-list')).to_be_visible()
                    expect(card.get_by_text('Evaluation details',exact=True)).to_have_count(0)
                    page.locator('#task-scenario-context > summary').click()
                    source=page.locator('#task-scenario-context')
                    expect(source.locator('summary')).to_have_text('Scenario details')
                    expect(source.get_by_role('tab')).to_have_count(0)
                    expect(source.locator('tbody tr')).to_contain_text('Target')
                    expect(source.locator('tbody tr')).to_contain_text('10.77.0.10')
                    expect(source).not_to_contain_text('saved verifier')
                    expect(card.get_by_role('tab',name='Progressive hints',exact=True)).to_have_attribute('aria-selected','true')
                    task_prompt=page.locator('.task-prompt-review').text_content()
                    expect(page.locator('.scenario-reference-actions')).not_to_have_attribute('open','')
                    page.locator('.scenario-reference-actions summary').click()
                    with page.expect_popup() as popup:
                        page.locator('#scenario-reference-choice').select_option('attack-graph')
                    graph=popup.value;graph.on('pageerror',lambda error:errors.append(str(error)))
                    expect(graph.locator('#reference-title')).to_have_text('Attack graph · Reference lab',timeout=30000);expect(graph.locator('svg[role="img"]')).to_be_visible();expect(graph.locator('#reference-downloads a')).to_have_count(2);expect(graph.locator('#loading-modal')).not_to_be_visible();graph.screenshot(path='/tmp/caf-scenario-attack-graph.png');graph.close()
                    page.locator('[data-wizard-step=tasks]').click()
                    for kind in ['participant-guide','facilitator-guide']:
                        with page.expect_popup() as popup:page.locator('#scenario-reference-choice').select_option(kind)
                        guide=popup.value;guide.on('pageerror',lambda error:errors.append(str(error)))
                        if kind=='participant-guide':
                            expect(guide.locator('.markdown-guide h1')).to_have_text('Saved participant guide',timeout=30000)
                            expect(guide.locator('.markdown-guide li')).to_have_text('Read the response')
                            expect(guide.locator('#reference-status')).to_contain_text('uploaded bundle')
                            assert guide.evaluate('typeof window.UNSAFE_GUIDE')=='undefined'
                        else:
                            expect(guide.locator('iframe')).to_be_visible(timeout=30000);frame=guide.frame_locator('iframe');expect(frame.locator('h1')).to_have_text('ScenarioForge exported guide');expect(frame.locator('h1')).to_have_css('color','rgb(0, 128, 0)')
                            assert guide.frames[1].evaluate('typeof window.UNSAFE_GUIDE')=='undefined'
                        guide.screenshot(path='/tmp/caf-scenario-'+kind+'.png');guide.close()
                        expect(page.locator('.task-prompt-review')).to_have_text(task_prompt)
                    page.context.route('**/api/scenarios/references',lambda route:route.fulfill(status=400,json=dict(error='Scenario XML changed; reload the scenario list.')))
                    with page.expect_popup() as popup:page.locator('#scenario-reference-choice').select_option('attack-graph')
                    failed=popup.value
                    expect(failed.locator('#reference-error')).to_contain_text('Scenario XML changed',timeout=30000)
                    expect(failed.locator('#loading-modal')).not_to_be_visible()
                    expect(failed.locator('#reload-reference')).to_be_enabled()
                    failed.close()
                    page.context.unroute('**/api/scenarios/references')
                    page.evaluate('window.originalOpen=window.open;window.open=()=>null;')
                    page.locator('#scenario-reference-choice').select_option('attack-graph')
                    expect(page.locator('#scenario-reference-notice')).to_be_visible()
                    expect(page.locator('#scenario-reference-notice a')).to_have_attribute('href','/scenario-reference?selection='+item['id']+'&kind=attack-graph')
                    page.evaluate('window.open=window.originalOpen;')
                    expect(page.locator('#experiment-builder')).to_be_visible();assert not errors,errors
                    expect(page.get_by_text('Define my own task for this experiment',exact=True)).to_have_count(0)
                    expect(page.locator('.task-prompt-review')).to_have_text(task_prompt)
                    page.locator('[data-wizard-step=judge]').click();page.locator('#judge-enabled').check()
                    page.locator('[data-wizard-step=helpers]').click()
                    page.get_by_label('Enable HTTP flag walker',exact=True).check()
                    controls=page.evaluate('experimentControls()')
                    assert controls['helpers']==['http-helper'] and controls['reset_each_trial']
                    assert len(controls['conditions'])<=1
                    page.locator('[data-wizard-step=tasks]').click()
                    card.get_by_role('tab',name='Verification',exact=True).click()
                    page.locator('.scenario-reference-actions > summary').click()
                    source.scroll_into_view_if_needed()
                    page.screenshot(path='/tmp/caf-task-review.png')
                    page.set_viewport_size(dict(width=390,height=844))
                    card.get_by_role('tab',name='Progressive hints',exact=True).click()
                    expect(card.locator('.task-hint-list')).to_be_visible()
                    expect(source.locator('.scenario-chain-table')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path='/tmp/caf-scenario-references-mobile.png')
                    assert all(op in ('list','tasks','references','model_config') for _,op in calls),calls
                    browser.close()
            finally:dashboard.close()
    print('PASS: reference popups, graph, formatted sandboxed guides, task review preserved, mobile layout')


if __name__=='__main__':main()
