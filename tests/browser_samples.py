"""Browser sample launch/results checks; guest/model calls are simulated."""
from pathlib import Path
import shutil
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
                    smoke.get_by_role('button', name='View results', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('no-tools')
                    expect(page.locator('#result-summary')).to_contain_text('1 / 1')
                    expect(page.get_by_role('dialog', name='Run results')).to_be_visible()
                    assert page.locator('#result-panel').evaluate('(n)=>n.matches(":modal")')
                    page.keyboard.press('Escape')
                    expect(page.locator('#result-panel')).to_be_hidden()
                    helper=create('tools-vs-helper','Tools vs. added helper')
                    helper.get_by_role('button', name='Run experiment', exact=True).click()
                    row=helper
                    expect(page.locator('#sample-activity')).to_contain_text('2 / 6 trials finished · 33%', timeout=30000)
                    expect(page.locator('#sample-activity')).to_contain_text('Guest worker executing')
                    expect(page.locator('#sample-activity')).to_contain_text('PID 123')
                    expect(page.locator('#sample-activity')).to_contain_text('7 / 7 files')
                    expect(page.locator('#console-output')).to_contain_text('trial-000003')
                    expect(smoke.get_by_role('button', name='Run again', exact=True)).to_be_disabled()
                    expect(helper.get_by_role('button', name='Run again', exact=True)).to_be_disabled()
                    page.locator('#close-progress').click()
                    expect(page.locator('#sample-activity-panel')).to_be_hidden()
                    helper.get_by_role('button', name='Open progress', exact=True).click()
                    page.locator('[data-route=overview]').click()
                    expect(page.locator('#sample-global-status')).to_contain_text('2/6 trials finished')
                    page.locator('#sample-global-status a').click()
                    page.locator('#sample-activity').scroll_into_view_if_needed()
                    page.screenshot(path=str(destination / 'sample-active.png'))
                    helper.get_by_role('button', name='View results', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('evaluating')
                    release.set()
                    expect(page.locator('#sample-activity')).to_contain_text('Cleaning up the sample environment', timeout=30000)
                    expect(page.locator('#sample-activity')).to_contain_text('6 / 6 trials finished · 100%')
                    expect(page.locator('#sample-global-status')).to_be_visible()
                    finish.set()
                    expect(row).to_contain_text('completed', timeout=30000)
                    expect(page.locator('#result-summary')).to_contain_text('completed', timeout=30000)
                    expect(page.locator('#result-summary')).not_to_contain_text('evaluating')
                    expect(page.locator('#sample-global-status')).to_be_hidden()
                    expect(row).to_contain_text('6 / 6')
                    page.get_by_role('button', name='Close results', exact=True).click()
                    page.locator('#close-progress').click()
                    page.evaluate('scrollTo(0, 0)')
                    page.screenshot(path=str(destination / 'experiment-table.png'))
                    row.get_by_role('button', name='View results', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('baseline')
                    expect(page.locator('#result-summary')).to_contain_text('added-helper')
                    expect(page.locator('#result-summary')).to_contain_text('3 / 3')
                    with page.expect_download() as download:
                        page.get_by_role('link', name='Download CSV').click()
                    download.value.save_as(destination / 'sample-dataset.csv')
                    assert 'added-helper' in (destination / 'sample-dataset.csv').read_text()
                    page.screenshot(path=str(destination / 'samples-desktop.png'), full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.reload()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#runs tr').filter(has_text='Tools vs. added helper').first.get_by_role('button', name='View results', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('added-helper')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(destination / 'samples-mobile.png'), full_page=True)
                    assert page.locator('#result-panel').evaluate('(n)=>n.getBoundingClientRect().width<=innerWidth && n.getBoundingClientRect().height<=innerHeight')
                    page.get_by_role('button', name='Close results', exact=True).click()
                    trials.clear();release.clear()
                    row=page.locator('#runs tr').filter(has_text='Tools vs. added helper').first
                    row.get_by_role('button', name='Run again', exact=True).click()
                    expect(page.locator('#sample-activity')).to_contain_text('2 / 6 trials finished', timeout=30000)
                    active=page.locator('#runs tr').filter(has_text='evaluating').first
                    active.get_by_role('button', name='Stop after current trial', exact=True).click()
                    expect(page.locator('#runs')).to_contain_text('stopping', timeout=30000)
                    release.set()
                    expect(page.locator('#runs')).to_contain_text('cancelled', timeout=30000)
                    cancelled=page.locator('#runs tr').filter(has_text='cancelled').first
                    expect(cancelled).to_contain_text('3 / 6')
                    cancelled.get_by_role('button', name='View results', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('cancelled')
                    page.get_by_role('button', name='Close results', exact=True).click()
                    release.clear();fail_next.set()
                    smoke=page.locator('#runs tr').filter(has_text='Model smoke test').first
                    smoke.get_by_role('button', name='Run again', exact=True).click()
                    expect(page.locator('#sample-activity')).to_contain_text('0 / 1 trials finished', timeout=30000)
                    running=page.locator('#runs tr').filter(has_text='Model smoke test').first
                    running.get_by_role('button', name='View results', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('evaluating', timeout=30000)
                    release.set()
                    expect(page.locator('#result-summary')).to_contain_text('completed_with_errors', timeout=30000)
                    expect(page.locator('#result-summary')).to_contain_text('Example model connection failure')
                    expect(page.locator('#result-summary')).to_contain_text('Provider unavailable; token=[redacted]')
                    page.get_by_text('Collected worker log', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('RuntimeError: example provider unavailable')
                    expect(page.locator('#result-summary')).not_to_contain_text('PRIVATE TEST PROMPT')
                    page.screenshot(path=str(destination / 'failure-diagnostics.png'))
                    expect(page.locator('#sample-activity')).to_contain_text('Example model connection failure')
                    assert any(op == 'sample_stop' for _, op, _ in calls)
                    assert not errors, errors
                    browser.close()
            finally:
                release.set(); finish.set()
                dashboard.close()
    print('PASS: both browser samples, scored results, CSV download, desktop/mobile layout and fixture cleanup')
    print(destination)


if __name__ == '__main__':
    main()
