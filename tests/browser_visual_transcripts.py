"""Side-by-side visual transcripts follow graph selection and preserve independent scroll."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_refresh import Dashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server,make_auth
from test_progress_visualization import fixture
from cyber_agent_flow_orchestrator.progress_visualization import build


def main():
    with tempfile.TemporaryDirectory() as temp,sync_playwright() as pw:
        root=Path(temp)
        row,_,_=fixture(root/'run')
        graph=build(root/'run',{},[row])
        now=datetime.now(timezone.utc)
        action=dict(live=True,phase='tool',agent_state='using_tool',tool='curl',kind='interaction',step_ids=['s2'],context_step_ids=[],targets=[dict(address='10.0.0.3',step_ids=['s2'],range=False)],observed_at=now.isoformat(),expires_at=(now+timedelta(seconds=40)).isoformat())
        graph['trials'][0]['activity']=action
        second=deepcopy(graph['trials'][0]);second.update(trial_id='trial-000002',status='completed',condition_id='configuration-2');second['activity'].update(live=False,phase='finished',expires_at=None)
        graph['trials'].append(second)
        def status(text):return dict(type='status',text=json.dumps(dict(text=text)))
        trials=[]
        for number in (1,2):
            trials.append(dict(trial_id=f'trial-{number:06d}',task_id='task',condition_id=f'configuration-{number}',attempt=1,status='running' if number==1 else 'completed',transport=dict(phase='executing'),
                transcript=dict(live=True,updated_at=now.isoformat(),events=[dict(type='response',elapsed_seconds=n,text=json.dumps(dict(text=f'CAF trial {number} output {n} <script>window.transcriptInjected=true</script>',turn=n))) for n in range(40)]),
                judge_transcript=dict(live=True,updated_at=now.isoformat(),events=[dict(type='reasoning',text=json.dumps(dict(text=f'Judge trial {number} evidence {n}',purpose='progress'))) for n in range(40)]+[status('Judge review completed')])))
        stage=dict(id='evaluate',label='Run and score trials',active=True,status='running',vmid=9403,events=[])
        current=dict(output='/saved/demo',workflow_id='demo',recorded_status='evaluating',coordinator_active=True,workflow_progress=dict(steps=[stage],current=stage,current_step='evaluate',roles=[],completed_steps=0,total_steps=1),
            sample_progress=dict(name='Demo',active=True,planned_trials=2,finished_trials=0,verified_successes=0,errors=0,trials=trials,current_trial=trials[0],events=[dict(at=now.isoformat(),kind='checkpoint',message=f'Saved event {n} <script>window.logInjected=true</script>') for n in range(60)],visualization=graph))
        with pve_server(root) as pve,secure_server(Dashboard(),root/'web',auth=make_auth(pve)) as server:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(ignore_https_errors=True,viewport={'width':1600,'height':1100})
            reads=[]
            def respond(route):reads.append(route.request.url);route.fulfill(json=current)
            context.route('**/api/runs/demo/status',respond)
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(server['origin']);page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD);page.get_by_role('button',name='Sign in',exact=True).click()
            expect(page.locator('#refresh')).to_be_enabled()
            page.evaluate("localStorage.setItem('caf-refresh-minutes','0');localStorage.setItem('caf-console-open','false')")
            page.clock.install()
            page.goto(server['origin']+'/run?view=progress&run=demo&display=visualize')
            page.locator('#visual-tab-transcripts').click()
            caf=page.locator('#visual-caf-panel');judge=page.locator('#visual-judge-panel');caf_output=page.locator('#visual-caf-transcript');judge_output=page.locator('#visual-judge-transcript')
            expect(caf).to_contain_text('CAF trial 1 output 39')
            # Speaker controls share a slim toolbar; dock space belongs to the text.
            expect(page.get_by_role('heading',name='Live transcripts',exact=True)).to_have_count(0)
            assert caf.locator('.visual-transcript-heading').bounding_box()['height']<=27
            expect(caf.locator('.visual-transcript-heading #visual-caf-follow')).to_be_visible()
            expect(caf.locator('.visual-transcript-heading #visual-caf-latest')).to_be_visible()
            assert caf_output.bounding_box()['height']>=caf.bounding_box()['height']-28
            assert page.locator('#visual-hint-unlock').bounding_box()['height']<=25
            expect(judge).to_contain_text('Judge trial 1 evidence 39')
            readable=caf.locator('.visual-transcript-event > pre').first
            expect(readable).to_contain_text('CAF trial 1 output 0')
            expect(readable).not_to_contain_text('"text":')
            expect(readable).to_contain_text('Turn: 0')
            expect(page.get_by_text('Raw JSON',exact=True)).to_have_count(0)
            # Nested judge verdicts and tool arguments stay readable and complete.
            rendered=page.evaluate("""()=>{
             const entry=document.createElement('article');
             renderTranscriptText(entry,{text:JSON.stringify({text:JSON.stringify({action:'verdict',criteria:[{id:'criterion-1',status:'satisfied',rationale:'Observed proof'}]}),turn:2})});
             return entry.querySelector('pre').textContent;
            }""")
            assert 'Action: verdict' in rendered and 'Status: satisfied' in rendered and 'Rationale: Observed proof' in rendered
            assert '"criteria"' not in rendered and 'Turn: 2' in rendered
            assert page.evaluate("transcriptReadableText(JSON.stringify({name:'execute_command',args:{command:'curl http://10.0.0.3/'},result:{stdout:'Observed token',exit_code:0}}))")== 'Name: execute_command\n\nArgs:\n  Command: curl http://10.0.0.3/\n\nResult:\n  Stdout: Observed token\n  \n  Exit code: 0'
            assert page.evaluate("transcriptReadableText('{incomplete')")=='{incomplete'
            expect(page.locator('#visual-caf-badge')).to_have_text('Active')
            expect(page.locator('#visual-judge-badge')).to_have_text('Waiting') # Both legacy live flags are true; only CAF is working.
            assert caf.bounding_box()['x']<judge.bounding_box()['x']
            assert abs(caf.bounding_box()['y']-judge.bounding_box()['y'])<1
            assert caf.bounding_box()['y']>page.locator('#attack-graph-wrap').bounding_box()['y']
            assert page.evaluate('window.transcriptInjected') is None
            page.locator('#visual-transcripts').scroll_into_view_if_needed()
            canvas=page.locator('#attack-graph');canvas_before=canvas.bounding_box()
            assert canvas_before['height']>150, page.evaluate("Object.fromEntries(['attack-graph','visual-editor','debug-console'].map(id=>{const e=document.getElementById(id);return [id,{bounds:JSON.stringify(e.getBoundingClientRect()),style:e.getAttribute('style'),parent:e.parentElement.id}]}))")
            page.locator('#visual-tab-events').click()
            expect(page.locator('#visual-pane-events')).to_be_visible()
            expect(page.get_by_text('About this graph',exact=True)).to_have_count(0)
            expect(page.locator('#debug-console')).not_to_be_visible()
            runlog=page.locator('#visual-events-output')
            expect(runlog).to_contain_text('Saved event 59')
            runlog.evaluate('(node)=>node.scrollTop=80');page.wait_for_timeout(100)
            expect(page.locator('#visual-events-follow')).not_to_be_checked()
            current['sample_progress']['events'].append(dict(at=now.isoformat(),kind='checkpoint',message='Newest saved checkpoint'))
            page.locator('#refresh-run').click()
            expect(runlog).to_contain_text('Newest saved checkpoint')
            expect(page.locator('#visual-tab-events')).to_have_attribute('aria-selected','true')
            assert abs(runlog.evaluate('(node)=>node.scrollTop')-80)<1
            assert page.evaluate('window.logInjected') is None
            assert abs(canvas.bounding_box()['y']-canvas_before['y'])<1
            page.locator('#visual-tab-hints').click()
            expect(page.locator('#visual-pane-hints')).to_be_visible()
            page.locator('#visual-tab-hints').press('ArrowLeft')
            expect(page.locator('#visual-tab-transcripts')).to_be_focused()
            expect(page.locator('#visual-transcripts')).to_be_visible()
            caf_output.evaluate('(node)=>node.scrollTop=0');page.wait_for_timeout(100)
            expect(page.locator('#visual-caf-follow')).not_to_be_checked()
            page.locator('#visual-caf-events article').first.evaluate('(node)=>node.dataset.identity="preserved"')
            # Transcript-only updates arrive even when the graph fingerprint is unchanged.
            trials[0]['transcript']['events'].append(dict(type='tool_result',text='New CAF output'))
            trials[0]['judge_transcript']['events'].append(dict(type='status',text='Saved judge event'))
            page.clock.fast_forward(5000)
            expect(caf).to_contain_text('New CAF output')
            page.locator('#visual-tab-events').click()
            assert abs(runlog.evaluate('(node)=>node.scrollTop')-80)<1
            page.locator('#visual-tab-transcripts').click()
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            assert abs(canvas.bounding_box()['y']-canvas_before['y'])<1
            assert caf_output.evaluate('(node)=>node.scrollTop')<10
            assert page.locator('#visual-caf-events article').first.get_attribute('data-identity')=='preserved'
            assert judge_output.evaluate('(node)=>node.scrollHeight-node.clientHeight-node.scrollTop')<40
            # A checkpoint review highlights judge and pauses CAF, without moving the reader.
            action.update(live=False,phase='reviewing',expires_at=None)
            graph['trials'][0]['monitor']['latest']['status']='running'
            trials[0]['judge_transcript']['events'].append(status('Judge review started'))
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-judge-badge')).to_have_text('Active')
            expect(page.locator('#visual-caf-badge')).to_have_text('Paused')
            expect(judge).to_have_attribute('aria-busy','true')
            page.locator('#graph-focus').click();page.locator('.flow-action').focus();page.locator('.flow-action').press('Enter')
            page.locator('#visual-node-detail').get_by_role('button',name='View judge transcript',exact=True).click()
            expect(judge_output).to_be_focused()
            expect(page.get_by_role('tab',name='Visualize',exact=True)).to_have_attribute('aria-selected','true')
            trials[0]['judge_transcript']['events'].append(dict(type='response',text='New judge review output'))
            page.clock.fast_forward(5000)
            expect(judge).to_contain_text('New judge review output')
            assert caf_output.evaluate('(node)=>node.scrollTop')<10
            judge_output.evaluate('(node)=>node.scrollTop=0');page.wait_for_timeout(100)
            expect(page.locator('#visual-judge-follow')).not_to_be_checked()
            page.locator('#visual-judge-panel [data-editor-toggle=judge]').click()
            expect(judge_output).not_to_be_visible()
            page.locator('#visual-judge-panel [data-editor-toggle=judge]').click()
            expect(judge_output).to_be_visible()
            assert judge_output.evaluate('(node)=>node.scrollTop')<10
            expect(page.locator('#visual-judge-follow')).not_to_be_checked()
            page.locator('#visual-caf-panel [data-editor-toggle=caf]').click()
            expect(caf_output).not_to_be_visible()
            page.locator('#visual-caf-panel [data-editor-toggle=caf]').click()
            expect(caf_output).to_be_visible()
            assert caf_output.evaluate('(node)=>node.scrollTop')<10
            page.locator('#visual-caf-latest').click()
            expect(page.locator('#visual-caf-follow')).to_be_checked()
            assert caf_output.evaluate('(node)=>node.scrollHeight-node.clientHeight-node.scrollTop')<40
            assert judge_output.evaluate('(node)=>node.scrollTop')<10
            # CAF takes over again after a completed checkpoint review.
            graph['trials'][0]['monitor']['latest']['status']='completed'
            trials[0]['judge_transcript']['events'].append(status('Judge review completed'))
            action.update(live=True,phase='tool',observed_at=page.evaluate('new Date().toISOString()'),expires_at=page.evaluate('new Date(Date.now()+40000).toISOString()'))
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-caf-badge')).to_have_text('Active')
            expect(page.locator('#visual-judge-badge')).to_have_text('Waiting')
            # Dock Follow Actions switches on actual work, not unchanged poll snapshots.
            follow=page.get_by_role('switch',name='Dock Follow Actions',exact=True)
            expect(follow).not_to_be_checked()
            page.locator('#visual-tab-events').click()
            graph_view=canvas.get_attribute('viewBox')
            follow.check()
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            expect(caf).to_have_class(re.compile(r'.*is-follow-target.*'))
            expect(follow).to_be_focused()
            assert canvas.get_attribute('viewBox')==graph_view
            assert judge_output.evaluate('(node)=>node.scrollTop')<10
            page.locator('#visual-tab-hints').click()
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-hints')).to_have_attribute('aria-selected','true')
            trials[0]['transcript']['events'].append(dict(type='reasoning',text='A new action to follow'))
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            # Follow the judge, reopen a folded transcript, preserve keyboard focus.
            page.locator('#visual-judge-panel [data-editor-toggle=judge]').click()
            action.update(live=False,phase='reviewing',expires_at=None)
            graph['trials'][0]['monitor']['latest']['status']='running'
            trials[0]['judge_transcript']['events'].append(status('Judge review started'))
            follow.focus()
            page.clock.fast_forward(5000)
            expect(judge_output).to_be_visible()
            expect(judge).to_have_class(re.compile(r'.*is-follow-target.*'))
            expect(follow).to_be_focused()
            assert judge_output.evaluate('(node)=>node.scrollTop')<10
            release=dict(id='follow-hint',step_id='s2',turn=9,at=page.evaluate('new Date().toISOString()'),text='New assistance to follow',solution=False)
            graph['trials'][0]['releases'].append(release)
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-hints')).to_have_attribute('aria-selected','true')
            expect(page.locator('#visual-assistance details[open]')).to_contain_text('New assistance to follow')
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-hints')).to_have_attribute('aria-selected','true')
            graph['trials'][0]['monitor']['latest']['status']='completed'
            trials[0]['judge_transcript']['events'].append(status('Judge review completed'))
            action.update(live=True,phase='tool',observed_at=page.evaluate('new Date().toISOString()'),expires_at=page.evaluate('new Date(Date.now()+40000).toISOString()'))
            trials[0]['transcript']['events'].append(dict(type='tool_result',text='CAF resumed after assistance'))
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            # Preparation/transfer work belongs in Run log.
            trials[0]['transport']['phase']='uploading'
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-events')).to_have_attribute('aria-selected','true')
            trials[0]['transport']['phase']='executing'
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            page.locator('#visual-trial').select_option('trial-000002|task|1')
            expect(page.locator('#visual-trial')).to_have_value('trial-000001|task|1')
            # Disabling pins the chosen tab through new activity.
            follow.uncheck()
            page.locator('#visual-tab-hints').click()
            trials[0]['transcript']['events'].append(dict(type='tool_result',text='Stay on the chosen tab'))
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-tab-hints')).to_have_attribute('aria-selected','true')
            # Completed trials never blink as active, even with stale live flags.
            page.locator('#visual-tab-hints').click()
            page.locator('#visual-trial').select_option('trial-000002|task|1')
            expect(page.locator('#visual-tab-hints')).to_have_attribute('aria-selected','true')
            page.locator('#visual-tab-transcripts').click()
            expect(caf).to_contain_text('CAF trial 2 output 39')
            expect(judge).to_contain_text('Judge trial 2 evidence 39')
            expect(caf).not_to_contain_text('New CAF output')
            expect(page.locator('#visual-caf-badge')).to_have_text('Saved')
            expect(page.locator('#visual-judge-badge')).to_have_text('Saved')
            page.locator('#visual-trial').select_option('trial-000001|task|1')
            expect(caf).to_contain_text('New CAF output')
            expect(page.locator('#visual-judge-follow')).not_to_be_checked()
            assert judge_output.evaluate('(node)=>node.scrollTop')<10
            # Coming back from Phase progress still follows newly appended CAF output.
            page.get_by_role('tab',name='Phase progress',exact=True).click()
            expect(page.locator('#debug-console')).to_be_visible()
            trials[0]['transcript']['events'].append(dict(type='reasoning',text='CAF output received while the graph was hidden'))
            page.clock.fast_forward(5000)
            page.get_by_role('tab',name='Visualize',exact=True).click()
            expect(caf).to_contain_text('CAF output received while the graph was hidden')
            page.wait_for_timeout(100)
            assert caf_output.evaluate('(node)=>node.scrollHeight-node.clientHeight-node.scrollTop')<40
            # Final host scoring highlights judge after the guest has stopped.
            action.update(live=False,phase='stale',expires_at=None);graph['trials'][0]['monitor']['latest']['status']='completed';trials[0]['transport']['phase']='collected'
            trials[0]['judge_transcript']['events'].append(status('Judge review started'))
            page.clock.fast_forward(5000)
            expect(page.locator('#visual-judge-badge')).to_have_text('Active')
            page.locator('#visual-transcripts').scroll_into_view_if_needed();page.screenshot(path='/tmp/caf-visual-transcripts-desktop.png',full_page=True)
            follow.check()
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            page.locator('#visual-tab-events').click()
            # A stopped status feed expires the active marker without clearing readable logs.
            page.evaluate('visualState.receivedAt=performance.now()-46000;syncVisualTranscriptActivity()')
            expect(page.locator('#visual-judge-badge')).to_have_text('Updates stale')
            page.wait_for_timeout(100)
            expect(page.locator('#visual-tab-events')).to_have_attribute('aria-selected','true')
            expect(judge).to_contain_text('New judge review output')
            current.update(recorded_status='completed',coordinator_active=True);current['sample_progress']['active']=True
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-judge-badge')).to_have_text('Saved')
            expect(page.locator('#visual-caf-badge')).to_have_text('Saved')
            expect(page.locator('#visual-tab-events')).to_have_attribute('aria-selected','true')
            page.locator('#visual-tab-transcripts').click()
            page.set_viewport_size({'width':430,'height':900})
            page.wait_for_timeout(100)
            mobile_canvas=canvas.bounding_box()
            page.locator('.visual-transcript-grid').evaluate('(node)=>node.scrollTop=node.scrollHeight')
            assert abs(canvas.bounding_box()['y']-mobile_canvas['y'])<1
            assert canvas.bounding_box()['y']>=0
            assert canvas.bounding_box()['y']+canvas.bounding_box()['height']<900
            assert judge.bounding_box()['y']>caf.bounding_box()['y']
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path='/tmp/caf-visual-transcripts-mobile.png',full_page=True)
            expect(follow).to_be_visible()
            # Preflight uses its own explicitly labelled startup transcript, never a trial verdict.
            current.update(recorded_status='preparing',coordinator_active=True)
            current['sample_progress'].update(trials=[],current_trial=None,visualization=dict(trials=[]))
            stage.update(id='preflight',input_checks=[dict(id='judge',status='running')])
            current['judge_preflight_transcript']=dict(live=True,events=[dict(type='response',text='Synthetic judge startup response')])
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-judge-context')).to_have_text('Test LLM Judge · startup check')
            expect(page.locator('#visual-judge-badge')).to_have_text('Active')
            expect(judge).to_contain_text('Synthetic judge startup response')
            expect(caf).not_to_contain_text('CAF trial 1')
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            expect(judge).to_have_class(re.compile(r'.*is-follow-target.*'))
            page.reload()
            expect(follow).to_be_checked()
            expect(page.locator('#visual-tab-transcripts')).to_have_attribute('aria-selected','true')
            page.screenshot(path='/tmp/caf-follow-actions-mobile.png')
            assert not errors,errors
            browser.close()
    print('PASS: side-by-side transcripts, activity handoff, selected-trial isolation, stable incremental updates/independent scroll, final judge/startup, Never polling, stale expiry and mobile')


if __name__=='__main__':main()
