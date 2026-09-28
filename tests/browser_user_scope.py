"""Real browser check for PVE VM roles; simulated VMs, no live host commands."""
from pathlib import Path
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-user-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-user-browser-') as temp:
        root = Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve'] = [vm(9401, pool='alice-lab'), vm(9402, pool='alice-lab'), vm(9403, pool='alice-lab')]
            pve[0]['resources']['bob@pve'] = [vm(9501, pool='bob-lab'), vm(9502, pool='bob-lab')]
            calls = []
            dashboard = UserDashboard(root / 'examples/01-reuse-export.yaml', root / 'runs', 2,
                                      lambda b, a: Probe(b, a, calls))
            try:
                with secure_server(dashboard, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='chrome', headless=True)
                    first = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1080})
                    second = browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844})
                    errors = []
                    def login(context, username):
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(server['origin'])
                        page.get_by_label('Username').fill(username)
                        page.get_by_label('Password', exact=True).fill(PASSWORD)
                        page.get_by_role('button', name='Sign in', exact=True).click()
                        page.locator('[data-route=setup]').click()
                        page.wait_for_selector('#role-panel:not([hidden])')
                        return page
                    alice = login(first, 'operator@pve')
                    assert alice.locator('#role-scenarioforge').input_value() == ''
                    alice.locator('#role-scenarioforge').select_option('9402')
                    alice.locator('#role-participant').select_option('9403')
                    alice.locator('#role-core').select_option('9401')
                    alice.get_by_role('button', name='Save VM roles').click()
                    expect(alice.locator('#machines')).to_contain_text('VM 9402', timeout=30000)
                    alice.screenshot(path=str(destination / 'user-vms-desktop.png'), full_page=True)
                    bob = login(second, 'bob@pve')
                    assert '9402' not in bob.locator('#role-scenarioforge').inner_text()
                    assert bob.locator('#role-scenarioforge').input_value() == ''
                    assert bob.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    bob.screenshot(path=str(destination / 'user-vms-mobile.png'), full_page=True)
                    alice.reload()
                    alice.wait_for_selector('#role-panel:not([hidden])')
                    assert alice.locator('#role-scenarioforge').input_value() == '9402'
                    pve[0]['resources']['operator@pve'] = []
                    alice.locator('#refresh').click()
                    expect(alice.locator('#machines')).not_to_contain_text('VM 9402')
                    expect(alice.locator('#role-scenarioforge option')).to_have_count(1)
                    assert all(vmid in ({9401, 9402, 9403} if username == 'operator@pve' else {9501, 9502}) for _, username, vmid in calls)
                    pve[0]['failure'] = 500
                    bob.locator('#refresh').click()
                    bob.wait_for_selector('#role-panel[hidden]', state='attached')
                    assert bob.locator('#machines').inner_text() == ''
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print('PASS: separate browser users, scoped VM choices, saved roles, dashboard update, mobile layout, revocation and outage clearing')
    print(destination)


if __name__ == '__main__':
    main()
