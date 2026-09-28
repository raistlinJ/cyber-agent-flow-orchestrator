"""Browser login/TOTP check against the HTTPS PVE test double (no live host)."""
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_smoke import FakeDashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth


class PVEFakeDashboard(FakeDashboard):
    scoped = True

    def read(self, access, *, force=False):
        access.current()
        return super().read()


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-pve-preview-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-pve-browser-') as temporary:
        root = Path(temporary)
        with pve_server(root) as pve, secure_server(PVEFakeDashboard(), root / 'web', auth=make_auth(pve)) as server:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 1000}, ignore_https_errors=True)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(server['origin'])
                page.get_by_text('Sign in with your Proxmox account.').wait_for()
                page.get_by_label('Username').fill('operator@pve')
                page.get_by_label('Password', exact=True).fill(PASSWORD)
                pve[0]['groups'] = ''
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.get_by_text('Login failed or orchestrator access not granted').wait_for()
                pve[0].update(groups='caf-orchestration', tfa=True)
                page.get_by_label('Password', exact=True).fill(PASSWORD)
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.get_by_label('Authenticator code (TOTP)').wait_for()
                assert page.locator('#password').is_hidden()
                assert not any(c['name'] == '__Host-caf_session' for c in page.context.cookies())
                page.screenshot(path=str(destination / 'pve-totp-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(destination / 'pve-totp-mobile.png'), full_page=True)
                page.get_by_label('Authenticator code (TOTP)').fill('123456')
                page.get_by_role('button', name='Verify code', exact=True).click()
                page.wait_for_selector('.card')
                assert page.locator('#username-label').inner_text() == 'operator@pve'
                pve[0]['groups'] = ''
                page.locator('#refresh').click()
                page.wait_for_url('**/login')
                assert page.request.get(server['origin'] + '/api/status').status == 401
                assert not errors, errors
                browser.close()
    print('PVE browser checks passed: group denial, TOTP, responsive page, login and live revocation.')
    print(destination)


if __name__ == '__main__':
    main()
