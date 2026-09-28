"""Application maintenance UI, real HTTPS/PVE auth; guest/OS operations simulated."""
from pathlib import Path
import shutil
import sys
import tempfile

import pytest
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator import updates
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm
from sample_fixture import install_backend


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-updates-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-updates-browser-') as temp, pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        calls, agent, _ = install_backend(patch, root)
        revisions = {9402: 'a' * 40, 9403: 'a' * 40}
        original = agent.call
        def call(self, vmid, op, **data):
            original(self, vmid, op, **data)
            if op == 'app_stage': return {'path': '/tmp/test-source.bundle'}
            if op == 'app_update': revisions[vmid] = 'b' * 40
            if op == 'app_rollback': revisions[vmid] = 'a' * 40
            return {'revision': revisions[vmid], 'missing_controls': ['allowed_tools'] if vmid == 9403 and revisions[vmid][0] == 'a' else [], 'modified': False}
        patch.setattr(agent, 'call', call)
        patch.setattr(agent, 'put', lambda self, vmid, path, content: self.authorize(['guest', 'exec', vmid]))
        def package(source, ref, base, directory):
            file = directory / 'source.bundle'
            file.write_bytes(b'simulated guest transfer; real Git packaging has separate integration tests')
            return 'b' * 40, file
        patch.setattr(updates, 'package', package)
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403)]
            dashboard = UserDashboard(root / 'examples/01-reuse-export.yaml', root / 'runs', 2, lambda b, a: Probe(b, a, []))
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
                    page.wait_for_selector('#updates-panel:not([hidden])')
                    page.locator('#role-participant').select_option('9403')
                    page.locator('#role-scenarioforge').select_option('9402')
                    page.get_by_role('button', name='Save VM roles').click()
                    button = lambda role, action: page.locator(f'[data-update-role="{role}"][data-update-action="{action}"]')
                    expect(button('participant', 'inspect')).to_be_enabled(timeout=30000)
                    expect(button('participant', 'update')).to_be_disabled()
                    button('participant', 'inspect').click()
                    expect(page.locator('#update-cards')).to_contain_text('Missing evaluator controls', timeout=30000)
                    pve[0]['groups'] += ',caf-maintainers'
                    page.locator('#refresh').click()
                    for role in ('participant', 'scenarioforge'):
                        expect(button(role, 'update')).to_be_enabled(timeout=30000)
                        button(role, 'update').click()
                        expect(page.locator('#update-cards')).to_contain_text('bbbbbbbbbbbb', timeout=30000)
                        # Wait for this specific application to finish before rollback.
                        expect(page.locator('#update-cards article').filter(has=button(role, 'update'))).to_contain_text('bbbbbbbbbbbb', timeout=30000)
                        expect(button(role, 'rollback')).to_be_enabled(timeout=30000)
                        button(role, 'rollback').click()
                        expect(page.locator('#update-cards article').filter(has=button(role, 'update'))).to_contain_text('aaaaaaaaaaaa', timeout=30000)
                    page.screenshot(path=str(destination / 'updates-desktop.png'), full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.reload()
                    page.wait_for_selector('#updates-panel:not([hidden])')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(destination / 'updates-mobile.png'), full_page=True)
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print('PASS: version check, maintenance permission, both application updates/rollback, responsive UI')
    print(destination)


if __name__ == '__main__':
    main()
