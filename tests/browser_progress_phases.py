"""Phase pages, navigation stability, active following and mobile progress."""
from pathlib import Path
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_refresh import Dashboard
from https_fixture import secure_server,PASSWORD
from test_pve_auth import pve_server,make_auth


def main():
    with tempfile.TemporaryDirectory() as temp,sync_playwright() as pw:
        root=Path(temp)
        steps=[dict(id='preflight',label='1. Check inputs',description='Validate model access.',status='completed',active=False,elapsed_seconds=3,input_checks=[dict(id='judge',label='Judge endpoint and host credentials',status='pass',message='Authenticated')]),
               dict(id='deploy',label='2. Deploy scenario',description='Prepare the network on CORE.',status='running',active=True,elapsed_seconds=7,vmid=9402,operation='Starting lab services',log='logs/deploy.log',log_tail='deployment line\n'*90,events=[]),
               dict(id='evaluate',label='3. Run and score trials',description='Execute participant tasks and score evidence.',status='pending',active=False,vmid=9403,events=[])]
        current=dict(judge_preflight_transcript=dict(live=False,events=[dict(type='response',text='Synthetic startup response: passed=false')]),output='/saved/demo',workflow_id='demo',recorded_status='preparing',coordinator_active=True,message='Starting lab services',
            workflow_progress=dict(steps=steps,current=steps[1],current_step='deploy',completed_steps=1,total_steps=3,observed_at='2026-10-16T20:00:00+00:00',roles=[dict(role='core',label='CORE',vmid=9404,responsibility='Hosts the scenario network.')]),
            sample_progress=dict(name='Demo',active=True,phase='preparing',percent=0,planned_trials=1,finished_trials=0,verified_successes=0,errors=0,max_turns=12,wall_seconds=1200,trials=[],events=[]))
        with pve_server(root) as pve,secure_server(Dashboard(),root/'web',auth=make_auth(pve)) as server:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1080})
            reads=[]
            def status(route):reads.append(route.request.url);route.fulfill(json=current)
            context.route('**/api/runs/demo/status',status)
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(server['origin']);page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD);page.get_by_role('button',name='Sign in',exact=True).click()
            expect(page.locator('#refresh')).to_be_enabled()
            page.evaluate("localStorage.setItem('caf-refresh-minutes','0');localStorage.setItem('caf-console-open','false')")
            page.goto(server['origin']+'/run?view=progress&run=demo&display=phases')
            expect(page.locator('#phase-title')).to_have_text('Deploy scenario')
            expect(page.locator('#phase-navigation button')).to_have_count(4)
            expect(page.locator('.workflow-step')).to_have_count(1)
            expect(page.locator('#phase-content')).to_contain_text('Starting lab services')
            expect(page.locator('#phase-follow')).to_be_checked()
            expect(page.locator('#phase-navigation [data-phase-id=evaluate]')).to_be_disabled()
            expect(page.locator('#phase-select option[value=evaluate]')).to_have_js_property('disabled',True)
            expect(page.locator('#phase-next')).to_be_disabled()
            assert page.evaluate("setProgressPhase('evaluate')") is False
            page.evaluate("document.getElementById('phase-select').value='evaluate';document.getElementById('phase-select').dispatchEvent(new Event('change'))")
            expect(page.locator('#phase-select')).to_have_value('deploy')
            expect(page.locator('#phase-title')).to_have_text('Deploy scenario')
            page.locator('#phase-navigation [data-phase-id=deploy]').press('End')
            expect(page.locator('#phase-title')).to_have_text('Deploy scenario')
            page.locator('#phase-follow').check()
            blocked=context.new_page()
            blocked.goto(server['origin']+'/run?view=progress&run=demo&display=phases#phase=evaluate')
            expect(blocked.locator('#phase-title')).to_have_text('Deploy scenario')
            assert blocked.url.endswith('#phase=deploy')
            expect(blocked.locator('#phase-next')).to_be_disabled()
            blocked.close()
            page.get_by_text('Stage details and output',exact=True).click()
            output=page.locator('.stage-output');output.evaluate('(element)=>element.scrollTop=70')
            page.wait_for_timeout(100)
            page.clock.install();page.evaluate("dispatchEvent(new StorageEvent('storage',{key:'caf-refresh-minutes',newValue:'0'}))")
            steps[1]['events'].append(dict(at='2026-10-16T20:00:01+00:00',kind='checkpoint',message='Service ready'))
            page.clock.fast_forward(5000)
            expect(page.locator('#phase-content')).to_contain_text('Service ready')
            expect(page.locator('.stage-details')).to_have_attribute('open','')
            expect(output).to_have_js_property('scrollTop',70)
            page.locator('#phase-navigation [data-phase-id=preflight]').click()
            expect(page.locator('#phase-title')).to_have_text('Check inputs')
            expect(page.locator('#phase-follow')).not_to_be_checked()
            assert page.url.endswith('#phase=preflight')
            page.locator('#phase-content').evaluate('(element)=>element.dataset.preserved="yes"')
            page.locator('#phase-content .workflow-step').evaluate('(element)=>element.dataset.preserved="yes"')
            steps[1].update(status='completed',active=False);steps[2].update(status='running',active=True)
            current.update(recorded_status='evaluating',message='Participant working')
            current['sample_progress']['current_trial']=dict(trial_id='trial-000001',attempt=1,task_id='task',condition_id='baseline',repetition=1) 
            current['workflow_progress'].update(current=steps[2],current_step='evaluate',completed_steps=2)
            page.clock.fast_forward(5000)
            expect(page.locator('#phase-title')).to_have_text('Check inputs')
            expect(page.locator('#phase-content .workflow-step')).to_have_attribute('data-preserved','yes')
            expect(page.locator('#phase-navigation [data-phase-id=evaluate]')).to_contain_text('running')
            expect(page.locator('#phase-navigation [data-phase-id=evaluate]')).to_be_enabled()
            page.get_by_text('Test LLM Judge',exact=True).click()
            expect(page.locator('[aria-label="Test LLM Judge output"]')).to_contain_text('Synthetic startup response')
            expect(page.locator('.judge-test-explanation')).to_contain_text('does not mean the experiment failed')
            expect(page.locator('#phase-select option[value=evaluate]')).to_have_js_property('disabled',False)
            expect(page.locator('#loading-modal')).not_to_be_visible()
            page.reload();expect(page.locator('#phase-title')).to_have_text('Check inputs')
            page.locator('#phase-follow').check()
            expect(page.locator('#phase-title')).to_have_text('Run and score trials')
            assert page.url.endswith('#phase=active')
            page.reload();expect(page.locator('#phase-title')).to_have_text('Run and score trials');expect(page.locator('#phase-follow')).to_be_checked()
            page.locator('#phase-navigation [data-phase-id=deploy]').click()
            page.get_by_text('Stage details and output',exact=True).click()
            page.locator('#phase-navigation [data-phase-id=overview]').click()
            expect(page.locator('#phase-content')).to_contain_text('VM 9404')
            expect(page.locator('#phase-completion-label')).to_have_text('2 / 3 phases completed')
            page.locator('#phase-next').click();expect(page.locator('#phase-title')).to_have_text('Check inputs')
            page.locator('#phase-next').click();expect(page.locator('#phase-title')).to_have_text('Deploy scenario')
            expect(page.locator('.stage-details')).to_have_attribute('open','')
            page.locator('#phase-previous').click();expect(page.locator('#phase-title')).to_have_text('Check inputs')
            page.go_back();expect(page.locator('#phase-title')).to_have_text('Deploy scenario')
            page.screenshot(path='/tmp/caf-progress-phase-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#phase-select').select_option('evaluate')
            expect(page.locator('#phase-title')).to_have_text('Run and score trials')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path='/tmp/caf-progress-phase-mobile.png',full_page=True)
            steps[2].update(status='failed',active=False,error='Trial output could not be collected')
            current.update(recorded_status='failed',coordinator_active=False);current['sample_progress']['active']=False
            page.locator('#refresh-run').click()
            expect(page.locator('#phase-content')).to_contain_text('Trial output could not be collected')
            expect(page.locator('#run-status')).to_contain_text('failed')
            expect(page.get_by_role('button',name='Open results')).to_have_count(0)
            # A run that stopped early cannot browse later phases, even with a direct URL.
            steps[1].update(status='pending',active=False);steps[2].update(status='pending',active=False,error=None)
            current['workflow_progress'].update(current=steps[0],current_step='preflight',completed_steps=1)
            page.locator('#refresh-run').click()
            expect(page.locator('#phase-title')).to_have_text('Check inputs')
            expect(page.locator('#phase-next')).to_be_disabled()
            expect(page.locator('#phase-select option[value=deploy]')).to_have_js_property('disabled',True)
            page.evaluate("location.hash='phase=evaluate'")
            expect(page.locator('#phase-title')).to_have_text('Check inputs')
            expect(page).to_have_url(server['origin']+'/run?view=progress&run=demo&display=phases#phase=preflight')
            # If no phase started, only Overview is available and the error is shown once.
            steps[0].update(status='pending',active=False,elapsed_seconds=None)
            error='Workflow, host artifacts, or source changed; use a new output directory'
            current.update(error=error,message=error)
            current['workflow_progress'].update(current=None,current_step=None,completed_steps=0)
            page.locator('#refresh-run').click()
            expect(page.locator('#phase-title')).to_have_text('Overview')
            expect(page.locator('#phase-next')).to_be_disabled()
            expect(page.locator('#phase-navigation button:enabled')).to_have_count(1)
            expect(page.locator('#phase-content').get_by_text(error,exact=True)).to_have_count(1)
            expect(page.locator('#phase-content')).to_contain_text('Run it again to create a new output directory')
            page.screenshot(path='/tmp/caf-progress-locked-phases-mobile.png',full_page=True)
            assert len(reads)>=5 and not errors,errors
            browser.close()
    print('PASS: dynamic phase pages, bookmarked navigation, live follow, stable logs, independent polling, failures and mobile layout')


if __name__=='__main__':main()
