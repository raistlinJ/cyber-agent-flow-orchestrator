"""Queue dismissal and real HTTPS/SSE transcript updates with simulated guests."""
from pathlib import Path
import sys
import tempfile
import threading
import pytest
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_eval.live_transcript import LiveTranscript
from test_workflow import lab,TrialBackend
from scenario_fixture import install_transport
from test_pve_auth import pve_server,make_auth
from test_user_access import vm,Probe
from https_fixture import secure_server,PASSWORD


def main():
    release_launch,finish,started=threading.Event(),threading.Event(),threading.Event()
    holder={}
    with tempfile.TemporaryDirectory() as temp,pytest.MonkeyPatch.context() as patch:
        root=Path(temp)
        config,_,agent,manifest=lab.__wrapped__(root,patch)
        install_transport(patch,agent,manifest)
        original_launch=TrialBackend.launch
        def launch(self,directory,seconds):
            mirror=LiveTranscript(directory);holder['mirror']=mirror
            mirror.emit({'type':'prompt','text':'Fetch the service token from the lab website.'})
            mirror.emit({'type':'reasoning','text':'Provider-returned explanation: inspect the HTTP response.'})
            started.set()
            if not finish.wait(45):raise AssertionError('Browser did not release the simulated trial')
            return original_launch(self,directory,seconds)
        patch.setattr(TrialBackend,'launch',launch)
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve']=[vm(9402),vm(9403),vm(9404)]
            dashboard=UserDashboard(config,root/'runs',2,lambda b,a:Probe(b,a,[]))
            original=dashboard.experiment
            def experiment(access,action,data):
                if action=='run' and not release_launch.wait(15):raise AssertionError('Launch modal was not dismissed')
                return original(access,action,data)
            patch.setattr(dashboard,'experiment',experiment)
            try:
                with secure_server(dashboard,root/'secure',auth=make_auth(pve)) as server,sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True)
                    context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1080})
                    page=context.new_page();errors=[]
                    page.route('**/api/model-network-scope',lambda route:route.fulfill(json={'provider_host':'localhost','excluded_targets':['127.0.0.1'],'warnings':[]}))
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click()
                    for role,vmid in [('scenarioforge','9402'),('participant','9403'),('core','9404')]:page.locator('#role-'+role).select_option(vmid)
                    page.get_by_role('button',name='Save VM roles').click()
                    expect(page.locator('#loading-modal')).to_be_hidden(timeout=30000)
                    page.locator('#refresh-period').select_option('0')
                    page.locator('[data-route=experiments]').click()
                    page.locator('#new-experiment').click()
                    page.get_by_role('tab',name='Evaluation',exact=True).click();page.locator('#judge-enabled').uncheck()
                    expect(page.locator('#create-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#create-experiment').click()
                    expect(page.locator('#experiment-dialog')).to_be_hidden(timeout=30000)
                    page.get_by_role('button',name='Deploy and run',exact=True).click()
                    expect(page.locator('#dismiss-loading')).to_be_visible()
                    page.locator('#dismiss-loading').click()
                    expect(page.locator('#loading-modal')).to_be_hidden()
                    expect(page.get_by_role('region',name='Run queue')).to_be_visible()
                    page.wait_for_timeout(1200)
                    expect(page.locator('#loading-modal')).to_be_hidden() # Heartbeat must not reopen it.
                    release_launch.set()
                    assert started.wait(10)
                    button=page.locator('#sample-global-status [data-transcript-run]')
                    expect(button).to_be_enabled(timeout=15000)
                    expect(button).to_have_class('agent-transcript is-executing',timeout=15000)
                    page.screenshot(path='/tmp/caf-live-run-queue.png')
                    with page.expect_popup() as opened:button.click()
                    popup=opened.value;popup.on('pageerror',lambda e:errors.append(str(e)))
                    expect(popup.locator('#transcript-events')).to_contain_text('Fetch the service token',timeout=10000)
                    expect(popup.locator('#transcript-events')).to_contain_text('Provider-returned explanation')
                    holder['mirror'].emit({'type':'tool_result','tool':'curl','args':{'url':'http://lab/'},'result':'Observed token <script>window.hacked=true</script>','exit_code':0})
                    expect(popup.locator('#transcript-events')).to_contain_text('Observed token',timeout=10000)
                    assert popup.evaluate('window.hacked') is None
                    popup.screenshot(path='/tmp/caf-live-transcript.png')
                    popup.locator('#refresh-run').click()
                    expect(popup.locator('#transcript-events .transcript-event')).to_have_count(3)
                    popup.close()
                    expect(page.get_by_role('region',name='Run queue')).to_be_visible()
                    assert not finish.is_set()
                    with page.expect_popup() as reopened:button.click()
                    popup=reopened.value
                    expect(popup.locator('#transcript-events .transcript-event')).to_have_count(3,timeout=10000)
                    finish.set()
                    expect(popup.locator('#run-status')).to_contain_text('Transcript complete',timeout=20000)
                    expect(page.locator('#runs tr').filter(has_text='completed')).to_have_count(1,timeout=20000)
                    assert not errors,errors
                    popup.close();browser.close()
            finally:
                release_launch.set();finish.set();dashboard.close()
    print('PASS: launch dismissal retains queue, guest indicator pulses, HTTPS transcript streams/replays safely, closing does not stop the run; Never refresh remains independent')


if __name__=='__main__':main()
