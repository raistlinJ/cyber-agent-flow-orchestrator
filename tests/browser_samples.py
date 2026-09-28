"""Browser sample launch/results checks; guest/model calls are simulated."""
from pathlib import Path
import shutil
import sys
import tempfile

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
        calls, _, _ = install_backend(patch, root)
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
                    page.wait_for_selector('#samples-panel:not([hidden])')
                    smoke = page.locator('[data-sample-id="smoke"]')
                    expect(smoke).to_be_disabled()
                    page.locator('#role-participant').select_option('9403')
                    page.get_by_role('button', name='Save VM roles').click()
                    expect(smoke).to_be_enabled(timeout=30000)
                    smoke.click()
                    expect(page.locator('#runs')).to_contain_text('completed', timeout=30000)
                    page.locator('#runs').get_by_role('button', name='sample-smoke', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('no-tools')
                    expect(page.locator('#result-summary')).to_contain_text('1 / 1')
                    helper = page.locator('[data-sample-id="tools-vs-helper"]')
                    expect(helper).to_be_enabled(timeout=30000)
                    helper.click()
                    row = page.locator('#runs tr').filter(has_text='sample-tools-vs-helper')
                    expect(row).to_contain_text('completed', timeout=30000)
                    expect(row).to_contain_text('6 / 6')
                    row.get_by_role('button').click()
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
                    page.wait_for_selector('#samples-panel:not([hidden])')
                    page.locator('#runs').get_by_role('button', name='sample-tools-vs-helper', exact=True).click()
                    expect(page.locator('#result-summary')).to_contain_text('added-helper')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(destination / 'samples-mobile.png'), full_page=True)
                    assert any(op == 'sample_stop' for _, op, _ in calls)
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print('PASS: both browser samples, scored results, CSV download, desktop/mobile layout and fixture cleanup')
    print(destination)


if __name__ == '__main__':
    main()
