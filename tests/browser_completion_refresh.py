"""Never suppresses page refreshes while completion still updates once."""
from copy import deepcopy
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
        root=Path(temp)
        with pve_server(root) as pve:
            dashboard=Dashboard()
            active=dict(output='/saved/demo', workflow_id='demo', recorded_status='evaluating',
                        coordinator_active=True, message='Agent is working',
                        evaluation={'planned_trials':1,'summary':{'trials_observed':0,'verified_successes':0}})
            current=deepcopy(active)
            dashboard.value['runs']=[active]
            with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                context=browser.new_context(ignore_https_errors=True)
                page=context.new_page()
                errors, dashboard_reads, run_reads=[],[],[]
                context.on('page',lambda tab:tab.on('pageerror',lambda e:errors.append(str(e))))
                page.on('pageerror',lambda e:errors.append(str(e)))
                context.on('request',lambda r:dashboard_reads.append(r.url) if '/api/status' in r.url else None)
                def status(route):
                    run_reads.append(route.request.url)
                    route.fulfill(json=current)
                context.route('**/api/runs/demo/status',status)
                context.route('**/api/runs/demo/results',lambda route:route.fulfill(json={'workflow':current,'evaluation':None,'run_configuration':None}))
                page.goto(server['origin'])
                page.get_by_label('Username').fill('operator@pve')
                page.get_by_label('Password',exact=True).fill(PASSWORD)
                page.get_by_role('button',name='Sign in',exact=True).click()
                expect(page.locator('#refresh')).to_be_enabled()
                page.locator('[data-route=setup]').click()
                page.get_by_label('Automatic refresh',exact=True).select_option('0')
                page.clock.install()
                # Reschedule under the controlled clock.
                page.locator('#refresh-period').dispatch_event('change')
                page.locator('[data-route=experiments]').click()
                row=page.locator('[data-run-id=demo]')
                expect(row).to_contain_text('evaluating')
                row.evaluate("(node)=>node.dataset.preserved='yes'")
                pages=[page]
                for view in ('progress','results'):
                    tab=context.new_page()
                    tab.goto(server['origin']+'/run?view='+view+'&run=demo')
                    expect(tab.locator('#run-status')).to_contain_text('Automatic refresh off')
                    expect(tab.locator('#loading-modal')).not_to_be_visible()
                    tab.clock.install()
                    tab.evaluate("dispatchEvent(new StorageEvent('storage',{key:'caf-refresh-minutes',newValue:'0'}))")
                    pages.append(tab)
                for tab in pages:
                    tab.evaluate("""() => {window.loadingShows=0;new MutationObserver(()=>{if(document.getElementById('loading-modal').open)window.loadingShows++;}).observe(document.getElementById('loading-modal'),{attributes:true,attributeFilter:['open']});}""")
                before=len(dashboard_reads)
                for tab in pages:
                    tab.clock.fast_forward(5000)
                    tab.wait_for_timeout(100)
                    expect(tab.locator('#loading-modal')).not_to_be_visible()
                assert len(run_reads)>=5
                assert len(dashboard_reads)==before
                expect(row).to_have_attribute('data-preserved','yes')
                current.update(recorded_status='completed',coordinator_active=False,message='Done')
                for tab in pages:
                    tab.clock.fast_forward(5000)
                    tab.wait_for_timeout(100)
                expect(row).to_contain_text('completed')
                for tab in pages[1:]:
                    expect(tab.locator('#run-status')).to_contain_text('completed')
                for tab in pages:
                    assert tab.evaluate('window.loadingShows')==0
                before_runs=len(run_reads)
                for tab in pages:
                    tab.clock.fast_forward(601000)
                    tab.wait_for_timeout(100)
                assert len(run_reads)==before_runs
                assert len(dashboard_reads)==before
                # Manual refresh remains usable.
                pages[1].locator('#refresh-run').click()
                expect(pages[1].locator('#refresh-run')).to_be_enabled()
                assert len(run_reads)==before_runs+1
                assert not errors,errors
                browser.close()
    print('PASS: Never prevents page refreshes; silent completion updates dashboard, progress and results once')


if __name__=='__main__':
    main()
