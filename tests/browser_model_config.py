"""Model config editor through HTTPS and authorized simulated guest operations."""
from pathlib import Path
import json
import shutil
import sys
import tempfile

import pytest
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import guest_model_config as guest
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm
from https_fixture import secure_server, PASSWORD
from sample_fixture import install_backend


def main():
    destination = Path(sys.argv[1]) if len(sys.argv)>1 else Path(tempfile.mkdtemp(prefix='caf-model-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-model-test-') as temporary, pytest.MonkeyPatch.context() as patch:
        root = Path(temporary).resolve()
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples', root / 'examples')
        _, original, _ = install_backend(patch, root)
        roots = {9403:root/'participant', 9402:root/'scenarioforge'}
        for path in roots.values(): path.mkdir()
        ev.write_json(roots[9403]/'configs/cli.json', dict(provider='openai', url='https://models.example/v1', model='custom-model', ssl_verify=True, api_key='never-show-this', network_policy={'disallow':['10.0.0.1']}))
        (roots[9402]/'.scenarioforge.env').write_text('CORETG_AI_PROVIDER=litellm\nCORETG_AI_BASE_URL=https://models.example/v1\nCORETG_AI_MODEL=scenario-model\nCORE_HOST=10.0.0.2\n')
        patch.setattr(guest, 'LOCK', root/'guest-maintenance.lock')
        patch.setattr(guest, 'SECRETS', root/'guest-secrets')
        class Agent(original):
            def call(self, vmid, op, **data):
                if op in ('model_config','write'):
                    self.authorize(['guest','exec',vmid])
                    return guest.dispatch(dict(data, root=str(roots[vmid])))
                return super().call(vmid, op, **data)
        patch.setattr(ev, 'GuestAgent', Agent)
        with pve_server(root) as pve:
            pve[0]['groups'] += ',caf-maintainers'
            pve[0]['resources']['operator@pve'] = [vm(9403),vm(9402)]
            dashboard = UserDashboard(root/'examples/01-reuse-export.yaml',root/'runs',2,lambda b,a:Probe(b,a,[]))
            try:
                with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server,sync_playwright() as playwright:
                    browser=playwright.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(ignore_https_errors=True,viewport={'width':1440,'height':1080})
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click()
                    expect(page.locator('#role-participant')).to_be_enabled(timeout=30000)
                    page.locator('#role-participant').select_option('9403');page.locator('#role-scenarioforge').select_option('9402')
                    page.get_by_role('button',name='Save VM roles').click()
                    expect(page.locator('[data-page=setup] #model-config-panel')).to_have_count(0)
                    page.locator('[data-route=experiments]').click()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#new-experiment').click()
                    expect(page.locator('#experiment-dialog #model-config-panel')).to_be_visible()
                    expect(page.locator('#model-participant-read')).to_be_enabled(timeout=30000)
                    page.locator('#model-participant-read').click()
                    expect(page.locator('#model-participant-model')).to_have_value('custom-model',timeout=30000)
                    expect(page.locator('#model-participant-key')).to_have_value('')
                    assert 'never-show-this' not in page.locator('body').inner_text()
                    page.locator('#model-participant-model').fill('updated-model')
                    expect(page.locator('#model-participant-use')).to_have_count(0)
                    expect(page.locator('#model-config-scenarioforge')).to_have_count(0)
                    page.get_by_role('button',name='Create experiment',exact=True).click()
                    expect(page.locator('#experiment-error')).to_contain_text('Save the model settings')
                    page.locator('#model-participant-key').fill('replacement-key')
                    page.locator('#model-participant-save').click()
                    expect(page.locator('#model-participant-message')).to_contain_text('Saved. Settings copied to the VM',timeout=30000)
                    expect(page.locator('#experiment-error')).to_have_text('')
                    assert ev.read_json(roots[9403]/'configs/cli.json')['api_key']=='replacement-key'
                    assert ev.read_json(roots[9403]/'configs/cli.json')['network_policy']['disallow']==['10.0.0.1']
                    expect(page.locator('#model-participant-key')).to_have_value('')
                    page.locator('#model-config-panel').scroll_into_view_if_needed()
                    page.screenshot(path=str(destination/'model-settings.png'))
                    expect(page.locator('#experiment-model-context')).to_contain_text('openai / updated-model')
                    page.get_by_role('button',name='Close new experiment').click()
                    expect(page.locator('#samples-context')).to_contain_text('openai / updated-model')
                    page.reload();expect(page.locator('#samples-context')).to_contain_text('openai / updated-model',timeout=30000)
                    page.locator('#new-experiment').click();page.get_by_label('Sample',exact=True).select_option('smoke')
                    page.get_by_role('button',name='Create experiment',exact=True).click()
                    page.get_by_role('button',name='Run experiment',exact=True).click()
                    expect(page.locator('#runs')).to_contain_text('completed',timeout=30000)
                    expect(page.locator('#runs')).to_contain_text('VM 9403 · openai / updated-model')
                    workspace=Workspace(root/'runs','operator@pve')
                    run=next(workspace.runs.glob('sample-*/workflow.json'))
                    assert ev.read_json(run)['runtime']['model']['name']=='updated-model'
                    assert 'replacement-key' not in page.locator('#console-output').inner_text()
                    for path in workspace.path.rglob('*.json'): assert 'replacement-key' not in path.read_text()
                    page.set_viewport_size({'width':390,'height':844})
                    page.locator('#new-experiment').click()
                    expect(page.locator('#model-participant-read')).to_be_enabled()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert page.locator('#experiment-dialog').evaluate('(n)=>n.getBoundingClientRect().width<=innerWidth && n.getBoundingClientRect().height<=innerHeight')
                    page.screenshot(path=str(destination/'model-settings-mobile.png'),full_page=True)
                    assert not errors,errors
                    browser.close()
            finally: dashboard.close()
    print('PASS: CAF-only editor, combined Save, unsaved guard, hidden keys, reload and sample execution, mobile layout')
    print(destination)

if __name__=='__main__': main()
