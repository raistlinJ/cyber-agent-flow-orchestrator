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
                    context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1080})
                    page=context.new_page()
                    errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    # This PVE operator has orchestration access, not maintenance
                    # access. Pulling an unchanged model must not block creation.
                    model_reads=[]
                    def read_model(route):
                        data=route.request.post_data_json
                        assert data==dict(role='participant',action='read')
                        model_reads.append(data)
                        route.fulfill(json=dict(token='c'*32,vmid=9403,exists=True,
                            settings=dict(provider='openai',url='https://models.example/v1',model='saved-model',ssl_verify=True),
                            api_key_set=False,api_key_env='OPENAI_API_KEY'))
                    page.route('**/api/model-config',read_model)
                    page.route('**/api/model-network-scope',lambda route:route.fulfill(json=dict(
                        provider_host='models.example',excluded_targets=['192.0.2.8'],warnings=[])))
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password',exact=True).fill(PASSWORD)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click()
                    for role,vmid in [('scenarioforge','9402'),('participant','9403'),('core','9404')]:
                        page.locator('#role-'+role).select_option(vmid)
                    page.get_by_role('button',name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click()
                    for sample,trials in [('smoke',2),('tools-vs-helper',4)]:
                        page.locator('#new-experiment').click()
                        page.get_by_label('Experiment type',exact=True).select_option(sample)
                        page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                        expect(page.locator('#sample-sf-companion')).to_be_disabled()
                        expect(page.locator('#sample-sf-companion')).to_have_value('demo-'+sample+'.xml · fixed sample XML')
                        page.get_by_role('tab',name='Evaluation',exact=True).click()
                        settings=dict(repetitions=2,max_turns=9,wall_seconds=333,tool_timeout=41,context_window=4096,max_tries_before_solution=4)
                        expect(page.locator('#eval-repetitions')).to_have_value('1' if sample=='smoke' else '3')
                        expect(page.locator('#eval-wall_seconds')).to_have_value('120')
                        expect(page.locator('#eval-max_tries_before_solution')).to_have_value('6')
                        for key,value in settings.items():
                            expect(page.locator('#eval-'+key)).to_be_enabled()
                            page.locator('#eval-'+key).fill(str(value))
                        page.get_by_role('tab',name='Experiment',exact=True).click()
                        page.get_by_label('Experiment type',exact=True).select_option('tools-vs-helper' if sample=='smoke' else 'smoke')
                        page.get_by_role('tab',name='Evaluation',exact=True).click()
                        expect(page.locator('#eval-wall_seconds')).to_have_value('120')
                        page.get_by_role('tab',name='Experiment',exact=True).click()
                        page.get_by_label('Experiment type',exact=True).select_option(sample)
                        page.get_by_role('tab',name='Evaluation',exact=True).click()
                        expect(page.locator('#eval-wall_seconds')).to_have_value('333')
                        page.locator('#eval-max_tries_before_solution').fill('0')
                        expect(page.locator('#create-experiment')).to_be_disabled()
                        page.locator('#eval-max_tries_before_solution').fill('4')
                        page.locator('#eval-repetitions').fill('0')
                        expect(page.locator('#create-experiment')).to_be_disabled()
                        page.locator('#eval-repetitions').fill('2')
                        if sample=='smoke':
                            page.locator('#eval-max_tries_before_solution').scroll_into_view_if_needed()
                            page.screenshot(path='/tmp/caf-solution-tries.png')
                        page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                        expect(page.locator('#caf-settings-summary')).to_have_count(0)
                        if sample=='smoke':
                            expect(page.locator('#create-experiment')).to_be_enabled(timeout=30000)
                            page.locator('#model-participant-read').click()
                            expect(page.locator('#model-participant-model')).to_have_value('saved-model',timeout=30000)
                            expect(page.locator('#model-participant-model')).to_be_disabled()
                            expect(page.locator('#model-participant-apply')).to_be_disabled()
                            expect(page.locator('#model-participant-message')).to_contain_text('You can create experiments using saved settings.')
                            expect(page.locator('#create-experiment')).to_be_enabled()
                            expect(page.locator('#create-experiment-hint')).to_be_hidden()
                            assert len(model_reads)==1
                        page.get_by_role('tab',name='ScenarioForge',exact=True).click()
                        page.locator('#sample-scenario-info').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/caf-fixed-'+sample+'.png')
                        page.get_by_role('button',name='Create experiment',exact=True).click()
                        expect(page.locator('#experiment-dialog')).to_be_hidden(timeout=30000)
                        row=page.locator('#runs tr').filter(has_text='ready').first
                        row.get_by_role('button',name='Deploy and run',exact=True).click()
                        try:
                            expect(page.locator('#runs tr').filter(has_text='completed')).to_have_count(1 if sample=='smoke' else 2,timeout=30000)
                        except AssertionError:
                            print('Browser errors:',errors)
                            print('Rows:',page.locator('#runs').inner_text())
                            print('Console:',page.locator('#console-output').inner_text()[-2000:])
                            for record in (root/'runs').rglob('workflow.json'):
                                value=json.loads(record.read_text())
                                print('Recorded status:',value.get('status'),value.get('error'))
                            raise
                        records=[(path,json.loads(path.read_text())) for path in (root/'runs').rglob('workflow.json')]
                        path,record=next((p,r) for p,r in records if r.get('sample_id')==sample)
                        assert record['status']=='completed'
                        assert record['runtime']['repetitions']==2
                        assert all(record['runtime']['execution'][key]==value for key,value in settings.items() if key!='repetitions')
                        from cyber_agent_flow_orchestrator import service
                        result=service.results(path.parent)
                        assert result['evaluation']['planned_trials']==trials
                        assert result['run_configuration']['scenarioforge']['xml_available']
                    progress_page=page.context.new_page()
                    progress_page.on('pageerror',lambda e:errors.append(str(e)))
                    progress_page.goto(server['origin']+'/run?view=progress&run='+path.parent.name)
                    expect(progress_page.get_by_text('Workflow across VMs',exact=True)).to_be_visible()
                    expect(progress_page.locator('.workflow-step')).to_have_count(10)
                    expect(progress_page.locator('.workflow-step.completed')).to_have_count(10)
                    expect(progress_page.locator('.workflow-vms')).to_contain_text('VM 9404')
                    expect(progress_page.locator('.workflow-readiness')).to_contain_text('containers: pass')
                    progress_page.screenshot(path='/tmp/caf-detailed-progress.png',full_page=True)
                    progress_page.set_viewport_size({'width':390,'height':844})
                    assert progress_page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    progress_page.close()
                    page.locator('#new-experiment').click()
                    overview=page.get_by_role('tab',name='Experiment',exact=True)
                    expect(overview).to_have_attribute('aria-selected','true')
                    # Opening New also resolves model routing asynchronously.
                    # Wait for that operation before checking keyboard focus.
                    expect(overview).to_be_enabled(timeout=30000)
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
