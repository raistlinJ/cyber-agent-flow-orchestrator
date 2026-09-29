"""Browser check for both ScenarioForge-backed sample presets."""
from pathlib import Path
import json
import sys
import tempfile
import pytest
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from test_workflow import lab
from scenario_fixture import install_transport
from test_user_access import vm, Probe
from test_pve_auth import pve_server, make_auth
from https_fixture import secure_server, PASSWORD


def main():
    with tempfile.TemporaryDirectory() as temp, pytest.MonkeyPatch.context() as patch:
        root=Path(temp)
        config,_,agent,manifest=lab.__wrapped__(root,patch)
        install_transport(patch,agent,manifest)
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve']=[vm(9402),vm(9403),vm(9404)]
            dashboard=UserDashboard(config,root/'runs',2,lambda b,a:Probe(b,a,[]))
            try:
                with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server,sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(ignore_https_errors=True,viewport={'width':1440,'height':1080})
                    errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password',exact=True).fill(PASSWORD)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click()
                    for role,vmid in [('scenarioforge','9402'),('participant','9403'),('core','9404')]:
                        page.locator('#role-'+role).select_option(vmid)
                    page.get_by_role('button',name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click()
                    for sample,trials in [('smoke',1),('tools-vs-helper',6)]:
                        page.locator('#new-experiment').click()
                        page.get_by_label('Experiment type',exact=True).select_option(sample)
                        page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                        expect(page.locator('#sample-sf-companion')).to_be_disabled()
                        expect(page.locator('#sample-sf-companion')).to_have_value('demo-'+sample+'.xml · fixed sample XML')
                        page.get_by_role('tab',name='Evaluation',exact=True).click()
                        expect(page.locator('#eval-repetitions')).to_be_disabled()
                        page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                        expect(page.locator('#caf-settings-summary')).to_contain_text('fixture')
                        page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                        page.locator('#sample-scenario-info').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/caf-fixed-'+sample+'.png')
                        page.get_by_role('button',name='Create experiment',exact=True).click()
                        expect(page.locator('#experiment-dialog')).to_be_hidden(timeout=30000)
                        row=page.locator('#runs tr').filter(has_text='ready').first
                        row.get_by_role('button',name='Deploy and run',exact=True).click()
                        expect(page.locator('#runs tr').filter(has_text='completed')).to_have_count(1 if sample=='smoke' else 2,timeout=30000)
                        records=[(path,json.loads(path.read_text())) for path in (root/'runs').rglob('workflow.json')]
                        path,record=next((p,r) for p,r in records if r.get('sample_id')==sample)
                        assert record['status']=='completed'
                        from cyber_agent_flow_orchestrator import service
                        result=service.results(path.parent)
                        assert result['evaluation']['planned_trials']==trials
                        assert result['run_configuration']['scenarioforge']['xml_available']
                    page.locator('#new-experiment').click()
                    overview=page.get_by_role('tab',name='Experiment',exact=True)
                    expect(overview).to_have_attribute('aria-selected','true')
                    overview.focus();page.keyboard.press('End')
                    expect(page.get_by_role('tab',name='Evaluation',exact=True)).to_be_focused()
                    page.keyboard.press('Home');expect(overview).to_be_focused()
                    page.get_by_label('Experiment type',exact=True).select_option('scenarioforge-xml')
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.locator('#eval-repetitions').fill('0')
                    overview.click()
                    expect(page.get_by_role('button',name='Create experiment',exact=True)).to_be_disabled()
                    expect(page.get_by_role('tab',name='Experiment',exact=True)).to_have_attribute('aria-selected','true')
                    page.get_by_role('tab',name='Evaluation',exact=True).click()
                    page.screenshot(path='/tmp/caf-new-tabs-desktop.png')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert page.locator('#experiment-dialog').evaluate('(n)=>n.getBoundingClientRect().width<=innerWidth && n.getBoundingClientRect().height<=innerHeight')
                    assert page.locator('#create-experiment').evaluate('(n)=>n.getBoundingClientRect().bottom<=innerHeight')
                    page.screenshot(path='/tmp/caf-new-tabs-mobile.png')
                    assert not errors,errors
                    browser.close()
            finally:
                dashboard.close()
    print('PASS: both samples use fixed XML, ScenarioForge workflow, and captured Results')


if __name__=='__main__':
    main()
