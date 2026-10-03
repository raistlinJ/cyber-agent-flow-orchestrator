"""Dashboard refresh preserves mounted rows, focus and the last good snapshot."""
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
    with tempfile.TemporaryDirectory(prefix='caf-inplace-refresh-') as temp:
        root=Path(temp)
        with pve_server(root) as pve:
            dashboard=Dashboard()
            with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server, sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                page=browser.new_page(ignore_https_errors=True)
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(server['origin'])
                page.get_by_label('Username').fill('operator@pve')
                page.get_by_label('Password',exact=True).fill(PASSWORD)
                page.get_by_role('button',name='Sign in',exact=True).click()
                expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                expect(page.locator('#loading-modal')).not_to_be_visible()
                page.evaluate("window.savedCard=document.querySelector('#machines .card')")
                pending=[]
                page.route('**/api/status?refresh=1',lambda route:pending.append(route))
                page.locator('#refresh').click()
                expect(page.locator('#loading-label')).to_contain_text('Background refresh')
                expect(page.locator('#loading-modal')).not_to_be_visible()
                expect(page.locator('#refresh')).to_be_enabled()
                value=deepcopy(dashboard.value)
                value['vms'][0]['name']='Updated VM name'
                pending.pop().fulfill(json=value)
                expect(page.locator('#machines')).to_contain_text('Updated VM name')
                assert page.evaluate("window.savedCard===document.querySelector('#machines .card')")
                assert page.evaluate("document.activeElement.id==='refresh'")
                page.locator('#refresh').click()
                expect(page.locator('#loading-label')).to_contain_text('Background refresh')
                pending.pop().fulfill(status=503,json={'error':'Unavailable'})
                expect(page.locator('#notice')).to_contain_text('503')
                expect(page.locator('#machines')).to_contain_text('Updated VM name')
                assert page.evaluate("window.savedCard===document.querySelector('#machines .card')")
                expect(page.locator('#loading-modal')).not_to_be_visible()
                assert not errors,errors
                browser.close()
    print('In-place refresh browser checks passed')


if __name__=='__main__':main()
