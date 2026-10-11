"""Live transcript updates, trial selection and scroll preservation in step 9."""
from pathlib import Path
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_refresh import Dashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth


def main():
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as pw:
        root = Path(temp)
        trials = []
        for number in (1, 2):
            trials.append(dict(trial_id=f'trial-{number:06d}', condition_id='baseline', task_id='task', repetition=1,
                attempt=1, status='running' if number == 2 else 'completed', verified_success=None, score=None,
                transcript={'live':number==2, 'events':[{'type':'response','elapsed_seconds':n,
                    'text':f'trial {number} output {n} <script>window.injected=true</script>'} for n in range(40)]}))
        stage = dict(id='evaluate', label='9. Evaluate agent', status='running', vmid=9403, description='Agent trials')
        current = dict(output='/saved/demo', workflow_id='demo', recorded_status='evaluating', coordinator_active=True,
            workflow_progress=dict(current=stage, steps=[stage], roles=[], completed_steps=8,total_steps=9),
            sample_progress=dict(name='Demo', active=True, percent=50, planned_trials=2, finished_trials=1,
                verified_successes=0, errors=0, max_turns=12,wall_seconds=300, current_trial=trials[1],trials=trials,events=[]))
        with pve_server(root) as pve, secure_server(Dashboard(), root/'web', auth=make_auth(pve)) as server:
            browser = pw.chromium.launch(channel='chrome',headless=True)
            context = browser.new_context(ignore_https_errors=True, viewport={'width':1440,'height':1080})
            context.route('**/api/runs/demo/status',lambda route:route.fulfill(json=current))
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(server['origin'])
            page.get_by_label('Username').fill('operator@pve')
            page.get_by_label('Password',exact=True).fill(PASSWORD)
            page.get_by_role('button',name='Sign in',exact=True).click()
            expect(page.locator('#refresh')).to_be_enabled()
            page.evaluate("localStorage.setItem('caf-refresh-minutes','0')")
            page.goto(server['origin']+'/run?view=progress&run=demo')
            details=page.locator('.transcript-details')
            details.locator('summary').click()
            expect(details).to_have_attribute('open','')
            expect(page.get_by_label('Transcript trial')).to_have_value('trial-000002|1')
            expect(details).to_contain_text('trial 2 output 39')
            assert page.evaluate('window.injected') is None
            page.clock.install()
            page.evaluate("dispatchEvent(new StorageEvent('storage',{key:'caf-refresh-minutes',newValue:'0'}))")
            output=page.locator('.transcript-output')
            output.evaluate('node=>node.scrollTop=0')
            page.wait_for_timeout(100)
            trials[1]['transcript']['events'].append({'type':'tool_result','elapsed_seconds':40,'text':'new streamed output'})
            page.clock.fast_forward(5000)
            expect(details).to_contain_text('new streamed output')
            expect(details).to_have_attribute('open','')
            assert output.evaluate('node=>node.scrollTop') < 10
            page.get_by_label('Transcript trial').select_option('trial-000001|1')
            expect(details).to_contain_text('trial 1 output 39')
            page.clock.fast_forward(5000)
            expect(page.get_by_label('Transcript trial')).to_have_value('trial-000001|1')
            page.get_by_label('Transcript trial').select_option('trial-000002|1')
            output.evaluate('node=>node.scrollTop=node.scrollHeight')
            page.wait_for_timeout(100)
            trials[1]['transcript']['events'].append({'type':'response','elapsed_seconds':41,'text':'final answer'})
            page.clock.fast_forward(5000)
            expect(details).to_contain_text('final answer')
            page.wait_for_timeout(100)
            assert output.evaluate('node=>node.scrollHeight-node.clientHeight-node.scrollTop') < 40
            page.screenshot(path='/tmp/orchestrator-transcript.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            browser.close()
    print('PASS: live step 9 transcript, Never refresh independence, inert content, selection/scroll preservation and mobile layout')


if __name__=='__main__':
    main()
