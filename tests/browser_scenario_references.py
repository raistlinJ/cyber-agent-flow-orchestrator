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
        calls=[]
        class Guest:
            def call(self,vmid,op,**data):
                calls.append((vmid,op))
                if op=='list':return dict(items=[item],truncated=False)
                assert op=='references'
                payload=dict(kind=data['kind'],scenario=item['scenario'],source=item['path'],xml_sha256=item['sha256'])
                if data['kind']=='attack-graph':payload.update(graph=dict(nodes=[dict(id='1',name='Start'),dict(id='2',name='Target')],edges=[dict(source='1',target='2')]),dot='digraph {1->2}')
                elif data['kind']=='participant-guide':payload.update(markdown='# Saved participant guide\n\nInspect the target service.\n\n- Read the response\n\n<script>window.UNSAFE_GUIDE=true</script>',reference_origin='uploaded-bundle')
                else:payload['html']='<!doctype html><html><head><style>h1{color:rgb(0, 128, 0)}</style></head><body><h1>ScenarioForge exported guide</h1><p>Inspect the target service.</p><script>window.UNSAFE_GUIDE=true</script></body></html>'
                encoded=base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
                return dict(chunk=encoded[data['offset']:data['offset']+16384],total=len(encoded),sha256=hashlib.sha256(encoded.encode()).hexdigest())
        patch.setattr(ev,'GuestAgent',lambda backend:Guest())
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve']=[vm(9402),vm(9403)]
            dashboard=UserDashboard(root/'examples/01-reuse-export.yaml',root/'runs',2,lambda b,a:Probe(b,a,[]))
            try:
                with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server,sync_playwright() as playwright:
                    browser=playwright.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(ignore_https_errors=True,viewport=dict(width=1440,height=1080))
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.route('**/api/model-network-scope',lambda route:route.fulfill(json=dict(provider_host='models.example',excluded_targets=['192.0.2.8'],warnings=[])))
                    page.goto(server['origin']);page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD);page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click();page.locator('#role-scenarioforge').select_option('9402');page.locator('#role-participant').select_option('9403');page.get_by_role('button',name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click();page.locator('#new-experiment').click();page.locator('#experiment-sample').select_option('scenarioforge-xml');page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                    expect(page.locator('#scenario-options [data-scenario-reference]')).to_have_count(0)
                    expect(page.locator('#scenario-reference-choice')).to_be_disabled()
                    page.locator('#load-scenarios').click();expect(page.locator('#scenario-selection option')).to_have_count(2,timeout=30000);page.locator('#scenario-selection').select_option(item['id'])
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    with page.expect_popup() as popup:
                        page.locator('#scenario-reference-choice').select_option('attack-graph')
                    graph=popup.value;graph.on('pageerror',lambda error:errors.append(str(error)))
                    expect(graph.locator('#reference-title')).to_have_text('Attack graph · Reference lab',timeout=30000);expect(graph.locator('svg[role="img"]')).to_be_visible();expect(graph.locator('#reference-downloads a')).to_have_count(2);expect(graph.locator('#loading-modal')).not_to_be_visible();graph.screenshot(path='/tmp/caf-scenario-attack-graph.png');graph.close()
                    page.get_by_role('tab',name='Evaluation',exact=True).click();page.locator('#task-source').select_option('custom');page.locator('#task-0-prompt').fill('My experiment prompt stays here.')
                    page.locator('#task-0-hints').fill('Inspect the target service.\nCheck the response headers.')
                    assert page.evaluate('parseTaskRows(customTaskRows)[0].progressive_hints')==['Inspect the target service.','Check the response headers.']
                    page.locator('#task-0-mode').select_option('exact')
                    page.locator('#task-0-criteria').fill('{"type":"json_equals","expected":{"token":"SECRET"}}')
                    page.locator('#task-0-hints').fill('The answer is SECRET.')
                    expect(page.locator('#task-editor-error')).to_contain_text('contains a verifier answer')
                    page.locator('#task-0-hints').fill('Inspect the target service.\nCheck the response headers.')
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
                        expect(page.locator('#task-0-prompt')).to_have_value('My experiment prompt stays here.')
                        expect(page.locator('#task-0-hints')).to_have_value('Inspect the target service.\nCheck the response headers.')
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
                    expect(page.locator('#experiment-dialog')).to_be_visible();assert not errors,errors
                    page.locator('#task-0-mode').select_option('judge')
                    expect(page.locator('#task-0-criteria')).to_be_disabled()
                    page.locator('.rubric-criterion textarea').nth(0).fill('Locate and read the service configuration.')
                    page.locator('.rubric-criterion textarea').nth(1).fill('A successful tool response containing the observed configuration.')
                    tasks=page.evaluate('parseTaskRows(customTaskRows)')
                    assert tasks[0]['verification_mode']=='judge' and 'verifier' not in tasks[0]
                    assert tasks[0]['rubric']['criteria'][0]['requirement']=='Locate and read the service configuration.'
                    page.get_by_role('tab',name='Judge LLM',exact=True).click();page.locator('#judge-enabled').check()
                    page.get_by_role('tab',name='Tasks & trial settings',exact=True).click()
                    page.locator('#baseline-helper-conditions').click()
                    controls=page.evaluate('experimentControls()')
                    assert len(controls['conditions'])==2 and controls['reset_each_trial']
                    page.locator('.rubric-editor').first.scroll_into_view_if_needed();page.screenshot(path='/tmp/caf-rubric-editor.png')
                    page.set_viewport_size(dict(width=390,height=844));assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path='/tmp/caf-scenario-references-mobile.png')
                    assert all(op in ('list','references') for _,op in calls),calls
                    browser.close()
            finally:dashboard.close()
    print('PASS: reference popups, graph, formatted sandboxed guides, author draft preserved, mobile layout')


if __name__=='__main__':main()
