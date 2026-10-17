"""Required setup gates desktop/mobile navigation; no VM or inference calls."""
from pathlib import Path
import sys
import tempfile
import pytest
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from test_workflow import lab
from test_user_access import vm, Probe
from test_pve_auth import pve_server, make_auth
from https_fixture import secure_server, PASSWORD


def main():
    with tempfile.TemporaryDirectory() as temp, pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        config, _, _, _ = lab.__wrapped__(root, patch)
        with pve_server(root) as pve:
            pve[0]['groups'] += ',caf-maintainers'
            pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403), vm(9404)]
            dashboard = UserDashboard(config, root/'runs', 2, lambda b,a: Probe(b,a,[]))
            try:
                with secure_server(dashboard, root/'web', auth=make_auth(pve)) as server, sync_playwright() as pw:
                    browser = pw.chromium.launch(channel='chrome', headless=True)
                    page = browser.new_page(ignore_https_errors=True, viewport={'width':1440,'height':1080})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    settings = dict(provider='openai',url='https://models.example/v1',model='fixture',ssl_verify=True)
                    def model(route):
                        data = route.request.post_data_json
                        if data['action'] == 'tools':
                            route.fulfill(json=dict(vmid=9403,tools=[dict(name='curl',description='Fetch a URL',risk='standard')],guidance_assets=[]))
                        else:
                            if data['action'] == 'save': settings.update(data['settings'])
                            route.fulfill(json=dict(vmid=9403,token='a'*32,exists=True,settings=settings,api_key_set=True))
                    page.route('**/api/model-config', model)
                    page.route('**/api/model-network-scope', lambda route: route.fulfill(json=dict(provider_host='models.example',excluded_targets=['192.0.2.8'],warnings=[])))
                    scenario=dict(id='b'*64,scenario='Resolved scenario',path='/opt/scenarioforge/uploads/example.xml',resolved_chain=True,chain_length=1,target_subnets=['10.77.0.0/24'],modified_epoch=1)
                    page.route('**/api/scenarios/list', lambda route: route.fulfill(json=dict(vmid=9402,items=[scenario],roots=['/opt/scenarioforge/uploads'],truncated=False)))
                    task=dict(id='read-token',family='http',prompt='Read the service token.',verifier=dict(type='contains_all',expected=['test-token']),required_checks=['containers'])
                    fail_task = [True]
                    def load_task(route):
                        if fail_task[0]: route.fulfill(status=400,json=dict(error='Fixture task load failed'))
                        else: route.fulfill(json=dict(tasks=[task],suggested_tasks=[],context={}))
                    page.route('**/api/scenarios/tasks',load_task)
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password',exact=True).fill(PASSWORD)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click()
                    for role,vmid in [('participant','9403'),('scenarioforge','9402'),('core','9404')]:
                        page.locator('#role-'+role).select_option(vmid)
                    page.get_by_role('button',name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click()
                    page.locator('#new-experiment').click()
                    nav=lambda step:page.locator('[data-wizard-step='+step+']')
                    expect(nav('review')).to_be_enabled(timeout=30000)
                    # Auto-filled valid sample defaults need no extra confirmation.
                    nav('caf').click()
                    original=page.locator('#model-participant-model').input_value()
                    page.locator('#model-participant-model').fill('')
                    expect(nav('caf')).to_be_enabled()
                    for step in ['tasks','tools','helpers','limits','hints','controls','judge','review']:
                        expect(nav(step)).to_be_disabled()
                        assert 'Agent & network' in nav(step).get_attribute('title')
                    expect(page.locator('#wizard-next')).to_be_disabled()
                    expect(page.locator('#wizard-next-label')).to_contain_text('To continue:')
                    assert page.evaluate("showWizardStep('review')") is False
                    expect(page.locator('#wizard-step-title')).to_have_text('Agent & network')
                    page.set_viewport_size({'width':390,'height':844})
                    expect(page.locator('#wizard-step-select option[value=tasks]')).to_have_js_property('disabled',True)
                    page.evaluate("const s=document.getElementById('wizard-step-select');s.value='review';s.dispatchEvent(new Event('change'))")
                    expect(page.locator('#wizard-step-select')).to_have_value('caf')
                    page.screenshot(path='/tmp/caf-wizard-locked-mobile.png',full_page=True)
                    page.set_viewport_size({'width':1440,'height':1080})
                    page.locator('#model-participant-model').fill(original+'-changed')
                    expect(nav('tasks')).to_be_disabled()
                    page.locator('#model-participant-apply').click()
                    expect(nav('tasks')).to_be_enabled(timeout=30000)
                    expect(page.locator('#wizard-next')).to_be_enabled()
                    nav('limits').click()
                    repetitions=page.locator('#eval-repetitions').input_value()
                    page.locator('#eval-repetitions').fill('0')
                    expect(nav('hints')).to_be_disabled()
                    expect(nav('helpers')).to_be_enabled()
                    expect(nav('limits')).to_be_enabled()
                    page.locator('#eval-repetitions').fill(repetitions)
                    expect(nav('hints')).to_be_enabled()
                    nav('hints').click()
                    page.locator('#eval-hint_stalled_turns').fill(page.locator('#eval-max_turns').input_value())
                    expect(nav('controls')).to_be_disabled()
                    page.locator('#provide-progressive-hints').uncheck()
                    expect(nav('controls')).to_be_enabled()
                    nav('controls').click()
                    page.get_by_text('Optional cost accounting (USD)',exact=True).click()
                    page.locator('#price-participant-input').fill('1')
                    expect(nav('judge')).to_be_disabled()
                    # Pricing errors belong to Run controls, never Run configurations.
                    expect(nav('controls')).to_be_enabled()
                    nav('tools').click()
                    expect(nav('controls')).to_be_enabled()
                    nav('controls').click()
                    page.locator('#price-participant-output').fill('2')
                    expect(nav('judge')).to_be_enabled()
                    nav('judge').click()
                    page.locator('#judge-inherit').uncheck()
                    endpoint=page.locator('#judge-url').input_value()
                    page.locator('#judge-url').fill('')
                    expect(nav('review')).to_be_disabled()
                    page.locator('#judge-url').fill(endpoint)
                    expect(nav('review')).to_be_enabled()
                    nav('review').click()
                    expect(page.locator('#wizard-review')).to_contain_text('Ready to create')
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    # Changing scenario type locks dependent steps until resolution/task load.
                    nav('overview').click()
                    page.locator('#experiment-sample').select_option('scenarioforge-xml')
                    expect(nav('scenario')).to_be_enabled()
                    expect(nav('caf')).to_be_disabled()
                    nav('scenario').click()
                    page.screenshot(path='/tmp/caf-wizard-locked-desktop.png',full_page=True)
                    page.locator('#load-scenarios').click()
                    expect(nav('caf')).to_be_enabled(timeout=30000)
                    expect(nav('tasks')).to_be_enabled()
                    expect(nav('tools')).to_be_disabled()
                    assert 'Task' in nav('tools').get_attribute('title')
                    nav('tasks').click()
                    expect(page.locator('#task-editor-error')).to_contain_text('Fixture task load failed')
                    fail_task[0]=False
                    page.locator('#retry-scenario-tasks').click()
                    expect(nav('tools')).to_be_enabled(timeout=30000)
                    expect(page.locator('#wizard-next')).to_be_enabled()
                    nav('caf').click()
                    page.locator('#scenario-allowed').fill('')
                    expect(nav('tasks')).to_be_disabled()
                    page.locator('#scenario-allowed').fill('10.77.0.0/24')
                    expect(nav('review')).to_be_enabled()
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print('PASS: valid defaults, required fields, Apply settings, task loading/retry, mobile and scripted guards, relocking and optional pricing scope')


if __name__=='__main__': main()
