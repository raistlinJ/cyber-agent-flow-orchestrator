"""Visual Monitor: evidence, assistance, accessible selection and stable live views."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_refresh import Dashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_progress_visualization import fixture
from test_preparation_visualization import preparation_fixture
from cyber_agent_flow_orchestrator.preparation_visualization import (
    build as preparation_build,
)
from cyber_agent_flow_orchestrator.progress_visualization import build


def main():
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as pw:
        root = Path(temp)
        row, _, _ = fixture(root / "run")
        visual = build(root / "run", {}, [row])
        activity_now = datetime.now(timezone.utc)
        visual["trials"][0]["activity"] = dict(
            live=True,
            phase="tool",
            tool="nmap",
            kind="scan",
            step_ids=["s2"],
            context_step_ids=[],
            targets=[
                dict(address="10.0.0.3", step_ids=["s2"], range=False),
                dict(address="192.0.2.8", step_ids=[], range=False),
            ],
            observed_at=activity_now.isoformat(),
            event_at=activity_now.isoformat(),
            expires_at=(activity_now + timedelta(seconds=40)).isoformat(),
            source=dict(
                file="evaluation/trials/trial-000001/attempt-0001/live-events.jsonl",
                offset=0,
                limit=100,
            ),
        )
        second = deepcopy(visual["trials"][0])
        second.update(trial_id="trial-000002", condition_id="configuration-2")
        visual["trials"].append(second)
        steps = [
            dict(
                id="preflight",
                label="1. Check inputs",
                status="completed",
                active=False,
                elapsed_seconds=3,
            ),
            dict(
                id="evaluate",
                label="2. Run and score trials",
                vmid=9403,
                description="Run participant and evaluate evidence.",
                status="running",
                active=True,
                events=[],
            ),
        ]
        prep_steps, prep_roles = preparation_fixture()
        prep = preparation_build(prep_steps, prep_roles)
        current = dict(
            output="/saved/demo",
            workflow_id="demo",
            recorded_status="evaluating",
            coordinator_active=True,
            workflow_progress=dict(
                steps=steps,
                current=steps[1],
                current_step="evaluate",
                completed_steps=1,
                total_steps=2,
                roles=prep_roles,
                preparation=prep,
            ),
            sample_progress=dict(
                name="Demo",
                active=True,
                planned_trials=2,
                finished_trials=0,
                verified_successes=0,
                errors=0,
                trials=[],
                events=[],
                visualization=visual,
                current_trial=dict(trial_id="trial-000001", attempt=1),
            ),
        )
        with (
            pve_server(root) as pve,
            secure_server(Dashboard(), root / "web", auth=make_auth(pve)) as server,
        ):
            browser = pw.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(
                ignore_https_errors=True, viewport={"width": 1600, "height": 1150}
            )
            reads = []

            def status(route):
                reads.append(route.request.url)
                route.fulfill(json=current)

            context.route("**/api/runs/demo/status", status)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server["origin"])
            page.get_by_label("Username").fill("operator@pve")
            page.get_by_label("Password", exact=True).fill(PASSWORD)
            page.get_by_role("button", name="Sign in", exact=True).click()
            expect(page.locator("#refresh")).to_be_enabled()
            page.evaluate(
                "localStorage.setItem('caf-refresh-minutes','0');localStorage.setItem('caf-console-open','false')"
            )
            page.goto(server["origin"] + "/run?view=progress&run=demo")
            expect(page.locator("#phase-title")).to_have_text("Run and score trials")
            expect(page.locator('#progress-view-tabs [role=tab]')).to_have_text(['Visualize','Phase progress'])
            expect(page.locator('#visualize-view-panel')).to_be_visible()
            expect(page.locator('#progress-visualize-tab')).to_have_attribute('aria-selected','true')
            expect(page.locator('#visual-step-number')).to_have_text('Step 2 of 2')
            expect(page.locator('#visual-step-title')).to_have_text('Run and score trials')
            expect(page.locator('#visual-step-description')).to_have_text('Run participant and evaluate evidence.')
            page.locator('#progress-visualize-tab').press('End')
            expect(page.locator('#phase-view-panel')).to_be_visible()
            expect(page.locator('#progress-phases-tab')).to_be_focused()
            page.locator('#progress-phases-tab').press('Home')
            expect(page.locator('#visualize-view-panel')).to_be_visible()
            expect(page.locator('#progress-visualize-tab')).to_be_focused()
            page.locator('#visual-step-open').click()
            expect(page.locator('#phase-view-panel')).to_be_visible()
            expect(page.locator('#phase-title')).to_have_text('Run and score trials')
            page.get_by_role('tab',name='Visualize',exact=True).click()
            # The compact card follows all nine coordinator stages, not selected trial/node state.
            original_progress=current['workflow_progress'];original_status=current['recorded_status']
            ids=['preflight','prepare','deploy','fetch','readiness','reproduction','freeze','import','evaluate']
            nine=[dict(id=key,label='Stage '+str(i+1),description='Description '+str(i+1),status='completed' if i<2 else 'pending',active=False,events=[]) for i,key in enumerate(ids)]
            nine[2].update(status='running',active=True,operation='Starting containers <img src=x onerror=window.stepInjected=true>')
            current['workflow_progress']={**original_progress,'steps':nine,'current':nine[2],'current_step':'deploy','total_steps':9}
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-step-number')).to_have_text('Step 3 of 9')
            expect(page.locator('#visual-step-description')).to_have_text('Description 3')
            expect(page.locator('#visual-step-operation')).to_contain_text('Starting containers')
            assert page.evaluate('window.stepInjected') is None
            # The step card is independently collapsible; updates do not reopen it.
            page.locator('#visual-step-card>summary').click()
            nine[2].update(status='completed',active=False);nine[8].update(status='running',active=True)
            current['workflow_progress'].update(current=nine[8],current_step='evaluate')
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-step-card')).not_to_have_attribute('open','')
            page.locator('#visual-step-card>summary').click()
            expect(page.locator('#visual-step-number')).to_have_text('Step 9 of 9')
            page.locator('#visual-trial').select_option('trial-000002|task|1')
            expect(page.locator('#visual-step-number')).to_have_text('Step 9 of 9')
            page.locator('#visual-trial').select_option('trial-000001|task|1')
            nine[8].update(status='failed',active=False,error='Worker stopped')
            current['recorded_status']='failed'
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-step-status')).to_have_text('Failed')
            expect(page.locator('#visual-step-operation')).to_have_text('Worker stopped')
            for step in nine:step.update(status='pending',active=False)
            current['workflow_progress'].update(current=None,current_step=None);current['recorded_status']='ready'
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-step-number')).to_have_text('Not started · 9 steps')
            expect(page.locator('#visual-step-open')).to_be_disabled()
            current['workflow_progress']=original_progress;current['recorded_status']=original_status
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-step-number')).to_have_text('Step 2 of 2')
            expect(page.locator("#phase-view-panel")).not_to_be_visible()
            expect(page.locator("#attack-graph .attack-node")).to_have_count(4)
            expect(page.locator("#attack-graph .attack-edge")).to_have_count(3)
            expect(page.locator('#attack-graph .preparation-node')).to_have_count(0)
            expect(page.locator('#preparation-graph .attack-node')).to_have_count(0)
            prep_canvas=page.locator('#preparation-graph');execution=page.locator('#attack-graph')
            prep_box=page.locator('#preparation-graph-wrap').bounding_box();main_box=page.locator('#attack-graph-wrap').bounding_box()
            assert abs(prep_box['y']-main_box['y'])<1
            assert 0<=main_box['x']-prep_box['x']-prep_box['width']<=5
            prep_nodes=page.locator('#preparation-graph .preparation-node').all()
            assert max(n.bounding_box()['x'] for n in prep_nodes)-min(n.bounding_box()['x'] for n in prep_nodes)<1
            assert all(prep_nodes[n].bounding_box()['y']<prep_nodes[n+1].bounding_box()['y'] for n in range(len(prep_nodes)-1))
            # The initial zoom includes every challenge and the full vertical activity column.
            page.wait_for_function('''()=>{const svg=document.getElementById('attack-graph'),frame=svg.getBoundingClientRect();return frame.height>150&&[...svg.querySelectorAll('.attack-node,.flow-card')].every(n=>{const b=n.getBoundingClientRect();return b.x>=frame.x-1&&b.y>=frame.y-1&&b.right<=frame.right+1&&b.bottom<=frame.bottom+1;});}''')
            frame=execution.bounding_box()
            for card in page.locator('#attack-graph .attack-node,#attack-graph .flow-card').all():
                box=card.bounding_box()
                assert box['x']>=frame['x']-1 and box['y']>=frame['y']-1
                assert box['x']+box['width']<=frame['x']+frame['width']+1
                assert box['y']+box['height']<=frame['y']+frame['height']+1
            actor=page.locator('.flow-actor').bounding_box();action_box=page.locator('.flow-action').bounding_box()
            assert abs(actor['x']-action_box['x'])<1 and actor['y']+actor['height']<action_box['y']
            assert min(n.bounding_box()['x'] for n in page.locator('.attack-node').all())>max(n.bounding_box()['x']+n.bounding_box()['width'] for n in page.locator('.flow-card').all())
            # Preparation and execution views are independent.
            overview=execution.get_attribute('viewBox');prep_overview=prep_canvas.get_attribute('viewBox')
            page.locator('#prep-zoom-in').click()
            assert prep_canvas.get_attribute('viewBox')!=prep_overview
            assert execution.get_attribute('viewBox')==overview
            zoomed_prep=prep_canvas.get_attribute('viewBox')
            page.locator('#graph-zoom-in').click()
            assert prep_canvas.get_attribute('viewBox')==zoomed_prep
            page.locator('#graph-fit').click();page.locator('#graph-preparation').click()
            # Dock dividers support mouse and keyboard; adjacent panes use the recovered space.
            sidebar=page.locator('#editor-sidebar-sash');sash=sidebar.bounding_box()
            before_width=page.locator('#preparation-graph-wrap').bounding_box()['width']
            page.mouse.move(sash['x']+2,sash['y']+80);page.mouse.down();page.mouse.move(sash['x']+62,sash['y']+80,steps=4);page.mouse.up()
            assert page.locator('#preparation-graph-wrap').bounding_box()['width']>=before_width+55
            sidebar.focus();sidebar.press('ArrowRight')
            assert int(sidebar.get_attribute('aria-valuenow'))>=before_width+70
            sidebar.dblclick()
            panel_sash=page.locator('#editor-panel-sash');old_panel=page.locator('.visual-inspector').bounding_box()['height']
            panel_sash.focus();panel_sash.press('ArrowUp')
            assert page.locator('.visual-inspector').bounding_box()['height']>=old_panel+15
            panel_sash.dblclick()
            main_width=page.locator('#attack-graph-wrap').bounding_box()['width']
            page.locator('#preparation-graph-wrap [data-editor-toggle=preparation]').click()
            expect(prep_canvas).not_to_be_visible()
            assert page.locator('#attack-graph-wrap').bounding_box()['width']>main_width+150
            page.locator('.editor-activity-rail [data-editor-toggle=preparation]').click()
            expect(prep_canvas).to_be_visible()
            page.locator('#attack-graph-wrap [data-editor-toggle=execution]').click()
            expect(execution).not_to_be_visible()
            page.locator('.editor-activity-rail [data-editor-toggle=execution]').click()
            expect(execution).to_be_visible()
            page.locator('.visual-inspector [data-editor-toggle=panel]').click()
            expect(page.locator('#visual-inspector-body')).not_to_be_visible()
            page.locator('[data-node-id=s2]').focus();page.locator('[data-node-id=s2]').press('Enter')
            expect(page.locator('#visual-inspector-body')).to_be_visible()
            page.get_by_role('button',name='Collapse agent activity',exact=True).click()
            expect(page.locator('.flow-card')).to_have_count(0)
            expect(page.locator('.attack-node')).to_have_count(4)
            page.get_by_role('button',name='Expand agent activity',exact=True).click()
            fold=page.get_by_role('button',name='Collapse challenges',exact=True);fold.focus();fold.press('Enter')
            expect(page.locator('.attack-node')).to_have_count(0)
            expect(page.get_by_role('button',name='Expand challenges',exact=True)).to_be_focused()
            page.get_by_role('button',name='Expand challenges',exact=True).press('Enter')
            expect(page.locator('.attack-node')).to_have_count(4)
            # Smaller run status/legend sections are native collapsible disclosures.
            page.locator('#visual-live-activity>summary').click()
            expect(page.locator('#visual-live-activity')).not_to_have_attribute('open','')
            page.locator('#visual-live-activity>summary').click()
            page.locator('.editor-legend>summary').click();page.locator('.editor-legend>summary').click()


            expect(page.locator('[data-node-id=s2] .attack-assistance-badge.solution')).to_contain_text('1 hint · 1 solution unlocked')
            expect(page.locator('[data-node-id=s1] .attack-assistance-badge.none')).to_contain_text('No guidance released')
            expect(page.locator('#attack-graph .assistance-new')).to_have_count(0)
            expect(page.locator('#visual-hint-message')).to_have_text('Solution unlocked · turn 5')
            expect(
                page.locator("#preparation-graph .preparation-node.passed")
            ).to_have_count(3)
            expect(
                page.locator("#preparation-graph .preparation-edge.passed")
            ).to_have_count(3)
            expect(page.locator("#attack-graph .visual-lane-divider")).to_have_count(1)
            expect(page.locator('.activity-flow-lane')).to_have_count(1)
            expect(page.locator('.flow-action')).to_contain_text('nmap')
            expect(page.locator('.flow-target')).to_have_count(2)
            expect(page.locator('.activity-flow-arrow')).to_have_count(3)
            expect(page.locator('.activity-challenge-arrow')).to_have_count(1)
            expect(page.locator('.activity-challenge-arrow')).to_have_attribute('data-step-id','s2')
            expect(page.locator('.activity-context-arrow')).to_have_count(0)
            page.locator('.flow-actor').focus()
            page.locator('.flow-actor').press('Enter')
            expect(page.locator('#visual-node-detail')).to_contain_text('attempted actions')
            expect(page.locator('#visual-node-detail')).to_contain_text('live-events.jsonl')
            page.locator('#graph-focus').click()
            page.locator('.flow-target[data-address="192.0.2.8"]').click()
            expect(page.locator('#visual-node-detail h3')).to_have_text('192.0.2.8')
            page.locator("#graph-preparation").click()
            page.locator("[data-prep-kind=node][data-prep-id=participant]").click()
            expect(page.locator("#visual-node-detail")).to_contain_text("CAF runtime")
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "3 / 3 checks passed"
            )
            page.locator("#visual-node-detail").get_by_role(
                "button", name="Open phase details"
            ).first.click()
            expect(page.locator("#phase-title")).to_have_text("Check inputs")
            page.get_by_role("tab", name="Visualize", exact=True).click()
            page.locator("[data-prep-kind=edge][data-prep-id=hitl]").focus()
            page.locator("[data-prep-kind=edge][data-prep-id=hitl]").press("Enter")
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "does not prove end-to-end"
            )
            page.locator("#graph-challenges").click()
            page.locator("[data-node-id=s3]").click()
            for state in ["solved", "partial", "targeted", "other"]:
                expect(page.locator("#visual-count-" + state)).to_have_text("1")
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "Requested endpoint; access denied."
            )
            expect(page.locator("#visual-live-status")).to_have_text("Scanning · nmap")
            participant=page.locator('[data-prep-kind=node][data-prep-id=participant]')
            expect(participant).to_have_class('preparation-node passed live-interaction')
            expect(participant).to_have_attribute('aria-busy','true')
            assert participant.evaluate('(e)=>getComputedStyle(e).animationName')=='entity-working-blink'
            assert page.locator('[data-node-id=s2]').evaluate('(e)=>getComputedStyle(e).animationName')=='entity-working-blink'
            expect(participant.locator('.entity-working-label')).to_be_visible()
            expect(page.locator("[data-node-id=s2]")).to_have_class(
                "attack-node partial live-interaction live-target-matched"
            )
            expect(
                page.locator("#visual-live-targets .live-interaction")
            ).to_have_count(2)
            page.locator(
                '#visual-live-targets button[data-address="192.0.2.8"]'
            ).click()
            expect(page.locator("#visual-node-detail h3")).to_have_text("192.0.2.8")
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "No challenge association is verified yet"
            )
            page.locator('#visual-live-targets button[data-address="10.0.0.3"]').click()
            expect(page.locator("#visual-node-detail")).to_contain_text("scan via nmap")
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "live-events.jsonl"
            )
            page.get_by_role("button", name="Fit graph", exact=True).click()
            # Native keyboard access also works for SVG nodes.
            page.locator("[data-node-id=s2]").focus()
            page.locator("[data-node-id=s2]").press("Enter")
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "Service version found"
            )
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "1 / 2 criteria supported"
            )
            expect(page.locator("#visual-node-detail")).to_contain_text("events.jsonl")
            page.locator("#visual-node-detail").get_by_text(
                "Hint · turn 3", exact=True
            ).click()
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "Inspect the configuration endpoint."
            )
            expect(page.locator("#visual-assistance-count")).to_have_text(
                "1 hints · 1 solutions"
            )
            assert "PRIVATE_" not in page.locator("#visualize-view-panel").inner_text()
            page.screenshot(path="/tmp/caf-visual-monitor-desktop.png", full_page=True)
            canvas = page.locator("#attack-graph")
            canvas.scroll_into_view_if_needed()
            bounds = canvas.bounding_box()
            before_pan = canvas.get_attribute("viewBox")
            page.mouse.move(bounds["x"] + 25, bounds["y"] + 55)
            page.mouse.down()
            page.mouse.move(bounds["x"] + 60, bounds["y"] + 75, steps=4)
            page.mouse.up()
            assert canvas.get_attribute("viewBox") != before_pan
            expect(page.locator("[data-node-id=s2]")).to_have_attribute(
                "aria-pressed", "true"
            )
            page.get_by_role("button", name="Zoom in", exact=True).click()
            view = page.locator("#attack-graph").get_attribute("viewBox")
            page.clock.install()
            page.evaluate(
                "dispatchEvent(new StorageEvent('storage',{key:'caf-refresh-minutes',newValue:'0'}))"
            )
            visual["trials"][0]["monitor"]["checks"] = 2
            visual["trials"][0]["monitor"]["latest"]["turn"] = 5
            visual["trials"][0]["nodes"][1]["discoveries"].append(
                "<script>window.injected=true</script>"
            )
            page.clock.fast_forward(5000)
            expect(page.locator("#visual-monitor-status")).to_contain_text("2 checks")
            expect(page.locator("[data-node-id=s2]")).to_have_attribute(
                "aria-pressed", "true"
            )
            assert page.locator("#attack-graph").get_attribute("viewBox") == view, (view,page.locator("#attack-graph").get_attribute("viewBox"),page.locator("#attack-graph").bounding_box())
            assert page.evaluate("window.injected") is None
            # Trial switching retains each trial's own viewport.
            page.locator("#visual-trial").select_option("trial-000002|task|1")
            expect(page.locator("#attack-graph .attack-node")).to_have_count(4)
            page.get_by_role("button", name="Fit graph", exact=True).click()
            page.locator("#visual-trial").select_option("trial-000001|task|1")
            assert page.locator("#attack-graph").get_attribute("viewBox") == view, (view,page.locator("#attack-graph").get_attribute("viewBox"),page.locator("#attack-graph").bounding_box())
            page.get_by_role("button", name="Focus CAF", exact=True).click()
            assert page.locator("#attack-graph").get_attribute("viewBox") != view
            # Current component work remains visible between tool calls, and moves to Monitor during review.
            activity=visual['trials'][0]['activity']
            saved_activity=deepcopy(activity)
            activity.update(live=False,phase='reviewing',step_ids=[],expires_at=None)
            visual['trials'][0]['monitor']['latest']['status']='running'
            page.locator('#refresh-run').click()
            expect(page.locator('.visual-monitor-line')).to_have_class('visual-monitor-line is-working')
            expect(participant).to_have_attribute('aria-busy','false')
            expect(page.locator('.flow-action')).to_contain_text('LLM Monitor')
            expect(page.locator('.activity-flow-lane')).to_have_class('activity-flow-lane flow-reviewing')
            expect(page.locator('.flow-target')).to_have_count(0)
            expect(page.locator('#attack-graph .attack-node.live-interaction')).to_have_count(0)
            visual['trials'][0]['monitor']['latest']['status']='completed'
            activity.update(live=False,phase='idle')
            page.locator('#refresh-run').click()
            expect(participant).to_have_attribute('aria-busy','true')
            expect(page.locator('.visual-monitor-line')).to_have_class('visual-monitor-line')
            expect(page.locator('#attack-graph .attack-node.live-interaction')).to_have_count(0)
            # Stop animation if status polling itself stalls, even if a stale phase says running.
            page.evaluate('visualState.receivedAt=performance.now()-46000;syncLiveHighlights()')
            expect(participant).to_have_attribute('aria-busy','false')
            # Rearranging the editor must not turn an old cached snapshot into a fresh heartbeat.
            page.get_by_role('button',name='Collapse agent activity',exact=True).click()
            expect(participant).to_have_attribute('aria-busy','false')
            page.get_by_role('button',name='Expand agent activity',exact=True).click()
            expect(participant).to_have_attribute('aria-busy','false')
            activity.clear();activity.update(saved_activity)
            page.locator('#refresh-run').click()
            # Generic local tools, provider-emitted reasoning and model waits have separate action nodes.
            original_action=deepcopy(activity)
            activity.update(tool='shell_dangerous',agent_state='using_tool',kind='interaction',targets=[],step_ids=[],context_step_ids=[])
            page.locator('#refresh-run').click()
            expect(page.locator('.flow-generic')).to_contain_text('No literal IP')
            expect(page.locator('.activity-flow-arrow')).to_have_count(2)
            expect(page.locator('.activity-challenge-arrow')).to_have_count(0)
            for state,label in [('reasoning','Reasoning'),('waiting_model','Model response'),('responding','Responding')]:
                activity.update(tool=None,phase='thinking',agent_state=state,targets=[],step_ids=['s3'],context_step_ids=['s3'])
                page.locator('#refresh-run').click()
                expect(page.locator('.flow-action')).to_contain_text(label)
                expect(page.locator('.flow-thinking-icon')).to_have_count(2)
                expect(page.locator('.flow-target')).to_have_count(0)
                expect(page.locator('.activity-challenge-arrow')).to_have_count(0)
                expect(page.locator('.activity-context-arrow')).to_have_count(1)
                expect(page.locator('.activity-context-arrow')).to_have_attribute('marker-end','url(#flow-context-arrow)')
            activity.update(agent_state='reasoning')
            page.locator('#refresh-run').click()
            expect(page.locator('.flow-action')).to_contain_text('Reasoning')
            page.locator('.flow-actor').focus();page.locator('.flow-actor').press('Enter')
            expect(page.locator('#visual-node-detail')).to_contain_text('emitted a reasoning event')
            page.get_by_role('button',name='Focus CAF',exact=True).click()
            page.screenshot(path='/tmp/caf-thinking-flow.png',full_page=True)
            # All bounded targets remain visible, even when there is no challenge mapping.
            activity.update(tool='nmap',phase='tool',agent_state='using_tool',targets=[dict(address='2001:db8::'+str(i),step_ids=[],range=False) for i in range(16)],step_ids=[],context_step_ids=[])
            page.locator('#refresh-run').click()
            expect(page.locator('.flow-target')).to_have_count(16)
            expect(page.locator('.activity-challenge-arrow')).to_have_count(0)
            activity.clear();activity.update(original_action)
            page.locator('#refresh-run').click()
            page.get_by_role('button',name='Focus CAF',exact=True).click()
            page.screenshot(path='/tmp/caf-tool-flow.png',full_page=True)
            # Delivered assistance unlocks a colored badge; it never solves a challenge.
            live_trial=visual["trials"][0]
            hinted=live_trial["nodes"][2]
            release=dict(id="hint-live",step_id="s3",node_id="n3",turn=6,at=page.evaluate("new Date().toISOString()"),solution=False,text="Try the alternate endpoint <img src=x onerror=window.badHint=true>.",truncated=False)
            live_trial["releases"].append(release)
            hinted["releases"].append(release)
            hinted["hints"]=1
            before_hint_view=page.locator('#attack-graph').get_attribute('viewBox')
            page.locator('#refresh-run').click()
            expect(page.locator('[data-node-id=s3] .attack-assistance-badge.hint')).to_contain_text('1 hint unlocked')
            expect(page.locator('[data-node-id=s3]')).to_have_class('attack-node targeted monitor-focus assistance-new')
            expect(page.locator('#visual-hint-unlock')).to_have_class('visual-hint-unlock new-release')
            expect(page.locator('#visual-hint-message')).to_have_text('Hint unlocked · turn 6')
            expect(page.locator('#visual-hint-context')).to_contain_text('Inspect vulnerable endpoint')
            expect(page.locator('#visual-assistance .hint.new-release')).to_have_count(1)
            assert page.locator('#attack-graph').get_attribute('viewBox')==before_hint_view
            page.emulate_media(reduced_motion='reduce')
            assert page.locator('[data-node-id=s3] .hint-release-halo').evaluate('(e)=>getComputedStyle(e).animationName')=='none'
            page.emulate_media(reduced_motion='no-preference')
            page.locator('#visual-hint-open').click()
            expect(page.locator('#visual-node-detail .visual-hint[open]')).to_contain_text('alternate endpoint')
            expect(page.locator('#visual-node-detail')).to_contain_text('Prerequisites have not been verified complete')
            assert page.evaluate('window.badHint') is None
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-node-detail .visual-hint[open]')).to_have_count(1)
            page.clock.fast_forward(9000)
            expect(page.locator('#attack-graph .assistance-new')).to_have_count(0)
            page.locator('#refresh-run').click()
            expect(page.locator('#attack-graph .assistance-new')).to_have_count(0)
            expect(page.locator('[data-node-id=s3] .attack-assistance-badge.hint')).to_contain_text('1 hint unlocked')
            # A released solution has a distinct violet indicator and delivered text.
            solution=dict(id='solution-live',step_id='s3',node_id='n3',turn=7,at=page.evaluate('new Date().toISOString()'),solution=True,text='Released walkthrough and answer for this challenge.',truncated=False)
            live_trial['releases'].append(solution)
            hinted['releases'].append(solution)
            hinted['solutions']=1
            page.locator('#refresh-run').click()
            expect(page.locator('[data-node-id=s3]')).to_have_class('attack-node targeted monitor-focus assistance-new assistance-new-solution')
            expect(page.locator('#visual-hint-message')).to_have_text('Solution unlocked · turn 7')
            page.locator('#visual-hint-open').click()
            expect(page.locator('#visual-node-detail .solution[open]')).to_contain_text('walkthrough and answer')
            page.get_by_role('button',name='Fit graph',exact=True).click()
            page.screenshot(path='/tmp/caf-hint-unlock-desktop.png',full_page=True)
            # Even a cached repeated status response cannot renew the same activity lease forever.
            page.clock.fast_forward(46000)
            expect(
                page.locator("#attack-graph .attack-node.live-interaction")
            ).to_have_count(0)
            expect(
                page.locator("#visual-live-targets .live-interaction")
            ).to_have_count(0)
            expect(page.locator("#visual-live-status")).to_have_text(
                "Live activity signal expired"
            )
            expect(page.locator(".activity-flow-lane.flow-live")).to_have_count(0)
            action = visual["trials"][0]["activity"]
            action.update(
                observed_at=(activity_now + timedelta(seconds=60)).isoformat(),
                expires_at=(activity_now + timedelta(seconds=100)).isoformat(),
            )
            page.locator("#refresh-run").click()
            expect(
                page.locator("#attack-graph .attack-node.live-interaction")
            ).to_have_count(1)
            page.emulate_media(reduced_motion="reduce")
            assert (
                page.locator(".attack-node.live-interaction .live-node-halo").evaluate(
                    "(element)=>getComputedStyle(element).animationName"
                )
                == "none"
            )
            assert participant.evaluate('(e)=>getComputedStyle(e).animationName')=='none'
            assert page.locator('.activity-flow-arrow').first.evaluate('(e)=>getComputedStyle(e).animationName')=='none'
            assert page.locator('[data-node-id=s2]').evaluate('(e)=>getComputedStyle(e).animationName')=='none'
            page.emulate_media(reduced_motion="no-preference")
            page.get_by_role("button", name="Fit graph", exact=True).click()
            page.screenshot(path="/tmp/caf-live-targets-desktop.png", full_page=True)
            current.update(recorded_status="completed", coordinator_active=True)
            current["sample_progress"]["active"] = False
            action.update(live=False, phase="finished", expires_at=None)
            late_hint=dict(id='late',step_id=None,node_id=None,turn=8,at=page.evaluate('new Date().toISOString()'),solution=False,text='Task-wide guidance collected at the end.',truncated=False)
            live_trial['releases'].append(late_hint)
            page.locator("#refresh-run").click()
            expect(page.locator('#visual-hint-context')).to_contain_text('Task-wide guidance')
            expect(page.locator('#visual-hint-unlock.new-release')).to_have_count(0)
            expect(page.locator("#attack-graph .live-interaction")).to_have_count(0)
            expect(page.locator(".activity-flow-lane.flow-live")).to_have_count(0)
            expect(page.locator("#attack-graph .assistance-new")).to_have_count(0)
            expect(page.locator("#visual-live-status")).to_have_text(
                "Recorded interactions · run finished"
            )
            current.update(recorded_status="evaluating", coordinator_active=True)
            current["sample_progress"]["active"] = True
            action.update(
                live=True,
                phase="tool",
                expires_at=(activity_now + timedelta(seconds=101)).isoformat(),
            )
            page.locator("#refresh-run").click()
            expect(page.locator('#attack-graph .assistance-new')).to_have_count(0)
            # Deployment highlights both the app and CORE; recorded passed checks stay green.
            steps[1].update(id='deploy',vmid=9402)
            page.locator('#refresh-run').click()
            for role in ['scenarioforge','core']:
                expect(page.locator('[data-prep-kind=node][data-prep-id='+role+']')).to_have_class('preparation-node passed live-interaction')
            steps[1].update(id='evaluate',vmid=9403)
            page.locator('#refresh-run').click()
            # Prep-only status changes update the canvas without moving its viewport.
            saved_view = page.locator("#attack-graph").get_attribute("viewBox")
            prep_steps[0]["input_checks"][3].update(
                status="fail", message="SSH handshake refused"
            )
            current["workflow_progress"]["preparation"] = preparation_build(
                prep_steps, prep_roles
            )
            page.locator("#refresh-run").click()
            expect(
                page.locator("[data-prep-kind=node][data-prep-id=core]")
            ).to_have_class("preparation-node failed")
            assert page.locator("#attack-graph").get_attribute("viewBox") == saved_view
            page.locator("[data-prep-kind=edge][data-prep-id=core-connection]").focus()
            page.locator("[data-prep-kind=edge][data-prep-id=core-connection]").press(
                "Enter"
            )
            expect(page.locator("#visual-node-detail")).to_contain_text(
                "SSH handshake refused"
            )
            prep_steps[0]["input_checks"][3].update(
                status="pass", message="Recorded check verified"
            )
            current["workflow_progress"]["preparation"] = preparation_build(
                prep_steps, prep_roles
            )
            page.locator("#refresh-run").click()
            expect(
                page.locator("#preparation-graph .preparation-node.passed")
            ).to_have_count(3)
            # Only the recorded running check blinks in selected block/connection details.
            connection_check=prep_steps[0]['input_checks'][3]
            connection_check.update(status='running',message='Connecting to CORE now')
            current['workflow_progress']['preparation']=preparation_build(prep_steps,prep_roles)
            page.locator('#refresh-run').click()
            expect(page.locator('[data-prep-kind=node][data-prep-id=core]')).to_have_class('preparation-node running live-interaction')
            page.locator('[data-prep-kind=node][data-prep-id=core]').focus()
            page.locator('[data-prep-kind=node][data-prep-id=core]').press('Enter')
            expect(page.locator('#visual-node-detail h3')).to_have_text('CORE')
            running_check=page.locator('#visual-node-detail .prep-check-detail[data-check-id="core-connection"]')
            expect(page.locator('#visual-node-detail .prep-check-detail.is-active')).to_have_count(1)
            expect(running_check).to_have_attribute('aria-busy','true')
            expect(running_check.locator('.prep-check-active-marker')).to_be_visible()
            assert running_check.locator('h4').evaluate('(node)=>getComputedStyle(node).animationName')=='live-target'
            assert page.locator('#visual-node-detail .prep-check-detail:not(.is-active) h4').first.evaluate('(node)=>getComputedStyle(node).animationName')=='none'
            page.emulate_media(reduced_motion='reduce')
            assert running_check.locator('h4').evaluate('(node)=>getComputedStyle(node).animationName')=='none'
            expect(running_check.locator('.prep-check-active-marker')).to_be_visible()
            page.emulate_media(reduced_motion='no-preference')
            page.screenshot(path='/tmp/caf-active-preparation-check.png',full_page=True)
            page.locator('#visual-node-detail').screenshot(path='/tmp/caf-active-preparation-check-detail.png')
            # Check disclosures preserve their state while live progress updates arrive.
            running_check.locator('summary').click()
            expect(running_check).not_to_have_attribute('open','')
            page.locator('#refresh-run').click()
            expect(running_check).not_to_have_attribute('open','')
            running_check.locator('summary').click()
            # Connection details use the same signal, without needing to reopen the selection.
            page.locator('[data-prep-kind=edge][data-prep-id=core-connection]').focus()
            page.locator('[data-prep-kind=edge][data-prep-id=core-connection]').press('Enter')
            expect(running_check).to_have_attribute('aria-busy','true')
            page.evaluate('visualState.receivedAt=performance.now()-46000;syncLiveHighlights()')
            expect(page.locator('#visual-node-detail .prep-check-detail.is-active')).to_have_count(0)
            page.locator('#refresh-run').click()
            expect(running_check).to_have_attribute('aria-busy','true')
            current.update(recorded_status='completed',coordinator_active=True)
            page.locator('#refresh-run').click()
            expect(page.locator('#visual-node-detail .prep-check-detail.is-active')).to_have_count(0)
            current.update(recorded_status='evaluating',coordinator_active=True)
            connection_check.update(status='pass',message='Recorded check verified')
            current['workflow_progress']['preparation']=preparation_build(prep_steps,prep_roles)
            page.locator('#refresh-run').click()
            expect(running_check).to_have_attribute('aria-busy','false')
            expect(running_check.locator('.prep-check-active-marker')).to_be_hidden()
            page.get_by_role("button", name="Fit preparation", exact=True).click()
            page.get_by_role("button", name="Fit graph", exact=True).click()
            page.screenshot(
                path="/tmp/caf-visual-preparation-desktop.png", full_page=True
            )
            page.evaluate("setProgressPhase('evaluate',{follow:true})")
            # Both views remain available and the saved mode survives a reload.
            page.locator('#preparation-graph-wrap [data-editor-toggle=preparation]').click()
            page.get_by_role('button',name='Collapse agent activity',exact=True).click()
            page.reload()
            expect(page.locator('#preparation-graph')).not_to_be_visible()
            expect(page.locator('.flow-card')).to_have_count(0)
            page.locator('.editor-activity-rail [data-editor-toggle=preparation]').click()
            page.get_by_role('button',name='Expand agent activity',exact=True).click()
            expect(
                page.get_by_role("tab", name="Visualize", exact=True)
            ).to_have_attribute("aria-selected", "true")
            expect(page.locator('#attack-graph .assistance-new')).to_have_count(0)
            expect(page.locator('[data-node-id=s3] .attack-assistance-badge.solution')).to_contain_text('1 hint · 1 solution unlocked')
            page.get_by_role("tab", name="Phase progress", exact=True).click()
            expect(page.locator("#phase-title")).to_have_text("Run and score trials")
            page.get_by_role("tab", name="Visualize", exact=True).click()
            page.set_viewport_size({"width": 430, "height": 900})
            page.get_by_role("button", name="Fit graph", exact=True).click()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path="/tmp/caf-visual-monitor-mobile.png", full_page=True)
            # An empty snapshot clears old evidence instead of displaying a stale graph.
            current["sample_progress"]["visualization"]["trials"] = []
            page.locator("#refresh-run").click()
            expect(page.locator("#attack-graph .attack-node")).to_have_count(0)
            expect(page.locator("#visual-hint-unlock")).to_be_hidden()
            expect(page.locator("#preparation-graph .preparation-node")).to_have_count(3)
            expect(page.locator("#visual-node-detail")).not_to_contain_text(
                "Service version found"
            )
            current["workflow_progress"]["preparation"] = preparation_build(
                [], prep_roles
            )
            page.locator("#refresh-run").click()
            expect(
                page.locator("#preparation-graph .preparation-node.pending")
            ).to_have_count(3)
            page.screenshot(
                path="/tmp/caf-visual-preparation-before-trials.png", full_page=True
            )
            expect(page.locator("#visual-count-solved")).to_have_text("0")
            assert len(reads) >= 4 and not errors, errors
            browser.close()
    print(
        "PASS: Visualize-first/default and phase links, live current-step card (nine stages/failure/queued), editor adjacency/vertical preparation, independent zoom, resizing, collapsible panes/groups/checks and saved layout, graph states/edges, live hint/solution unlocks, expiry and no replay, released text and private-data boundaries, keyboard selection, stable zoom, trial switching, reduced motion, mobile and empty snapshots"
    )


if __name__ == "__main__":
    main()
