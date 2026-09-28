"""Page navigation and persistent console using a simulated lab over real HTTPS."""
from pathlib import Path
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_smoke import FakeDashboard
from https_fixture import secure_server, PASSWORD


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-navigation-'))
    destination.mkdir(parents=True, exist_ok=True)
    dashboard = FakeDashboard()
    dashboard.value['updates'] = dict(can_update=True, group='caf-maintainers', jobs=[], applications=[
        dict(role='participant', vmid=9403, ref='main'), dict(role='scenarioforge', vmid=9402, ref='main')])
    dashboard.value.update(roles=dict(participant=9403, scenarioforge=9402, core=9401), available_vms=[
        dict(vmid=9403, name='participant', status='running'), dict(vmid=9402, name='app', status='running'),
        dict(vmid=9401, name='core', status='running')])
    with tempfile.TemporaryDirectory(prefix='caf-nav-server-') as temporary, secure_server(dashboard, Path(temporary)) as server:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(ignore_https_errors=True, viewport={'width':1440, 'height':1000})
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))
            page.goto(server['origin'])
            page.get_by_label('Username').fill('operator')
            page.get_by_label('Password').fill(PASSWORD)
            page.get_by_role('button', name='Sign in', exact=True).click()
            expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
            page.evaluate("window.originalConsole=document.getElementById('console-output')")
            console_text=page.locator('#console-output').text_content()
            before=len(requests)
            for route, title in [('overview','Overview'),('experiments','Experiments'),('applications','Applications'),('setup','Lab setup')]:
                page.locator(f'[data-route={route}]').click()
                expect(page.locator('#page-title')).to_have_text(title)
                expect(page.locator(f'[data-route={route}]')).to_have_attribute('aria-current','page')
                assert page.locator('[data-page]:not([hidden])').evaluate_all('(nodes)=>nodes.every(n=>n.dataset.page===location.hash.slice(1))')
                assert page.evaluate("window.originalConsole===document.getElementById('console-output')")
                assert page.locator('#console-output').text_content()==console_text
                assert page.locator('#debug-console').evaluate('(n)=>Math.abs(n.getBoundingClientRect().bottom-innerHeight)<2')
                assert page.locator('#console-output').evaluate('(n)=>n.getBoundingClientRect().bottom<=innerHeight+1')
                page.screenshot(path=str(destination/f'{route}-desktop.png'))
            assert len(requests)==before  # Navigating does not reload or issue extra polls.
            page.locator('#role-participant').select_option('9402')
            page.locator('[data-route=applications]').click()
            page.locator('[data-update-ref=participant]').fill('release/test')
            page.locator('[data-route=setup]').click()
            expect(page.locator('#role-participant')).to_have_value('9402')
            page.locator('[data-route=applications]').click()
            expect(page.locator('[data-update-ref=participant]')).to_have_value('release/test')
            page.go_back()
            expect(page.locator('#page-title')).to_have_text('Lab setup')
            page.go_forward()
            expect(page.locator('#page-title')).to_have_text('Applications')
            page.locator('#debug-console summary').click()
            expect(page.locator('#console-output')).to_be_hidden()
            page.locator('[data-route=experiments]').click()
            expect(page.locator('#console-output')).to_be_hidden()
            page.reload()
            expect(page.locator('#page-title')).to_have_text('Experiments')
            expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
            expect(page.locator('#console-output')).to_be_hidden()
            page.locator('#debug-console summary').click()
            page.set_viewport_size({'width':390,'height':844})
            for route in ('overview','experiments','applications','setup'):
                page.locator(f'[data-route={route}]').click()
                expect(page.locator(f'[data-route={route}]')).to_have_attribute('aria-current','page')
                page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.evaluate('scrollTo(0,document.body.scrollHeight)')
                footer=page.locator('footer').bounding_box()
                dock=page.locator('#debug-console').bounding_box()
                assert footer['y']+footer['height']<=dock['y']+1, (route, footer, dock)
                assert page.locator('#console-output').evaluate('(n)=>n.getBoundingClientRect().bottom<=innerHeight+1')
                page.screenshot(path=str(destination/f'{route}-mobile.png'))
            page.evaluate("location.hash='unknown-page'")
            expect(page.locator('#page-title')).to_have_text('Overview')
            # Navigation and the dock remain usable while a foreground read locks edits.
            pending=[]
            page.route('**/api/status?refresh=1', lambda route: pending.append(route))
            page.locator('#refresh').click()
            expect(page.locator('#refresh')).to_be_disabled()
            page.locator('[data-route=applications]').click()
            expect(page.locator('#page-title')).to_have_text('Applications')
            expect(page.locator('[data-update-role=participant][data-update-action=update]')).to_be_disabled()
            expect(page.locator('#download-console')).to_be_enabled()
            page.locator('[data-route=setup]').click()
            expect(page.locator('#role-participant')).to_be_disabled()
            assert len(pending)==1
            pending.pop().fulfill(json=dashboard.value)
            expect(page.locator('#refresh')).to_be_enabled()
            assert not errors, errors
            browser.close()
    print('PASS: page routes/history, preserved forms/logs, fixed console, collapse persistence, no navigation requests, desktop/mobile layout')
    print(destination)


if __name__ == '__main__': main()
