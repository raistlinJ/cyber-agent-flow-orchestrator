"""Real EventSource over HTTPS: deltas, shared viewers, reconnect, fallback and final state."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_refresh import Dashboard
from test_pve_auth import pve_server, make_auth
from https_fixture import secure_server, PASSWORD
from test_progress_visualization import fixture
from cyber_agent_flow_orchestrator.progress_visualization import build
from cyber_agent_flow_orchestrator.run_stream import RunStreams


class StreamingDashboard(Dashboard):
    def __init__(self, current):
        super().__init__()
        self.current, self.version = current, 1
        self.hub = RunStreams(interval=.05, refresh=60)
        self.lock = threading.RLock()
        self.connections, self.reads = 0, 0
    def snapshot(self):
        with self.lock: return deepcopy(self.current)
    def run_detail(self, access, run_id, kind):
        access.current()
        assert run_id == 'demo' and kind == 'status'
        self.reads += 1
        return self.snapshot()
    def run_stream(self, access, run_id):
        access.current()
        assert run_id == 'demo'
        self.connections += 1
        return self.hub.subscribe((access.username, run_id), self.snapshot, lambda: self.version)
    def change(self, callback):
        with self.lock:
            callback(self.current)
            self.version += 1


def main():
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as pw:
        root = Path(temp)
        row, _, _ = fixture(root/'run')
        graph = build(root/'run', {}, [row])
        now = datetime.now(timezone.utc)
        action=dict(live=True,phase='tool',agent_state='using_tool',tool='curl',kind='interaction',step_ids=['s2'],context_step_ids=[],targets=[dict(address='10.0.0.3',step_ids=['s2'],range=False)],observed_at=now.isoformat(),expires_at=(now+timedelta(seconds=40)).isoformat())
        graph['trials'][0]['activity'] = action
        trial = dict(trial_id='trial-000001',task_id='task',condition_id='configuration-1',attempt=1,status='running',transport=dict(phase='executing',updated_at=now.isoformat()),
            transcript=dict(live=True,updated_at=now.isoformat(),events=[dict(type='response',text=json.dumps(dict(text=f'Initial CAF output {n} <script>window.UNSAFE=true</script>'))) for n in range(40)]),
            judge_transcript=dict(live=True,updated_at=now.isoformat(),events=[dict(type='status',text=json.dumps(dict(text='Judge review completed')))]))
        phase=dict(id='evaluate',label='Run and score trials',description='Run participant tasks.',status='running',active=True,events=[])
        current=dict(output='/saved/demo',workflow_id='demo',recorded_status='evaluating',coordinator_active=True,workflow_progress=dict(steps=[phase],current=phase,current_step='evaluate',completed_steps=0,total_steps=1,roles=[]),
            sample_progress=dict(name='Demo',active=True,planned_trials=1,finished_trials=0,verified_successes=0,errors=0,trials=[trial],current_trial=trial,visualization=graph,events=[]))
        dashboard=StreamingDashboard(current)
        try:
            with pve_server(root) as pve, secure_server(dashboard,root/'web',auth=make_auth(pve)) as server:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                context=browser.new_context(ignore_https_errors=True,viewport=dict(width=1440,height=1000))
                page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(server['origin']);page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD);page.get_by_role('button',name='Sign in',exact=True).click()
                expect(page.locator('#refresh')).to_be_enabled()
                page.evaluate("localStorage.setItem('caf-refresh-minutes','0')")
                url=server['origin']+'/run?view=progress&run=demo&display=visualize'
                page.goto(url)
                expect(page.locator('#run-status')).to_contain_text('Live updates · streaming')
                page.locator('#visual-tab-transcripts').click()
                caf=page.locator('#visual-caf-events');output=page.locator('#visual-caf-transcript')
                expect(caf).to_contain_text('Initial CAF output 39')
                assert page.evaluate('window.UNSAFE') is None
                assert dashboard.reads == 1
                assert dashboard.hub.clients == 1
                page.clock.install()
                page.clock.fast_forward(6000)
                assert dashboard.reads == 1  # No recurring HTTP status request on a healthy stream.
                page.locator('#visual-caf-events > article').first.evaluate('(node)=>node.dataset.identity="preserved"')
                output.evaluate('(node)=>node.scrollTop=0');page.wait_for_timeout(100)
                expect(page.locator('#visual-caf-follow')).not_to_be_checked()
                viewbox=page.locator('#attack-graph').get_attribute('viewBox')
                def append(run): run['sample_progress']['trials'][0]['transcript']['events'].append(dict(type='tool_result',text=json.dumps(dict(text='New streamed tool output'))))
                dashboard.change(append)
                expect(caf).to_contain_text('New streamed tool output')
                assert output.evaluate('(node)=>node.scrollTop')<10
                assert page.locator('#visual-caf-events > article').first.get_attribute('data-identity')=='preserved'
                assert page.locator('#attack-graph').get_attribute('viewBox')==viewbox
                # A second real connection shares the same producer.
                second=context.new_page();second.goto(url)
                expect(second.locator('#run-status')).to_contain_text('Live updates · streaming')
                assert len(dashboard.hub.feeds)==1 and dashboard.hub.clients==2
                second.close()
                follow=page.get_by_role('switch',name='Dock Follow Actions',exact=True);follow.check()
                def review(run):
                    item=run['sample_progress']['trials'][0]
                    item['judge_transcript']['events'].append(dict(type='status',text=json.dumps(dict(text='Judge review started'))))
                    target=run['sample_progress']['visualization']['trials'][0]
                    target['activity'].update(live=False,phase='reviewing',expires_at=None)
                    target['monitor']['latest']['status']='running'
                dashboard.change(review)
                expect(page.locator('#visual-judge-badge')).to_have_text('Active')
                expect(page.locator('#visual-judge-panel')).to_have_class(re.compile('.*is-follow-target.*'))
                def hint(run): run['sample_progress']['visualization']['trials'][0]['releases'].append(dict(id='sse-hint',step_id='s2',turn=9,text='Hint delivered over SSE',solution=False,at=now.isoformat()))
                dashboard.change(hint)
                expect(page.locator('#visual-tab-hints')).to_have_attribute('aria-selected','true')
                expect(page.locator('#visual-assistance details[open]')).to_contain_text('Hint delivered over SSE')
                # Browser heartbeat freshness must not extend the recorded action lease.
                deadline=page.evaluate('visualState.activityDeadline')
                page.evaluate("runStream.dispatchEvent(new MessageEvent('heartbeat',{data:'{}'}))")
                assert page.evaluate('visualState.activityDeadline')==deadline
                # EOF automatically reconnects with a full, current snapshot.
                connections=dashboard.connections
                for feed in list(dashboard.hub.feeds.values()): feed.close()
                page.wait_for_function('() => streamState === "reconnecting"')
                page.wait_for_function('() => streamState === "live"',timeout=10000)
                assert dashboard.connections>connections
                expect(page.locator('#visual-assistance')).to_contain_text('Hint delivered over SSE')
                assert page.locator('#visual-assistance').get_by_text('Hint delivered over SSE',exact=True).count()==1
                # Closed transport: fallback stays functional, then a manual refresh retries SSE.
                page.evaluate("runStream.close();runStream.dispatchEvent(new Event('error'))")
                expect(page.locator('#run-status')).to_contain_text('polling every 5 seconds')
                reads=dashboard.reads
                page.clock.fast_forward(5000)
                page.wait_for_function('() => loading === false')
                expect(page.locator('#refresh-run')).to_be_enabled()
                assert dashboard.reads>reads
                page.clock.fast_forward(30000)
                expect(page.locator('#run-status')).to_contain_text('Live updates · streaming')
                page.evaluate("runStream.close();runStream.dispatchEvent(new Event('error'))")
                expect(page.locator('#run-status')).to_contain_text('polling every 5 seconds')
                page.locator('#refresh-run').click()
                expect(page.locator('#run-status')).to_contain_text('Live updates · streaming')
                # An out-of-sequence delta cannot silently corrupt the run.
                page.evaluate("runStream.dispatchEvent(new MessageEvent('update',{data:JSON.stringify({base:-1,revision:999,changes:[]})}))")
                expect(page.locator('#run-status')).to_contain_text('polling every 5 seconds')
                page.locator('#refresh-run').click()
                expect(page.locator('#run-status')).to_contain_text('Live updates · streaming')
                assert page.evaluate("(()=>{const before={};const after=applyRunChanges(before,[{op:'set',path:['__proto__'],value:{safe:true}}]);return Object.hasOwn(after,'__proto__')&&!({}).safe&&!Object.hasOwn(before,'__proto__');})()")
                # Completion is pushed once, including with Never selected and a stale active flag.
                def finish(run):
                    run['recorded_status']='completed'
                    run['sample_progress']['finished_trials']=1
                    run['sample_progress']['trials'][0]['status']='completed'
                    run['workflow_progress']['current'].update(active=False,status='completed')
                dashboard.change(finish)
                expect(page.locator('#run-status')).to_contain_text('completed')
                expect(page.locator('#run-status')).to_contain_text('Automatic refresh off')
                assert page.evaluate('runStream') is None
                before=dashboard.reads;page.clock.fast_forward(61000);assert dashboard.reads==before
                page.set_viewport_size(dict(width=430,height=900))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path='/tmp/caf-sse-progress-mobile.png')
                assert not errors,errors
                browser.close()
        finally: dashboard.hub.close()
    print('PASS: real HTTPS SSE, shared viewers, incremental transcripts/hints, stable scroll/zoom, automatic reconnect, polling fallback/resync, terminal Never and mobile')


if __name__=='__main__': main()
