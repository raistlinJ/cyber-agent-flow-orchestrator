"""Model config editor through HTTPS and authorized simulated guest operations."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
import threading

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
        patch.setattr(guest, 'network_scope', lambda url,config:dict(provider_host='models.example',excluded_targets=['192.0.2.8','192.0.2.1'],warnings=[]))
        patch.setattr(guest, 'LOCK', root/'guest-maintenance.lock')
        patch.setattr(guest, 'SECRETS', root/'guest-secrets')
        fail_save = [False]
        push_entered, release_push = threading.Event(), threading.Event()
        create_entered, release_create = threading.Event(), threading.Event()
        class Agent(original):
            def call(self, vmid, op, **data):
                if op in ('model_config','write'):
                    self.authorize(['guest','exec',vmid])
                    if data.get('action') == 'save':
                        push_entered.set()
                        assert release_push.wait(20)
                    if data.get('action') == 'save' and fail_save[0]:
                        raise ValueError('Model configuration save failed (fixture)')
                    return guest.dispatch(dict(data, root=str(roots[vmid])))
                return super().call(vmid, op, **data)
        patch.setattr(ev, 'GuestAgent', Agent)
        with pve_server(root) as pve:
            pve[0]['groups'] += ',caf-maintainers'
            pve[0]['resources']['operator@pve'] = [vm(9403),vm(9402),vm(9404)]
            dashboard = UserDashboard(root/'examples/01-reuse-export.yaml',root/'runs',2,lambda b,a:Probe(b,a,[]))
            original_create = dashboard.samples.create
            def create(*args, **kwargs):
                kwargs.pop('evaluation',None)  # This fixture isolates model persistence.
                progress=kwargs.pop('progress',None)
                if progress:progress(3,'Saving experiment')
                create_entered.set()
                assert release_create.wait(20)
                return original_create(*args, **kwargs)
            # Isolate model persistence here; full ScenarioForge sample creation is checked in browser_scenario_samples.py.
            patch.setattr(dashboard.scenarios, 'create_sample', create)
            try:
                with secure_server(dashboard,root/'web',auth=make_auth(pve)) as server,sync_playwright() as playwright:
                    browser=playwright.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(ignore_https_errors=True,viewport={'width':1440,'height':1080})
                    errors=[];dialogs=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.accept()))
                    page.goto(server['origin'])
                    page.get_by_label('Username').fill('operator@pve');page.get_by_label('Password',exact=True).fill(PASSWORD)
                    page.get_by_role('button',name='Sign in',exact=True).click()
                    page.locator('[data-route=setup]').click()
                    expect(page.locator('#role-participant')).to_be_enabled(timeout=30000)
                    page.locator('#role-core').select_option('9404');page.locator('#role-participant').select_option('9403');page.locator('#role-scenarioforge').select_option('9402')
                    page.get_by_role('button',name='Save VM roles').click()
                    expect(page.locator('[data-page=setup] #model-config-panel')).to_have_count(0)
                    page.locator('[data-route=experiments]').click()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#new-experiment').click()
                    page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                    expect(page.locator('#experiment-dialog #model-config-panel')).to_be_visible()
                    expect(page.locator('#model-participant-read')).to_be_enabled(timeout=30000)
                    for field in ['provider','url','model','ssl','key','clear']:
                        expect(page.locator('#model-participant-'+field)).to_be_enabled()
                    page.locator('#model-participant-model').fill('editable-before-pull')
                    expect(page.locator('#model-participant-apply')).to_be_enabled()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#model-participant-read').click()
                    expect(page.locator('#model-participant-model')).to_have_value('custom-model',timeout=30000)
                    assert 'replace all model settings' in dialogs[-1]
                    expect(page.locator('#model-participant-key')).to_have_value('')
                    assert 'never-show-this' not in page.locator('body').inner_text()
                    expect(page.locator('#model-participant-use')).to_have_count(0)
                    expect(page.locator('#model-participant-save')).to_have_count(0)
                    expect(page.locator('#model-participant-apply')).to_be_disabled()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    # An input event/autofill with the pulled value is not an
                    # edit and must not demand another VM save.
                    page.locator('#model-participant-model').dispatch_event('input')
                    expect(page.locator('#model-participant-apply')).to_be_disabled()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.locator('#model-participant-model').fill('temporary-edit')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#model-participant-model').fill('custom-model')
                    expect(page.locator('#model-participant-apply')).to_be_disabled()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    expect(page.locator('#model-config-scenarioforge')).to_have_count(0)
                    expect(page.locator('.model-card-heading #model-participant-read')).to_be_visible()
                    page.locator('#model-participant-model').fill('')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.get_by_role('tab',name='Experiment',exact=True).click()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                    page.locator('#model-participant-model').fill('updated-model')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    expect(page.locator('#model-participant-apply')).to_be_enabled()
                    page.locator('#model-participant-key').fill('replacement-key')
                    fail_save[0] = True
                    page.get_by_role('button',name='Apply settings',exact=True).click()
                    assert push_entered.wait(10)
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.screenshot(path=str(destination/'saving-progress.png'))
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.locator('#experiment-save-progress').evaluate('(n)=>n.getBoundingClientRect().right<=innerWidth && n.getBoundingClientRect().bottom<innerHeight')
                    page.screenshot(path=str(destination/'saving-progress-mobile.png'))
                    page.set_viewport_size({'width':1440,'height':1080})
                    release_push.set()
                    expect(page.locator('#model-participant-message')).to_contain_text('Model configuration save failed',timeout=30000)
                    expect(page.locator('#create-experiment-hint')).to_contain_text('Model settings were not applied: Model configuration save failed')
                    expect(page.locator('#experiment-dialog')).to_be_visible()
                    expect(page.locator('#runs tr')).to_have_count(0)
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    fail_save[0] = False
                    page.locator('#model-participant-key').fill('replacement-key')
                    page.locator('#model-config-panel').scroll_into_view_if_needed()
                    page.screenshot(path=str(destination/'model-settings.png'))
                    page.get_by_role('button',name='Apply settings',exact=True).click()
                    expect(page.locator('#model-participant-message')).to_contain_text('Applied.',timeout=30000)
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.get_by_role('button',name='Create experiment',exact=True).click()
                    assert create_entered.wait(10)
                    expect(page.locator('#experiment-save-push')).to_contain_text('not needed')
                    expect(page.locator('#experiment-save-record')).to_contain_text('Saving experiment')
                    expect(page.locator('#experiment-save-status')).to_contain_text('67%')
                    release_create.set()
                    expect(page.locator('#experiment-dialog')).not_to_be_visible(timeout=30000)
                    assert ev.read_json(roots[9403]/'configs/cli.json')['api_key']=='replacement-key'
                    assert ev.read_json(roots[9403]/'configs/cli.json')['model']=='updated-model'
                    assert ev.read_json(roots[9403]/'configs/cli.json')['network_policy']['disallow']==['10.0.0.1']
                    expect(page.locator('#samples-context')).to_contain_text('openai / updated-model')
                    page.reload();expect(page.locator('#samples-context')).to_contain_text('openai / updated-model',timeout=30000)
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
                    page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                    page.locator('#model-participant-read').click()
                    expect(page.locator('#model-participant-model')).to_have_value('updated-model',timeout=30000)
                    # An API/client update after Pull must not make an untouched
                    # browser draft overwrite the new settings or fail creation.
                    config_path = roots[9403]/'configs/cli.json'
                    config = ev.read_json(config_path)
                    config['model'] = 'externally-updated-model'
                    ev.write_json(config_path, config)
                    page.get_by_role('button',name='Create experiment',exact=True).click()
                    expect(page.locator('#experiment-dialog')).not_to_be_visible(timeout=30000)
                    assert ev.read_json(config_path)['model'] == 'externally-updated-model'
                    # Start with no guest revision/token and Apply a typed draft
                    # directly. The background read must not replace that draft.
                    page.reload()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#new-experiment').click()
                    page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                    expect(page.locator('#model-participant-model')).to_be_enabled()
                    expect(page.locator('#model-participant-url')).to_have_value('https://models.example/v1')
                    # A failed first Apply of unchanged defaults stays retryable.
                    fail_save[0]=True
                    page.get_by_role('button',name='Apply settings',exact=True).click()
                    expect(page.locator('#model-participant-message')).to_contain_text('Model configuration save failed',timeout=30000)
                    expect(page.locator('#model-participant-apply')).to_be_enabled()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    fail_save[0]=False
                    page.get_by_role('button',name='Apply settings',exact=True).click()
                    expect(page.locator('#model-participant-message')).to_contain_text('Applied.',timeout=30000)
                    # Reset again so the typed draft uses the background read.
                    page.reload()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#new-experiment').click()
                    page.get_by_role('tab',name='Cyber-agent-flow',exact=True).click()
                    page.locator('#model-participant-model').fill('entered-without-pull')
                    page.locator('#model-participant-key').fill('direct-save-key')
                    before_dialogs=len(dialogs)
                    page.get_by_role('button',name='Apply settings',exact=True).click()
                    expect(page.locator('#model-participant-message')).to_contain_text('Applied.',timeout=30000)
                    expect(page.locator('#model-participant-model')).to_have_value('entered-without-pull')
                    expect(page.locator('#model-participant-apply')).to_be_disabled()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    assert len(dialogs)==before_dialogs
                    assert ev.read_json(config_path)['model']=='entered-without-pull'
                    assert ev.read_json(config_path)['api_key']=='direct-save-key'
                    assert 'direct-save-key' not in page.locator('#console-output').inner_text()
                    assert not errors,errors
                    browser.close()
            finally:
                release_push.set(); release_create.set(); dashboard.close()
    print('PASS: local save/VM percentage/experiment progress, failure/retry, hidden keys and sample execution, mobile layout')
    print(destination)

if __name__=='__main__': main()
