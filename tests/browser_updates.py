"""Application maintenance UI, real HTTPS/PVE auth; guest/OS operations simulated."""
from pathlib import Path
import base64
import shutil
import sys
import tempfile
import threading

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
        finish_upload = threading.Event()
        dirty = set()
        replaceable = set()
        activation_failures = set()
        original = agent.call
        def call(self, vmid, op, **data):
            original(self, vmid, op, **data)
            if op == 'app_stage': return {'path': '/tmp/test-source.bundle'}
            if op == 'app_update' and vmid in activation_failures: raise ValueError('Tracked local edits appeared before activation. No files changed.')
            if op == 'app_update':
                revisions[vmid] = 'b' * 40
                dirty.discard(vmid)
                replaceable.discard(vmid)
            if op == 'app_rollback': revisions[vmid] = 'a' * 40
            return {'revision': revisions[vmid], 'missing_controls': ['allowed_tools'] if vmid == 9403 and revisions[vmid][0] == 'a' else [], 'modified': vmid in dirty, 'tools_config_replaceable': vmid in replaceable, 'modified_files': [' M \"mcp_client.py\"'] if vmid in dirty else [], 'modified_file_count': 1 if vmid in dirty else 0}
        patch.setattr(agent, 'call', call)
        def put(self, vmid, path, content):
            midpoint = len(content) // 2
            self.call(vmid, 'write', path=path, offset=0, content=base64.b64encode(content[:midpoint]).decode())
            if vmid == 9403:
                assert finish_upload.wait(90)
            self.call(vmid, 'write', path=path, offset=midpoint, content=base64.b64encode(content[midpoint:]).decode())
        patch.setattr(agent, 'put', put)
        def package(source, ref, base, directory, *, trace=None):
            file = directory / 'source.bundle'
            file.write_bytes(b'simulated guest transfer; real Git packaging has separate integration tests')
            if trace:
                trace.emit('response', '<img src=x onerror="window.consoleInjected=true">', force=True)
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
                    status = page.locator('#update-status-participant')
                    expect(status).to_contain_text('Update permission required:')
                    expect(status).to_contain_text('caf-maintainers PVE group')
                    assert button('participant', 'update').evaluate("node => getComputedStyle(node).cursor") == 'not-allowed'
                    page.screenshot(path=str(destination / 'updates-permission.png'), full_page=True)
                    pending = []
                    page.route('**/api/applications', lambda route: pending.append(route))
                    button('participant', 'inspect').click()
                    expect(status).to_contain_text('Please wait: Submitting application maintenance')
                    expect(button('participant', 'inspect')).to_be_disabled()
                    expect(page.locator('#download-console')).to_be_enabled()
                    page.locator('#debug-console summary').click()
                    expect(page.locator('#console-output')).not_to_be_visible()
                    page.locator('#debug-console summary').click()
                    expect(page.locator('#console-output')).to_be_visible()
                    page.wait_for_timeout(100)
                    assert len(pending) == 1
                    pending.pop().continue_()
                    page.unroute('**/api/applications')
                    expect(page.locator('#update-cards')).to_contain_text('Missing evaluator controls', timeout=30000)
                    expect(button('participant', 'inspect')).to_be_enabled(timeout=30000)
                    expect(page.locator('#console-output')).to_contain_text('app_inspect completed')
                    expect(page.locator('#console-output')).to_contain_text('PVE authorization passed')
                    expect(button('participant', 'update')).to_be_disabled()
                    expect(status).to_contain_text('Update permission required:')
                    pve[0]['groups'] += ',caf-maintainers'
                    page.locator('#refresh').click()
                    for role in ('participant', 'scenarioforge'):
                        expect(button(role, 'update')).to_be_enabled(timeout=30000)
                        expect(page.locator(f'#update-status-{role}')).to_have_text('Ready to check, update or roll back this application.')
                        button(role, 'update').click()
                        if role == 'participant':
                            expect(page.locator('#update-jobs')).to_contain_text('37 / 75 bytes acknowledged', timeout=30000)
                            expect(page.locator('#loading-label')).to_contain_text('49.3%')
                            expect(page.locator('#download-console')).to_be_enabled()
                            page.screenshot(path=str(destination / 'transfer-progress.png'), full_page=True)
                            finish_upload.set()
                        expect(page.locator('#update-cards')).to_contain_text('bbbbbbbbbbbb', timeout=30000)
                        # Wait for this specific application to finish before rollback.
                        expect(page.locator('#update-cards article').filter(has=button(role, 'update'))).to_contain_text('bbbbbbbbbbbb', timeout=30000)
                        expect(button(role, 'rollback')).to_be_enabled(timeout=30000)
                        button(role, 'rollback').click()
                        expect(page.locator('#update-cards article').filter(has=button(role, 'update'))).to_contain_text('aaaaaaaaaaaa', timeout=30000)
                    dirty.add(9403)
                    expect(button('participant', 'update')).to_be_enabled(timeout=30000)
                    before = len(calls)
                    button('participant', 'update').click()
                    expect(page.locator('#update-message')).to_contain_text('update: failed', timeout=30000)
                    expect(page.locator('#update-message')).to_contain_text('No source bundle downloaded or transferred')
                    expect(page.locator('#update-cards')).to_contain_text('Tracked local edits block updates')
                    expect(page.locator('#update-cards')).to_contain_text('mcp_client.py')
                    expect(page.locator('#loading-label')).to_contain_text('participant update failed')
                    expect(page.locator('#loading-progress')).to_be_hidden()
                    assert 'complete' not in (page.locator('#loading-status').get_attribute('class') or '').split()
                    assert [op for _, op, _ in calls[before:]] == ['app_inspect']
                    expect(button('participant', 'inspect')).to_be_enabled()
                    dirty.clear()
                    activation_failures.add(9403)
                    button('participant', 'update').click()
                    expect(page.locator('#update-message')).to_contain_text('Tracked local edits appeared before activation', timeout=30000)
                    latest = page.locator('#update-jobs article').first
                    expect(latest).to_contain_text('100%')
                    expect(latest).to_contain_text('Upload checksum verified')
                    expect(latest).to_contain_text('Activation not confirmed')
                    expect(latest).to_contain_text('Target revision: ' + 'b' * 40)
                    expect(page.locator('#update-cards article').filter(has=button('participant', 'update'))).to_contain_text('Installed revision (last checked): aaaaaaaaaaaa')
                    expect(page.locator('#loading-label')).to_contain_text('participant update failed')
                    page.screenshot(path=str(destination / 'updates-desktop.png'), full_page=True)
                    activation_failures.clear()
                    dirty.add(9403)
                    replaceable.add(9403)
                    button('participant', 'inspect').click()
                    expect(page.locator('#update-cards')).to_contain_text('Update will back up and replace this runtime catalog', timeout=30000)
                    expect(button('participant', 'update')).to_be_enabled(timeout=30000)
                    button('participant', 'update').click()
                    expect(page.locator('#update-message')).to_contain_text('update: completed', timeout=30000)
                    expect(page.locator('#loading-label')).to_have_text('Dashboard loaded')

                    with page.expect_download() as download:
                        page.locator('#download-console').click()
                    downloaded = Path(download.value.path()).read_text()
                    assert 'Upload complete; guest size and SHA-256 verified' in downloaded
                    assert PASSWORD not in downloaded
                    assert '<img src=x' in downloaded
                    assert page.locator('#console-output img').count() == 0
                    assert not page.evaluate('Boolean(window.consoleInjected)')
                    page.locator('#debug-console summary').click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.reload()
                    page.wait_for_selector('#updates-panel:not([hidden])')
                    expect(page.locator('#console-output')).not_to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(destination / 'updates-mobile.png'), full_page=True)
                    assert not errors, errors
                    browser.close()
            finally:
                finish_upload.set()
                dashboard.close()
    print('PASS: version check, explicit permission/loading reasons, permission refresh, both application updates/rollback, responsive UI')
    print(destination)


if __name__ == '__main__':
    main()
