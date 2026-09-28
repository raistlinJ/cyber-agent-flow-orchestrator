"""Slow-request UI regression: real HTTPS/PVE auth, controlled guest observations."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm, access


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-loading-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-loading-browser-') as temp:
        root = Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve'] = [vm(101), vm(102), vm(103)]
            release = {vmid: threading.Event() for vmid in (101, 102, 103)}
            class SlowProbe(Probe):
                def guest(self, vmid, definition, units):
                    assert release[vmid].wait(90)
                    return super().guest(vmid, definition, units)
            dash = UserDashboard(root / 'examples/01-reuse-export.yaml', root / 'runs', 300,
                                 lambda b, a: SlowProbe(b, a, []))
            dash.select(access(pve), dict(scenarioforge=101, participant=102, core=103))
            try:
                with secure_server(dash, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='chrome', headless=True)
                    context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1080})
                    page = context.new_page()
                    errors, requests, logins, sessions, saves, refreshes = [], [], [], [], [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: requests.append(request.url))
                    page.route('**/api/login', lambda route: logins.append(route))
                    page.route('**/api/session', lambda route: sessions.append(route))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password', exact=True).fill(PASSWORD)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    expect(page.get_by_label('Username')).to_be_disabled()
                    expect(page.get_by_label('Password', exact=True)).to_be_disabled()
                    expect(page.locator('#login-status')).to_contain_text('Checking your account')
                    page.locator('#login-form').dispatch_event('submit')
                    assert len(logins) == 1
                    page.screenshot(path=str(destination / 'login-loading.png'), full_page=True)
                    logins[0].continue_()
                    page.wait_for_selector('#dashboard-controls')
                    expect(page.locator('#refresh')).to_be_disabled()
                    expect(page.locator('#loading-label')).to_contain_text('login session')
                    assert not any('/api/status' in url for url in requests)
                    page.wait_for_function("document.getElementById('dashboard-controls').disabled")
                    # Wait until the route callback has received the session request.
                    expect(page.locator('#loading-progress')).not_to_have_attribute('value', '100')
                    assert len(sessions) == 1
                    sessions[0].continue_()
                    page.unroute('**/api/session')
                    expect(page.locator('#loading-label')).to_contain_text('0/3', timeout=30000)
                    expect(page.locator('#role-participant')).to_be_disabled()
                    release[101].set()
                    expect(page.locator('#loading-progress')).to_have_attribute('value', '33', timeout=30000)
                    page.screenshot(path=str(destination / 'dashboard-loading.png'), full_page=True)
                    release[102].set()
                    expect(page.locator('#loading-progress')).to_have_attribute('value', '67', timeout=30000)
                    release[103].set()
                    expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                    expect(page.locator('#loading-label')).to_contain_text('100%')
                    page.route('**/api/roles', lambda route: saves.append(route))
                    page.get_by_role('button', name='Save VM roles').click()
                    expect(page.locator('#role-participant')).to_be_disabled()
                    expect(page.locator('#refresh')).to_be_disabled()
                    page.locator('#role-form').dispatch_event('submit')
                    before = sum('/api/status' in url for url in requests)
                    page.locator('#refresh').dispatch_event('click')
                    page.wait_for_timeout(5500)  # Cross an automatic polling interval while save is held.
                    assert sum('/api/status' in url for url in requests) == before
                    assert len(saves) == 1
                    saves[0].continue_()
                    expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                    page.route('**/api/status?refresh=1', lambda route: refreshes.append(route))
                    page.locator('#refresh').click()
                    expect(page.locator('#role-participant')).to_be_disabled()
                    expect(page.locator('#loading-label')).to_contain_text('Refreshing dashboard')
                    refreshes[0].fulfill(status=503, content_type='application/json', body='{"error":"Temporary outage"}')
                    expect(page.locator('#notice')).to_contain_text('503')
                    expect(page.locator('#refresh')).to_be_enabled()
                    assert len(logins) == 1 and not page.url.endswith('/login')
                    page.unroute('**/api/status?refresh=1')
                    page.locator('#refresh').click()
                    expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.reload()
                    expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(destination / 'dashboard-mobile.png'), full_page=True)
                    assert not errors, errors
                    browser.close()
            finally:
                for event in release.values(): event.set()
                dash.close()
    print('PASS: single login, serialized bootstrap, locked controls, measured VM progress, no overlapping save/poll, outage recovery and mobile layout')
    print(destination)


if __name__ == '__main__':
    main()
