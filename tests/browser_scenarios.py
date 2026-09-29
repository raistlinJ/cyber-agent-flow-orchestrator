"""Saved ScenarioForge selector browser check; guest access is simulated."""
from pathlib import Path
import shutil
import sys
import tempfile
import pytest
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator import scenarios
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm


def main():
    with tempfile.TemporaryDirectory() as temp, pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        class Guest:
            def call(self, vmid, op, **data):
                item = dict(id='a'*64, scenario='Demo saved scenario', path='/opt/scenarioforge/uploads/demo.xml',
                            sha256='b'*64, bytes=1024, resolved_chain=True, chain_length=3)
                if op == 'list':
                    return dict(items=[item, dict(item, id='c'*64, scenario='Unresolved', resolved_chain=False)],
                                roots=data['roots'], truncated=False)
                assert op == 'snapshot'
                return dict(item, snapshot_path='/opt/scenarioforge/outputs/caf-orchestrator/'+data['token']+'/scenario.xml')
        patch.setattr(scenarios, 'guest', lambda backend: Guest())
        with pve_server(root) as pve:
            pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403)]
            dashboard = UserDashboard(root / 'examples/01-reuse-export.yaml', root / 'runs', 2, lambda b,a: Probe(b,a,[]))
            try:
                with secure_server(dashboard, root / 'web', auth=make_auth(pve)) as server, sync_playwright() as pw:
                    browser = pw.chromium.launch(channel='chrome', headless=True)
                    page = browser.new_page(ignore_https_errors=True, viewport={'width':1280,'height':1000})
                    errors=[]
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve')
                    page.get_by_label('Password', exact=True).fill(PASSWORD)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.locator('[data-route=setup]').click()
                    page.locator('#role-scenarioforge').select_option('9402')
                    page.locator('#role-participant').select_option('9403')
                    page.get_by_role('button', name='Save VM roles').click()
                    page.locator('[data-route=experiments]').click()
                    page.locator('#new-experiment').click()
                    page.get_by_label('Experiment type', exact=True).select_option('scenarioforge-xml')
                    page.locator('#load-scenarios').click()
                    expect(page.locator('#scenario-selection option')).to_have_count(3, timeout=30000)
                    assert page.locator('#scenario-selection option').nth(2).is_disabled()
                    page.locator('#scenario-selection').select_option('a'*64)
                    expect(page.locator('#scenario-selection-info')).to_contain_text('demo.xml')
                    page.locator('#scenario-allowed').fill('10.77.0.0/24')
                    page.screenshot(path='/tmp/caf-scenario-selector.png')
                    page.get_by_role('button', name='Create experiment', exact=True).click()
                    expect(page.locator('#experiment-dialog')).to_be_hidden(timeout=30000)
                    row=page.locator('#runs tr').filter(has_text='Demo saved scenario')
                    expect(row).to_contain_text('ready')
                    expect(row.get_by_role('button',name='Deploy and run',exact=True)).to_be_enabled()
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print('Scenario selector browser check passed')


if __name__ == '__main__':
    main()
