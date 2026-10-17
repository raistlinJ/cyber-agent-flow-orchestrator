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


def main(unlimited=False):
    with tempfile.TemporaryDirectory() as temp, pytest.MonkeyPatch.context() as patch:
        root=Path(temp)
        config,_,agent,manifest=lab.__wrapped__(root,patch)
        install_transport(patch,agent,manifest)
        from cyber_agent_flow_eval import judge
        def completion(config,messages,timeout, **kwargs):
            if kwargs.get('on_delta'):
                kwargs['on_delta']('reasoning','Synthetic fixture judge output.')
            if messages[0]['content'].startswith('Synthetic preflight'):
                return json.dumps({'action':'verdict','passed':False,'score':0,'reason':'Synthetic','evidence':[]}),{}
            if len(messages)==2:return json.dumps({'action':'read_evidence','file':'worker-result.json'}),{'prompt_tokens':10,'output_tokens':5}
            return json.dumps({'action':'verdict','passed':True,'score':1,'reason':'Reviewed the saved result.','evidence':['worker-result.json']}),{'prompt_tokens':10,'output_tokens':5}
        patch.setattr(judge,'_completion',completion)
        from cyber_agent_flow_orchestrator import trial_reset
        resets=[]
        def reset_callback(*args):
            def reset(directory,trial):
                resets.append(trial['trial_id']);return {'passed':True,'method':'simulated frozen scenario reset'}
            return reset
        patch.setattr(trial_reset,'callback',reset_callback)
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
                        if data==dict(role='participant',action='tools'):
                            route.fulfill(json=dict(vmid=9403,tools=[dict(name='curl',description='Fetch HTTP',risk='standard')],guidance_assets=[]));return
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
                        page.locator('[data-wizard-step=scenario]').click()
                        expect(page.locator('#sample-sf-companion')).to_be_disabled()
                        expect(page.locator('#sample-sf-companion')).to_have_value('demo-'+sample+'.xml · fixed sample XML')
                        page.locator('[data-wizard-step=tasks]').click()
                        sample_task=page.locator('#sample-task-info')
                        sample_task.get_by_role('tab',name='Readiness',exact=True).click()
                        expect(sample_task.locator('#sample-checks')).to_be_visible()
                        expect(sample_task.locator('#sample-success')).to_be_hidden()
                        sample_task.get_by_role('tab',name='Progressive hints',exact=True).click()
                        expect(sample_task.locator('#sample-task-panel-hints')).to_contain_text('Sample guidance')
                        sample_task.get_by_role('tab',name='Verification',exact=True).click()
                        expect(sample_task.locator('#sample-success')).to_be_visible()
                        expect(sample_task.locator('#sample-success')).to_be_disabled()
                        page.locator('[data-wizard-step=judge]').click()
                        expect(page.locator('#judge-enabled')).to_be_checked()
                        page.locator('#judge-enabled').check()
                        expect(page.locator('#judge-inherit')).to_be_checked()
                        expect(page.locator('#judge-monitor_progress')).to_be_checked()
                        page.locator('#judge-monitor_progress').uncheck()
                        expect(page.locator('#judge-progress_timeout_seconds')).to_be_disabled()
                        page.locator('#judge-monitor_progress').check()
                        page.locator('#judge-progress_timeout_seconds').fill('12')
                        page.locator('#judge-progress_max_checks').fill('8')
                        expect(page.locator('#judge-url')).to_be_disabled()
                        page.locator('#judge-max_turns').fill('4')
                        page.locator('#judge-timeout_seconds').fill('45')
                        if sample=='tools-vs-helper':
                            page.locator('#judge-inherit').uncheck()
                            page.locator('#judge-provider').select_option('openai')
                            page.locator('#judge-url').fill('http://judge.fixture/v1')
                            page.locator('#judge-name').fill('independent-judge')
                        page.locator('[data-wizard-step=limits]').click()
                        settings=dict(repetitions=2,max_turns=9,wall_seconds=333,tool_timeout=41,context_window=4096,hint_stalled_turns=3,max_tries_before_solution=4)
                        expect(page.locator('#eval-repetitions')).to_have_value('1' if sample=='smoke' else '3')
                        expect(page.locator('#eval-wall_seconds')).to_have_value('120')
                        expect(page.locator('#eval-max_tries_before_solution')).to_have_value('6')
                        expect(page.locator('#eval-hint_stalled_turns')).to_have_value('2')
                        expect(page.locator('#eval-hint_stalled_turns')).to_be_enabled()
                        page.locator('[data-wizard-step=hints]').click()
                        expect(page.locator('#provide-progressive-hints')).to_be_checked()
                        page.locator('#provide-progressive-hints').uncheck()
                        expect(page.locator('#eval-hint_stalled_turns')).to_be_disabled()
                        page.locator('#provide-progressive-hints').check()
                        expect(page.locator('#eval-hint_stalled_turns')).to_be_enabled()
                        page.locator('[data-wizard-step=limits]').click()
                        page.locator('#eval-max_turns').fill('2')
                        page.locator('[data-wizard-step=hints]').click()
                        page.locator('#eval-hint_stalled_turns').fill('2')
                        assert page.locator('#eval-hint_stalled_turns').evaluate('(input)=>!input.validity.valid')
                        for key,value in settings.items():
                            page.locator('[data-wizard-step='+('hints' if key in ('hint_stalled_turns','max_tries_before_solution') else 'limits')+']').click()
                            expect(page.locator('#eval-'+key)).to_be_enabled()
                            page.locator('#eval-'+key).fill(str(value))
                        assert page.locator('#eval-hint_stalled_turns').evaluate('(input)=>input.validity.valid')
                        page.locator('[data-wizard-step=overview]').click()
                        page.get_by_label('Experiment type',exact=True).select_option('tools-vs-helper' if sample=='smoke' else 'smoke')
                        page.locator('[data-wizard-step=tasks]').click()
                        expect(page.locator('#eval-wall_seconds')).to_have_value('120')
                        page.locator('[data-wizard-step=overview]').click()
                        page.get_by_label('Experiment type',exact=True).select_option(sample)
                        page.locator('[data-wizard-step=tasks]').click()
                        expect(page.locator('#eval-wall_seconds')).to_have_value('333')
                        page.locator('[data-wizard-step=hints]').click()
                        page.locator('#eval-max_tries_before_solution').fill('0')
                        expect(page.locator('#create-experiment')).to_be_disabled()
                        page.locator('#eval-max_tries_before_solution').fill('4')
                        page.locator('[data-wizard-step=limits]').click()
                        page.locator('#eval-repetitions').fill('0')
                        expect(page.locator('#create-experiment')).to_be_disabled()
                        page.locator('#eval-repetitions').fill('2')
                        if sample=='smoke':
                            page.locator('[data-wizard-step=judge]').click()
                            page.locator('#judge-max_turns').scroll_into_view_if_needed()
                            page.screenshot(path='/tmp/caf-judge-configuration.png')
                            page.locator('[data-wizard-step=hints]').click()
                            page.locator('#eval-max_tries_before_solution').scroll_into_view_if_needed()
                            page.screenshot(path='/tmp/caf-solution-tries.png')
                        page.locator('[data-wizard-step=caf]').click()
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
                        page.locator('[data-wizard-step=scenario]').click()
                        page.locator('#sample-scenario-info').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/caf-fixed-'+sample+'.png')
                        if sample=='smoke':
                            page.locator('[data-wizard-step=tasks]').click()
                            page.locator('[data-wizard-step=judge]').click()
                            page.locator('#judge-max_turns').fill('0')
                            page.locator('[data-wizard-step=limits]').click()
                            assert page.evaluate("validateExperimentFields(document.getElementById('experiment-form'))") is False
                            expect(page.locator('#evaluation-page-judge')).to_be_visible()
                            page.locator('#judge-max_turns').fill('4')
                            page.locator('[data-wizard-step=controls]').click()
                            expect(page.locator('#evaluation-page-controls')).to_be_visible()
                        if unlimited:
                            page.locator('[data-wizard-step=limits]').click()
                            for key in ('wall_seconds','tool_timeout'):
                                field=page.locator('#eval-'+key).locator('..')
                                before_height=field.bounding_box()['height']
                                toggle=page.locator('#eval-'+key+'-no-timeout');toggle.check()
                                expect(page.locator('#eval-'+key)).to_have_value('0')
                                expect(page.locator('#eval-'+key)).to_be_disabled()
                                warning=page.locator('#eval-'+key+'-timeout-warning')
                                expect(warning).to_be_visible()
                                expect(warning.locator('button')).to_have_text('!')
                                tooltip=warning.get_by_role('tooltip',include_hidden=True)
                                expect(tooltip).to_be_hidden()
                                assert abs(field.bounding_box()['height']-before_height)<1
                                warning.locator('button').hover()
                                expect(tooltip).to_be_visible()
                                expect(tooltip).to_contain_text('infinite wait')
                                page.mouse.move(0,0)
                                expect(tooltip).to_be_hidden()
                                warning.locator('button').focus()
                                expect(tooltip).to_be_visible()
                                assert abs(field.bounding_box()['height']-before_height)<1
                                page.keyboard.press('Tab')
                                expect(tooltip).to_be_hidden()
                                expect(toggle).to_be_checked()
                                toggle.uncheck();expect(page.locator('#eval-'+key)).to_be_enabled()
                                expect(page.locator('#eval-'+key)).to_have_value(str(settings[key]))
                                toggle.check()
                            settings.update(wall_seconds=0,tool_timeout=0)
                            page.screenshot(path='/tmp/caf-no-timeout-trial-limits.png',full_page=True)
                            page.locator('[data-wizard-step=judge]').click()
                            for key in ('timeout_seconds','progress_timeout_seconds'):
                                page.locator('#judge-'+key+'-no-timeout').check()
                                expect(page.locator('#judge-'+key)).to_be_disabled()
                                warning=page.locator('#judge-'+key+'-timeout-warning')
                                expect(warning.locator('button')).to_have_text('!')
                                warning.locator('button').focus()
                                expect(warning.get_by_role('tooltip')).to_contain_text('infinite wait')
                                page.keyboard.press('Tab')
                            page.locator('#judge-monitor_progress').uncheck()
                            expect(page.locator('#judge-progress_timeout_seconds-no-timeout')).to_be_disabled()
                            page.locator('#judge-monitor_progress').check()
                            expect(page.locator('#judge-progress_timeout_seconds')).to_be_disabled()
                            page.screenshot(path='/tmp/caf-no-timeout-judge.png',full_page=True)
                        page.locator('[data-wizard-step=review]').click()
                        page.get_by_role('button',name='Create experiment',exact=True).click()
                        expect(page.locator('#experiment-builder')).to_be_hidden(timeout=30000)
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
                        assert record['workflow']['reset_each_trial']
                        assert resets
                        assert record['runtime']['judge']['enabled']
                        assert record['runtime']['judge']['model']['name']==('independent-judge' if sample=='tools-vs-helper' else record['runtime']['model']['name'])
                        assert record['runtime']['judge']['monitor_progress'] is True
                        assert record['runtime']['judge']['progress_timeout_seconds']==(0 if unlimited else 12)
                        assert record['runtime']['judge']['progress_max_checks']==8
                        assert record['runtime']['judge']['max_turns']==4
                        assert record['runtime']['judge']['timeout_seconds']==(0 if unlimited else 45)
                        assert record['runtime']['repetitions']==2
                        assert all(record['runtime']['execution'][key]==value for key,value in settings.items() if key!='repetitions')
                        from cyber_agent_flow_orchestrator import service
                        result=service.results(path.parent)
                        assert result['evaluation']['planned_trials']==trials
                        assert result['run_configuration']['scenarioforge']['xml_available']
                    progress_page=page.context.new_page()
                    progress_page.on('pageerror',lambda e:errors.append(str(e)))
                    progress_page.goto(server['origin']+'/run?view=progress&run='+path.parent.name+'&display=phases')
                    progress_page.locator('#phase-navigation [data-phase-id=overview]').click()
                    expect(progress_page.get_by_text('Workflow across VMs',exact=True)).to_be_visible()
                    expect(progress_page.locator('#phase-navigation .progress-phase-link')).to_have_count(11)
                    expect(progress_page.locator('#phase-navigation [data-state=completed]')).to_have_count(11)
                    expect(progress_page.locator('.workflow-vms')).to_contain_text('VM 9404')
                    expect(progress_page.locator('.workflow-readiness')).to_contain_text('containers: pass')
                    progress_page.locator('#phase-navigation [data-phase-id=preflight]').click()
                    preflight=progress_page.locator('.workflow-step')
                    preflight.get_by_text('Stage details and output',exact=True).click()
                    expect(preflight).to_contain_text('Test LLM Judge · pass')
                    expect(preflight).to_contain_text('Participant model, guest credentials and connectivity · pass')
                    expect(preflight).to_contain_text('CORE upload paths and HITL interface · pass')
                    preflight.get_by_text('Test LLM Judge',exact=True).click()
                    expect(preflight.locator('.judge-test-explanation')).to_contain_text('does not mean the experiment failed')
                    expect(preflight.locator('[aria-label="Test LLM Judge output"]')).to_contain_text('Synthetic fixture judge output.')
                    progress_page.locator('#phase-navigation [data-phase-id=evaluate]').click()
                    judge_output=progress_page.locator('.workflow-step').locator('details').filter(has=progress_page.get_by_text('Judge output',exact=True))
                    judge_output.locator('summary').click()
                    expect(judge_output.locator('[aria-label="Judge output output"]')).to_contain_text('Synthetic fixture judge output.')
                    progress_page.screenshot(path='/tmp/caf-judge-streaming-progress.png',full_page=True)
                    progress_page.set_viewport_size({'width':390,'height':844})
                    assert progress_page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    progress_page.close()
                    page.locator('#new-experiment').click()
                    overview=page.locator('[data-wizard-step=overview]')
                    expect(overview).to_have_attribute('aria-current','step')
                    # Opening New also resolves model routing asynchronously.
                    # Wait for that operation before checking keyboard focus.
                    expect(overview).to_be_enabled(timeout=30000)
                    overview.focus();page.keyboard.press('Tab')
                    expect(page.locator('[data-wizard-step=scenario]')).to_be_focused()
                    page.get_by_label('Experiment type',exact=True).select_option('scenarioforge-xml')
                    expect(page.locator('[data-wizard-step=tasks]')).to_be_disabled()
                    page.locator('[data-wizard-step=scenario]').click()
                    expect(page.get_by_text('Define my own task for this experiment',exact=True)).to_have_count(0)
                    expect(page.locator('#task-load-state')).to_have_text('Waiting for scenario')
                    expect(page.locator('[data-wizard-step=limits]')).to_be_disabled()
                    overview.click()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    expect(page.locator('[data-wizard-step=overview]')).to_have_attribute('aria-current','step')
                    expect(page.locator('[data-wizard-step=tasks]')).to_be_disabled()
                    page.locator('[data-wizard-step=scenario]').click()
                    page.screenshot(path='/tmp/caf-new-tabs-desktop.png')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert page.locator('#experiment-builder').evaluate('(n)=>n.getBoundingClientRect().width<=innerWidth')
                    expect(page.locator('#wizard-step-select')).to_be_visible()
                    expect(page.locator('#wizard-step-select option[value=review]')).to_have_js_property('disabled',True)
                    assert page.evaluate("showWizardStep('review')") is False
                    expect(page.locator('#wizard-step-title')).to_have_text('Scenario')
                    expect(page.locator('#experiment-review')).to_be_hidden()
                    page.screenshot(path='/tmp/caf-new-tabs-mobile.png')
                    assert not errors,errors
                    browser.close()
            finally:
                dashboard.close()
    print('PASS: both samples use fixed XML, ScenarioForge workflow, and captured Results')


if __name__=='__main__':
    main('--unlimited' in sys.argv)
