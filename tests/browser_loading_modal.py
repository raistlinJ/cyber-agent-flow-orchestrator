"""Loading modal coverage for dashboard and independent run windows."""
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_refresh import Dashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth


def main():
    with tempfile.TemporaryDirectory(prefix='caf-modal-') as temp:
        root = Path(temp)
        with pve_server(root) as pve:
            dashboard = Dashboard()
            with secure_server(dashboard, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(server['origin'])
                expect(page.locator('#loading-modal')).not_to_be_visible()
                page.get_by_label('Username').fill('operator@pve')
                page.get_by_label('Password', exact=True).fill(PASSWORD)
                page.get_by_role('button', name='Sign in', exact=True).click()
                expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                expect(page.locator('#loading-modal')).not_to_be_visible()
                pending = []
                page.route('**/api/status?refresh=1', lambda route: pending.append(route))
                page.locator('#refresh').click()
                expect(page.locator('#loading-modal')).to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('#loading-modal')).to_be_visible()
                pending.pop().fulfill(status=503, json={'error': 'Unavailable'})
                expect(page.locator('#loading-modal')).not_to_be_visible()
                expect(page.locator('#notice')).to_contain_text('503')
                page.locator('#refresh').click()
                expect(page.locator('#loading-modal')).to_be_visible()
                pending.pop().fulfill(json=dashboard.value)
                expect(page.locator('#loading-modal')).not_to_be_visible()

                # Automatic refresh uses the same modal, including asynchronous VM checks.
                page.evaluate('void refresh(true, true)')
                expect(page.locator('#loading-modal')).to_be_visible()
                pending.pop().fulfill(json=dashboard.value)
                expect(page.locator('#loading-modal')).not_to_be_visible()

                workflow = dict(output='/runs/demo', recorded_status='completed', coordinator_active=False,
                    sample_progress=dict(name='Demo', phase='completed', percent=100, finished_trials=1,
                        planned_trials=1, verified_successes=1, errors=0, elapsed_seconds=1,
                        active=False, max_turns=3, wall_seconds=120, trials=[], events=[]))
                for view, endpoint in [('results', 'results'), ('progress', 'status')]:
                    popup = context.new_page()
                    popup.on('pageerror', lambda error: errors.append(str(error)))
                    held = []
                    popup.route('**/api/runs/demo/' + endpoint, lambda route: held.append(route))
                    popup.goto(server['origin'] + '/run?view=' + view + '&run=demo')
                    expect(popup.locator('#loading-modal')).to_be_visible()
                    held.pop().fulfill(json={'workflow': workflow, 'evaluation': None} if view == 'results' else workflow)
                    expect(popup.locator('#loading-modal')).not_to_be_visible()
                    if view == 'progress':
                        expect(popup.get_by_role('button', name='Open results')).to_have_count(0)
                    popup.locator('#refresh-run').click()
                    expect(popup.locator('#loading-modal')).to_be_visible()
                    held.pop().abort()
                    expect(popup.locator('#loading-modal')).not_to_be_visible()
                    expect(popup.locator('#run-error')).to_be_visible()
                    popup.set_viewport_size({'width': 390, 'height': 844})
                    popup.locator('#refresh-run').click()
                    expect(popup.locator('#loading-modal')).to_be_visible()
                    assert popup.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    popup.screenshot(path=str(root / (view + '-loading.png')))
                    held.pop().fulfill(status=403, json={'error': 'Forbidden'})
                    expect(popup.locator('#loading-modal')).not_to_be_visible()
                    popup.close()
                assert not errors, errors
                browser.close()
    print('PASS: dashboard and popup loading, error recovery, modal dismissal prevention, mobile layout, no Results button in Progress')


if __name__ == '__main__':
    main()
