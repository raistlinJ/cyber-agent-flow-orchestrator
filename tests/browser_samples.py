"""Browser sample launch/results checks; guest/model calls are simulated."""
from pathlib import Path
import shutil
import re
import sys
import tempfile
import threading

import pytest
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm
from sample_fixture import install_backend


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-sample-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-sample-browser-') as temp, pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        calls, _, backend = install_backend(patch, root)
        from cyber_agent_flow_orchestrator import samples
        from cyber_agent_flow_eval import integration as ev
        release, finish, fail_next = threading.Event(), threading.Event(), threading.Event()
        launch, cleanup = backend.launch, samples.stop_fixture
        trials = []
        def paused_trial(self, directory, seconds):
            if self.spec['id'] == 'sample-smoke' and fail_next.is_set():
                assert release.wait(90)
                guest = directory / 'guest-output'
                guest.mkdir()
                (guest / 'worker.log').write_text('Traceback:\nRuntimeError: example provider unavailable\n')
                ev.write_json(guest / 'model_calls/call-000001.json', dict(
                    request={'prompt':'PRIVATE TEST PROMPT'}, error='Provider unavailable; token=private-test-token'))
                return {'status':'error', 'errors':['Example model connection failure'], 'execution_seconds':1}
            if self.spec['id'] == 'sample-tools-vs-helper':
                trials.append(directory)
                if len(trials) == 3:
                    ev.write_json(directory / 'transport.json', dict(phase='executing', updated_at=samples.now(),
                        vmid=9403, unit='caf-sample-test.service',
                        files_uploaded=7, files_total=7, bytes_uploaded=1000, bytes_total=1000,
                        service_status={'SubState':'running','ExecMainPID':'123'}))
                    assert release.wait(90)
            result = launch(self, directory, seconds)
            transport = directory / 'transport.json'
            if transport.is_file():
                ev.write_json(transport, dict(ev.read_json(transport), stopped=True, collected=True, phase='collected'))
            return result
        def paused_cleanup(output, journal):
            if journal['sample_id'] == 'tools-vs-helper': assert finish.wait(90)
            cleanup(output, journal)
        patch.setattr(backend, 'launch', paused_trial)
        patch.setattr(samples, 'stop_fixture', paused_cleanup)
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve'] = [vm(9403, pool='operator-lab')]
            dashboard = UserDashboard(root / 'examples/01-reuse-export.yaml', root / 'runs', 2,
                                      lambda b, a: Probe(b, a, []))
            try:
                with secure_server(dashboard, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='chrome', headless=True)
                    context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1080})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password', exact=True).fill(PASSWORD)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.locator('[data-route=experiments]').click()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#new-experiment').click()
                    expect(page.locator('#experiment-dialog')).to_be_visible()
                    page.screenshot(path=str(destination / 'new-experiment.png'))
                    page.get_by_role('button', name='Close new experiment').click()
                    expect(page.locator('#runs tr')).to_have_count(0)
                    def create(sample_id, name):
                        page.locator('#new-experiment').click()
                        page.get_by_label('Sample', exact=True).select_option(sample_id)
                        page.get_by_role('button', name='Create experiment', exact=True).click()
                        expect(page.locator('#experiment-dialog')).to_be_hidden(timeout=30000)
                        row=page.locator('#runs tr').filter(has_text=name).first
                        expect(row).to_contain_text('ready')
                        return row
                    smoke=create('smoke','Model smoke test')
                    expect(smoke.get_by_role('button', name='Run experiment', exact=True)).to_be_disabled()
                    page.locator('[data-route=setup]').click()
                    page.locator('#role-participant').select_option('9403')
                    page.get_by_role('button', name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click()
                    expect(smoke.get_by_role('button', name='Run experiment', exact=True)).to_be_enabled(timeout=30000)
                    smoke.get_by_role('button', name='Run experiment', exact=True).click()
                    expect(page.locator('#runs')).to_contain_text('completed', timeout=30000)
                    def popup(row, label):
                        page.bring_to_front()
                        with page.expect_popup() as opened:
                            row.get_by_role('button',name=label,exact=True).click()
                        window=opened.value
                        window.on('pageerror',lambda error:errors.append(str(error)))
                        expect(window).to_have_url(re.compile(r'/run\?view='))
                        expect(window.locator('dialog')).to_have_count(0)
                        return window
                    result=popup(smoke,'View results')
                    expect(result.locator('#result-summary')).to_contain_text('no-tools')
                    expect(result.locator('#result-summary')).to_contain_text('1 / 1')
                    expect(page.locator('#result-panel')).to_have_count(0)
                    expect(page.locator('#new-experiment')).to_be_enabled()
                    result.get_by_role('button',name='Close window').click()
                    assert result.is_closed()
                    helper=create('tools-vs-helper','Tools vs. added helper')
                    helper.get_by_role('button',name='Run experiment',exact=True).click()
                    expect(helper.get_by_role('button',name='Open progress')).to_be_enabled(timeout=30000)
                    progress=popup(helper,'Open progress')
                    expect(progress.locator('#sample-activity')).to_contain_text('2 / 6 trials finished · 33%',timeout=30000)
                    expect(progress.locator('#sample-activity')).to_contain_text('Guest worker executing')
                    expect(progress.locator('#sample-activity')).to_contain_text('PID 123')
                    expect(progress.locator('#sample-activity')).to_contain_text('7 / 7 files')
                    expect(progress.locator('#console-output')).to_contain_text('trial-000003')
                    expect(page.locator('#sample-activity-panel')).to_have_count(0)
                    expect(smoke.get_by_role('button',name='Run again',exact=True)).to_be_disabled()
                    page.bring_to_front()
                    page.locator('[data-route=overview]').click()
                    expect(page.locator('#sample-global-status')).to_contain_text('2/6 trials finished',timeout=30000)
                    page.locator('#sample-global-status a').click()
                    expect(progress.locator('#sample-activity')).to_contain_text('2 / 6 trials finished',timeout=30000)
                    progress.screenshot(path=str(destination/'sample-active.png'))
                    page.locator('[data-route=experiments]').click()
                    result=popup(helper,'View results')
                    expect(result.locator('#result-summary')).to_contain_text('evaluating')
                    release.set()
                    expect(progress.locator('#sample-activity')).to_contain_text('Cleaning up the sample environment',timeout=30000)
                    expect(progress.locator('#sample-activity')).to_contain_text('6 / 6 trials finished · 100%')
                    finish.set()
                    expect(helper).to_contain_text('completed',timeout=30000)
                    expect(result.locator('#result-summary')).to_contain_text('completed',timeout=30000)
                    expect(result.locator('#result-summary')).not_to_contain_text('evaluating')
                    expect(page.locator('#sample-global-status')).to_be_hidden()
                    expect(helper).to_contain_text('6 / 6')
                    expect(result.locator('#result-summary')).to_contain_text('baseline')
                    expect(result.locator('#result-summary')).to_contain_text('added-helper')
                    expect(result.locator('#result-summary')).to_contain_text('3 / 3')
                    with result.expect_download() as download:
                        result.get_by_role('link',name='Download CSV').click()
                    download.value.save_as(destination/'sample-dataset.csv')
                    assert 'added-helper' in (destination/'sample-dataset.csv').read_text()
                    result.screenshot(path=str(destination/'samples-desktop.png'),full_page=True)
                    result.set_viewport_size({'width':390,'height':844})
                    result.reload()
                    expect(result.locator('#result-summary')).to_contain_text('added-helper')
                    assert result.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    result.screenshot(path=str(destination/'samples-mobile.png'),full_page=True)
                    page.screenshot(path=str(destination/'experiment-table.png'))
                    result.close();progress.close()
                    # A blocked popup offers a normal new-tab link, without a modal fallback.
                    page.evaluate('()=>{window.originalOpen=window.open;window.open=()=>null;}')
                    helper.get_by_role('button',name='View results',exact=True).click()
                    expect(page.locator('#window-notice')).to_contain_text('blocked the popup')
                    page.evaluate('()=>{window.open=window.originalOpen;}')
                    with page.expect_popup() as fallback:
                        page.locator('#window-notice a').click()
                    fallback.value.wait_for_load_state()
                    expect(fallback.value.locator('#result-summary')).to_contain_text('added-helper')
                    fallback.value.close()
                    trials.clear();release.clear()
                    helper.get_by_role('button',name='Run again',exact=True).click()
                    active=page.locator('#runs tr').filter(has_text='evaluating').first
                    expect(active.get_by_role('button',name='Open progress')).to_be_enabled(timeout=30000)
                    progress=popup(active,'Open progress')
                    expect(progress.locator('#sample-activity')).to_contain_text('2 / 6 trials finished',timeout=30000)
                    active.get_by_role('button',name='Stop after current trial',exact=True).click()
                    expect(page.locator('#runs')).to_contain_text('stopping',timeout=30000)
                    release.set()
                    expect(page.locator('#runs')).to_contain_text('cancelled',timeout=30000)
                    cancelled=page.locator('#runs tr').filter(has_text='cancelled').first
                    expect(cancelled).to_contain_text('3 / 6')
                    result=popup(cancelled,'View results')
                    expect(result.locator('#result-summary')).to_contain_text('cancelled')
                    result.close();progress.close()
                    release.clear();fail_next.set()
                    smoke=page.locator('#runs tr').filter(has_text='Model smoke test').first
                    smoke.get_by_role('button',name='Run again',exact=True).click()
                    running=page.locator('#runs tr').filter(has_text='Model smoke test').first
                    expect(running.get_by_role('button',name='Open progress')).to_be_enabled(timeout=30000)
                    progress=popup(running,'Open progress')
                    expect(progress.locator('#sample-activity')).to_contain_text('0 / 1 trials finished',timeout=30000)
                    result=popup(running,'View results')
                    expect(result.locator('#result-summary')).to_contain_text('evaluating',timeout=30000)
                    release.set()
                    expect(result.locator('#result-summary')).to_contain_text('completed_with_errors',timeout=30000)
                    expect(result.locator('#result-summary')).to_contain_text('Example model connection failure')
                    expect(result.locator('#result-summary')).to_contain_text('Provider unavailable; token=[redacted]')
                    result.get_by_text('Collected worker log',exact=True).click()
                    expect(result.locator('#result-summary')).to_contain_text('RuntimeError: example provider unavailable')
                    expect(result.locator('#result-summary')).not_to_contain_text('PRIVATE TEST PROMPT')
                    result.screenshot(path=str(destination/'failure-diagnostics.png'))
                    expect(progress.locator('#sample-activity')).to_contain_text('Example model connection failure',timeout=30000)
                    # Closing the dashboard leaves the two run windows functional.
                    page.close()
                    result.get_by_role('button',name='Refresh',exact=True).click()
                    expect(result.locator('#run-status')).to_contain_text('completed_with_errors',timeout=30000)
                    result.route('**/api/runs/**/results',lambda route:route.fulfill(status=401,json={'error':'Login required'}))
                    result.get_by_role('button',name='Refresh',exact=True).click()
                    expect(result.locator('#run-error')).to_contain_text('Session expired')
                    expect(result.locator('#result-summary')).to_have_text('')
                    expect(result.locator('#result-content')).to_have_text('')
                    assert any(op == 'sample_stop' for _, op, _ in calls)
                    assert not errors, errors
                    browser.close()
            finally:
                release.set(); finish.set()
                dashboard.close()
                for future in dashboard.samples.jobs.values():
                    future.result(timeout=15)
    print('PASS: independent results/progress popups, live updates, blocked popup fallback, CSV, mobile, cancellation and failure diagnostics')
    print(destination)


if __name__ == '__main__':
    main()
