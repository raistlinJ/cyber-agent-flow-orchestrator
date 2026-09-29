"""Refresh cadence and serialized events behind the loading modal."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_smoke import sample
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth


class Dashboard:
    scoped = True

    def __init__(self):
        self.value = sample()
        self.value['runs'] = []
        self.value['updates'] = dict(can_update=True, group='caf-maintainers', jobs=[], applications=[
            dict(role='participant', vmid=9403, ref='main'),
            dict(role='scenarioforge', vmid=9402, ref='main')])

    def read(self, access, *, force=False, observe=True):
        access.current()
        return deepcopy(self.value)

    def maintain(self, access, data):
        access.current()
        return {'id': 'simulated-check'}


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-refresh-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-refresh-browser-') as temp:
        root = Path(temp)
        with pve_server(root) as pve:
            pve[0]['groups'] += ',caf-maintainers'
            dashboard = Dashboard()
            with secure_server(dashboard, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                page = browser.new_page(ignore_https_errors=True, viewport={'width': 1440, 'height': 1080})
                errors, statuses, pending, applications = [], [], [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: statuses.append(request.url) if '/api/status' in request.url else None)
                page.goto(server['origin'])
                page.get_by_label('Username').fill('operator@pve')
                page.get_by_label('Password', exact=True).fill(PASSWORD)
                page.get_by_role('button', name='Sign in', exact=True).click()
                expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                page.locator('[data-route=setup]').click()
                period = page.get_by_label('Automatic refresh', exact=True)
                def choose_period(value):
                    # Simulate an already queued preference event while the modal blocks clicks.
                    if page.locator('#loading-modal').is_visible():
                        page.evaluate("value => {const select=document.getElementById('refresh-period');select.value=value;select.dispatchEvent(new Event('change'));}", value)
                    else:
                        page.locator('[data-route=setup]').click()
                        period.select_option(value)
                expect(period).to_have_value('1')
                assert period.locator('option').all_text_contents() == [
                    'Never', 'Every 1 minute', 'Every 2 minutes', 'Every 5 minutes', 'Every 10 minutes']
                page.clock.install()
                choose_period('0')
                before = len(statuses)
                page.clock.fast_forward(601000)
                assert len(statuses) == before  # Never does not poll an idle dashboard.
                page.reload()
                expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                expect(period).to_have_value('0')
                page.route('**/api/status?refresh=1', lambda route: pending.append(route))
                for minutes in (1, 2, 5, 10):
                    choose_period(str(minutes))
                    before = len(statuses)
                    page.clock.fast_forward(minutes * 60000 - 1000)
                    assert len(statuses) == before
                    page.clock.fast_forward(1000)
                    expect(page.locator('#loading-label')).to_contain_text('Background refresh')
                    assert len(pending) == 1
                    for role in ('participant', 'scenarioforge'):
                        expect(page.locator(f'[data-update-role="{role}"][data-update-action="update"]')).to_be_enabled()
                    expect(page.locator('#refresh')).to_be_enabled()
                    # Changing to Never during a request is allowed; it cancels subsequent idle polls.
                    choose_period('0')
                    pending.pop().fulfill(json=dashboard.value)
                    expect(page.locator('#loading-label')).to_have_text('Dashboard loaded')

                # A background VM probe displays the modal while progress is read.
                choose_period('1')
                page.clock.fast_forward(60000)
                expect(page.locator('#loading-label')).to_contain_text('Background refresh')
                checking = deepcopy(dashboard.value)
                checking.update(refreshing=True, loading=dict(completed=1, total=3, percent=33, status='Checking VMs'))
                pending.pop().fulfill(json=checking)
                expect(page.locator('#loading-label')).to_contain_text('33%')
                choose_period('0')
                page.route('**/api/status?refresh=0', lambda route: pending.append(route))
                before = len(statuses)
                page.clock.fast_forward(601000)
                assert len(statuses) == before and not pending
                expect(page.locator('#loading-modal')).not_to_be_visible()
                choose_period('1')
                page.clock.fast_forward(5000)
                expect(page.locator('#loading-elapsed')).not_to_be_empty()
                assert len(pending) == 1 and pending[0].request.url.endswith('refresh=0')
                choose_period('0')
                page.route('**/api/applications', lambda route: applications.append(route))
                inspect = page.locator('[data-update-role="participant"][data-update-action="inspect"]')
                expect(inspect).to_be_enabled()
                page.screenshot(path=str(destination / 'background-refresh.png'), full_page=True)
                # Exercise queued action serialization; normal clicks are blocked by the modal.
                expect(page.locator('#loading-modal')).to_be_visible()
                inspect.dispatch_event('click')
                expect(inspect).to_be_disabled()
                inspect.dispatch_event('click')
                assert not applications  # The submitted action waits for the in-flight read.
                pending.pop().fulfill(json=dashboard.value)
                page.wait_for_timeout(100)
                assert len(applications) == 1
                applications.pop().fulfill(status=202, json={'id': 'simulated-check'})
                page.wait_for_timeout(100)
                assert len(pending) == 1
                pending.pop().fulfill(json=dashboard.value)
                expect(inspect).to_be_enabled()
                before = len(statuses)
                page.clock.fast_forward(601000)
                assert len(statuses) == before
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(destination / 'refresh-mobile.png'), full_page=True)
                # A failed background read must not dispatch the waiting action.
                choose_period('1')
                page.clock.fast_forward(60000)
                expect(page.locator('#loading-label')).to_contain_text('Background refresh')
                expect(page.locator('#loading-modal')).to_be_visible()
                inspect.dispatch_event('click')
                assert not applications
                pending.pop().fulfill(status=503, json={'error': 'Temporary outage'})
                expect(page.locator('#notice')).to_contain_text('503')
                expect(page.locator('#refresh')).to_be_enabled()
                assert not applications
                assert not errors, errors
                browser.close()
    print('PASS: all refresh periods, Never persistence, background controls, progress-only reads, serialized action and mobile layout')
    print(destination)


if __name__ == '__main__':
    main()
