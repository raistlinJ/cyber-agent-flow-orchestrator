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
        ev.write_json(roots[9403]/'kali_tools.default.json', {'tools': [
            {'name':'nmap','command':'nmap','description':'Inspect target services.'},
            {'name':'shell_dangerous','command':'bash','allow_args':True,'description':'Run unrestricted shell commands.'},
        ]})
        (roots[9403]/'tools_docs').mkdir()
        (roots[9403]/'tools_docs/shell_dangerous.md').write_text('# Dangerous shell\nUse only in the isolated participant.\n')
        (roots[9403]/'mcp_client.py').write_text("_TOOL_DOC_MAP = {'shell_dangerous': 'tools_docs/shell_dangerous.md'}\n")
        generated=roots[9403]/'plugins/mcp_tools/http_probe';generated.mkdir(parents=True)
        (generated/'probe.py').write_text('print("ok")\n')
        ev.write_json(generated/'manifest.json', {'name':'http_probe','command':'python3','args':['probe.py'],'description':'Probe HTTP services.'})
        playbooks=roots[9403]/'plugins/playbooks';playbooks.mkdir(parents=True)
        (playbooks/'web-triage.md').write_text('# Web triage\nInspect the HTTP response first.\n')
        (roots[9402]/'.scenarioforge.env').write_text('CORETG_AI_PROVIDER=litellm\nCORETG_AI_BASE_URL=https://models.example/v1\nCORETG_AI_MODEL=scenario-model\nCORE_HOST=10.0.0.2\n')
        patch.setattr(guest, 'network_scope', lambda url,config:dict(provider_host='models.example',excluded_targets=['192.0.2.8','192.0.2.1'],warnings=[]))
        patch.setattr(guest, 'LOCK', root/'guest-maintenance.lock')
        patch.setattr(guest, 'SECRETS', root/'guest-secrets')
        fail_save = [False]
        push_entered, release_push = threading.Event(), threading.Event()
        create_entered, release_create = threading.Event(), threading.Event()
        tool_reads=[]
        class Agent(original):
            def call(self, vmid, op, **data):
                if op in ('model_config','write'):
                    self.authorize(['guest','exec',vmid])
                    if data.get('action') == 'tools':tool_reads.append(vmid)
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
                kwargs.pop('judge',None)
                kwargs.pop('controls',None)
                kwargs.pop('provide_progressive_hints',None)
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
                    dialog_handler=lambda dialog:(dialogs.append(dialog.message),dialog.accept())
                    page.on('dialog',dialog_handler)
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
                    expect(page.locator('#participant-tools-status')).to_contain_text('Loaded 7 tools and 2 CAF guidance assets',timeout=30000)
                    assert page.evaluate('judgeSettings().enabled && judgeSettings().use_participant_model')
                    before_calls=len(tool_reads)
                    page.evaluate("csrfToken='expired-session-token'")
                    recovered=page.evaluate("async()=>{const r=await apiFetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify({role:'participant',action:'tools'})});return {status:r.status,data:await r.json()};}")
                    assert recovered['status']==200 and len(recovered['data']['tools'])==7
                    assert len(tool_reads)==before_calls+1  # CSRF rejection never reaches the guest.
                    assert 'expired-session-token' not in page.locator('#console-output').inner_text()
                    page.locator('[data-wizard-step=caf]').click()
                    expect(page.locator('#experiment-builder #model-config-panel')).to_be_visible()
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
                    page.locator('[data-wizard-step=limits]').click()
                    for key in ['repetitions','max_turns','wall_seconds','tool_timeout','context_window']:
                        field=page.locator('#eval-'+key)
                        name=field.locator('..').locator('label').first.inner_text()
                        before=field.input_value()
                        field.locator('..').get_by_role('button',name='About '+name,exact=True).click()
                        expect(page.locator('#resource-info-popover')).to_contain_text('Minimum: '+field.evaluate('(element)=>Number(element.min).toLocaleString()'))
                        expect(page.locator('#resource-info-popover')).to_contain_text('Maximum: '+field.evaluate('(element)=>Number(element.max).toLocaleString()'))
                        assert len(page.locator('#resource-info-popover p').first.inner_text())>60
                        assert field.input_value()==before
                        if key=='tool_timeout':page.screenshot(path=str(destination/'trial-limit-help.png'))
                        page.keyboard.press('Escape')
                    page.locator('[data-wizard-step=tasks]').click()
                    page.locator('[data-wizard-step=tools]').click()
                    expect(page.locator('#participant-tools-status')).to_contain_text('Loaded 7 tools and 2 CAF guidance assets',timeout=30000)
                    page.locator('#add-condition').click()
                    expect(page.locator('.condition-tools .condition-tool-option')).to_have_count(7)
                    expect(page.locator('.condition-guidance-assets .condition-tool-option')).to_have_count(2)
                    expect(page.locator('.tool-risk.dangerous')).to_have_text('dangerous')
                    expect(page.locator('.tool-risk.interactive')).to_have_count(4)
                    expect(page.locator('.tool-origin',has_text='generated')).to_have_count(1)
                    expect(page.locator('.tool-origin',has_text='guide')).to_have_count(1)
                    expect(page.get_by_role('tab',name='Configuration settings',exact=True)).to_have_count(0)
                    expect(page.get_by_label('Tool catalog',exact=True)).to_have_count(0)
                    page.locator('#debug-console').evaluate('(element)=>element.open=false')
                    before_tools=page.evaluate('experimentControls().conditions[0].tools')
                    page.get_by_role('button',name='About shell_dangerous',exact=True).click()
                    expect(page.locator('#resource-info-popover')).to_be_visible()
                    expect(page.locator('#resource-info-popover')).to_contain_text('Run unrestricted shell commands.')
                    page.screenshot(path=str(destination/'tool-description.png'))
                    assert page.evaluate('experimentControls().conditions[0].tools')==before_tools
                    page.keyboard.press('Escape')
                    expect(page.locator('#resource-info-popover')).to_be_hidden()

                    page.get_by_label('Search available tools').fill('probe')
                    expect(page.locator('.condition-tools .condition-tool-option')).to_have_count(1)
                    page.get_by_label('Search available tools').fill('')
                    page.get_by_role('tab',name='Guidance & documents',exact=True).click()
                    guidance=page.locator('.condition-guidance-assets')
                    guidance.get_by_role('button',name='Select visible',exact=True).click()
                    all_docs=page.evaluate('experimentControls().conditions[0].guidance_assets')
                    assert len(all_docs)==2
                    page.get_by_label('Search CAF guidance').fill('web-triage')
                    guidance.get_by_role('button',name='Clear visible',exact=True).click()
                    hidden_docs=page.evaluate('experimentControls().conditions[0].guidance_assets')
                    assert len(hidden_docs)==1 and 'playbook:web-triage' not in hidden_docs
                    guidance.get_by_role('button',name='Select visible',exact=True).click()
                    assert page.evaluate('experimentControls().conditions[0].guidance_assets')==all_docs
                    page.get_by_label('Search CAF guidance').fill('')
                    page.get_by_label('Filter guidance types').select_option(label='Tool guide')
                    guidance.get_by_role('button',name='Clear visible',exact=True).click()
                    assert page.evaluate('experimentControls().conditions[0].guidance_assets')==['playbook:web-triage']
                    page.get_by_label('Filter guidance types').select_option('')
                    before_docs=page.evaluate('experimentControls().conditions[0].guidance_assets')
                    page.get_by_role('button',name='About web-triage',exact=True).click()
                    expect(page.locator('#resource-info-popover')).to_contain_text('playbook.md')
                    assert page.evaluate('experimentControls().conditions[0].guidance_assets')==before_docs
                    page.get_by_role('button',name='Close description',exact=True).click()

                    controls=page.evaluate('experimentControls()')
                    assert controls['conditions'][0]['source']=='participant-vm'
                    assert 'shell_dangerous' in controls['conditions'][0]['tools']
                    assert controls['conditions'][0]['guidance_assets']==['playbook:web-triage']
                    assert controls['auto_approve_dangerous'] is True
                    page.get_by_role('tab',name='Tools',exact=True).click()
                    page.locator('.condition-tools .condition-tool-option').filter(has_text='nmap').locator('input').uncheck()
                    page.locator('#load-participant-tools').click()
                    expect(page.locator('#participant-tools-status')).to_contain_text('Loaded 7 tools and 2 CAF guidance assets',timeout=30000)
                    assert 'nmap' not in page.evaluate('experimentControls().conditions[0].tools')
                    assert page.evaluate('experimentControls().conditions[0].guidance_assets')==['playbook:web-triage']
                    page.locator('.condition-tools').evaluate('(element)=>element.scrollIntoView({block:"center"})')
                    page.screenshot(path=str(destination/'condition-tools.png'))
                    page.locator('#add-condition').click()
                    expect(page.locator('.condition-choice')).to_have_count(2)
                    expect(page.locator('#condition-panel-0')).to_be_hidden()
                    expect(page.locator('#condition-panel-1')).to_be_visible()
                    assert 'nmap' not in page.evaluate('experimentControls().conditions[1].tools')
                    expect(page.locator('#condition-overview')).to_contain_text('2')
                    expect(page.locator('#condition-name-0')).to_have_value('Configuration 1')
                    expect(page.locator('#condition-name-1')).to_have_value('Configuration 2')
                    expect(page.locator('#condition-name-1')).to_have_attribute('readonly','')
                    page.get_by_role('button',name='Rename run configuration Configuration 2',exact=True).click()
                    page.locator('#condition-name-1').fill('Configuration 1')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#condition-name-1').fill('Guided copy')
                    page.locator('#condition-name-1').press('Enter')
                    expect(page.locator('#condition-name-1')).to_have_attribute('readonly','')
                    expect(page.locator('#condition-name-1')).to_have_value('Guided copy')
                    assert page.evaluate('experimentControls().conditions[1].name')=='Guided copy'
                    assert page.evaluate('experimentControls().conditions[1].id')=='configuration-2'
                    page.get_by_role('button',name='Rename run configuration Guided copy',exact=True).click()
                    page.locator('#condition-name-1').fill('Canceled name')
                    page.locator('#condition-name-1').press('Escape')
                    expect(page.locator('#condition-name-1')).to_have_value('Guided copy')
                    page.get_by_role('button',name='Rename run configuration Guided copy',exact=True).click()
                    page.locator('#condition-name-1').fill('Guided copy')
                    page.get_by_role('button',name='Save name for Guided copy',exact=True).click()
                    expect(page.locator('#condition-name-1')).to_have_attribute('readonly','')
                    page.locator('#condition-picker').scroll_into_view_if_needed()
                    page.screenshot(path=str(destination/'configuration-names.png'))
                    page.locator('#condition-choice-0').click()
                    expect(page.locator('#condition-panel-0')).to_be_visible()
                    expect(page.locator('#condition-panel-1')).to_be_hidden()
                    page.get_by_role('button',name='Remove run configuration Guided copy',exact=True).click()
                    expect(page.locator('.condition-choice')).to_have_count(1)
                    expect(page.locator('#condition-choice-0')).to_have_attribute('aria-selected','true')
                    expect(page.locator('#condition-choice-0')).to_be_focused()
                    expect(page.get_by_text('What is a run configuration?',exact=True)).to_have_count(0)
                    page.locator('#add-condition').click()
                    expect(page.locator('.condition-choice')).to_have_count(2)
                    page.locator('#condition-choice-1').press('Delete')
                    expect(page.locator('.condition-choice')).to_have_count(1)
                    expect(page.locator('#condition-choice-0')).to_be_focused()
                    page.get_by_role('tab',name='Guidance & documents',exact=True).click()
                    expect(page.locator('.condition-guidance-assets input[type=checkbox]').last).to_be_checked()
                    page.locator('.condition-guidance-assets').evaluate('(element)=>element.scrollIntoView({block:"center"})')
                    page.screenshot(path=str(destination/'condition-guidance.png'))
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('.condition-guidance-assets').evaluate('(element)=>element.scrollIntoView({block:"center"})')
                    page.screenshot(path=str(destination/'condition-guidance-mobile.png'))
                    page.set_viewport_size({'width':1440,'height':1080})
                    page.locator('.condition-execution-settings summary').click()
                    page.locator('#auto-approve-dangerous').uncheck()
                    assert page.evaluate('experimentControls().auto_approve_dangerous') is False
                    page.locator('[data-wizard-step=caf]').click()
                    page.locator('#model-participant-model').fill('temporary-edit')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('#model-participant-model').fill('custom-model')
                    expect(page.locator('#model-participant-apply')).to_be_disabled()
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    expect(page.locator('#model-config-scenarioforge')).to_have_count(0)
                    expect(page.locator('.model-card-heading #model-participant-read')).to_be_visible()
                    page.locator('#model-participant-model').fill('')
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('[data-wizard-step=overview]').click()
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    page.locator('[data-wizard-step=caf]').click()
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
                    assert page.locator('#experiment-save-progress').evaluate('(n)=>n.getBoundingClientRect().right<=innerWidth')
                    page.screenshot(path=str(destination/'saving-progress-mobile.png'))
                    page.set_viewport_size({'width':1440,'height':1080})
                    release_push.set()
                    expect(page.locator('#model-participant-message')).to_contain_text('Model configuration save failed',timeout=30000)
                    expect(page.locator('#create-experiment-hint')).to_contain_text('Model settings were not applied: Model configuration save failed')
                    expect(page.locator('#experiment-builder')).to_be_visible()
                    expect(page.locator('#runs tr')).to_have_count(0)
                    expect(page.locator('#create-experiment')).to_be_disabled()
                    fail_save[0] = False
                    page.locator('#model-participant-key').fill('replacement-key')
                    page.locator('#model-config-panel').scroll_into_view_if_needed()
                    page.screenshot(path=str(destination/'model-settings.png'))
                    page.get_by_role('button',name='Apply settings',exact=True).click()
                    expect(page.locator('#model-participant-message')).to_contain_text('Applied.',timeout=30000)
                    expect(page.locator('#create-experiment')).to_be_enabled()
                    page.locator('[data-wizard-step=review]').click()
                    expect(page.locator('#wizard-review')).to_contain_text('updated-model')
                    assert 'replacement-key' not in page.locator('#wizard-review').inner_text()
                    page.screenshot(path=str(destination/'wizard-review.png'))
                    page.get_by_role('button',name='Create experiment',exact=True).click()
                    assert create_entered.wait(10)
                    expect(page.locator('#experiment-save-push')).to_contain_text('not needed')
                    expect(page.locator('#experiment-save-record')).to_contain_text('Saving experiment')
                    expect(page.locator('#experiment-save-status')).to_contain_text('67%')
                    release_create.set()
                    expect(page.locator('#experiment-builder')).not_to_be_visible(timeout=30000)
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
                    expect(page.locator('#participant-tools-status')).to_contain_text('Loaded 7 tools and 2 CAF guidance assets',timeout=30000)
                    restored=page.evaluate('experimentControls().conditions')
                    assert len(restored)==1 and 'nmap' not in restored[0]['tools']
                    assert restored[0]['guidance_assets']==['playbook:web-triage']
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert page.locator('#experiment-builder').evaluate('(n)=>n.getBoundingClientRect().width<=innerWidth')
                    page.screenshot(path=str(destination/'model-settings-mobile.png'),full_page=True)
                    page.locator('#wizard-step-select').select_option('caf')
                    page.locator('#model-participant-read').click()
                    expect(page.locator('#model-participant-model')).to_have_value('updated-model',timeout=30000)
                    # An API/client update after Pull must not make an untouched
                    # browser draft overwrite the new settings or fail creation.
                    config_path = roots[9403]/'configs/cli.json'
                    config = ev.read_json(config_path)
                    config['model'] = 'externally-updated-model'
                    ev.write_json(config_path, config)
                    page.locator('#wizard-step-select').select_option('review')
                    page.get_by_role('button',name='Create experiment',exact=True).click()
                    expect(page.locator('#experiment-builder')).not_to_be_visible(timeout=30000)
                    assert ev.read_json(config_path)['model'] == 'externally-updated-model'
                    # Start with no guest revision/token and Apply a typed draft
                    # directly. The background read must not replace that draft.
                    page.reload()
                    expect(page.locator('#new-experiment')).to_be_enabled(timeout=30000)
                    page.locator('#new-experiment').click()
                    page.locator('#wizard-step-select').select_option('caf')
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
                    expect(page.locator('#experiment-builder')).to_be_visible(timeout=30000)
                    expect(page.locator('#wizard-step-select')).to_be_enabled(timeout=30000)
                    page.locator('#wizard-step-select').select_option('caf')
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
                    # Defaults retain explicit empty selections and independent configurations.
                    page.locator('#wizard-step-select').select_option('tools')
                    page.get_by_role('tab',name='Tools',exact=True).click()
                    page.locator('.condition-tools').get_by_role('button',name='Clear visible',exact=True).click()
                    page.get_by_role('tab',name='Guidance & documents',exact=True).click()
                    page.locator('.condition-guidance-assets').get_by_role('button',name='Clear visible',exact=True).click()
                    page.locator('#add-condition').click()
                    page.locator('#condition-panel-1 .condition-tools').get_by_role('button',name='Select visible',exact=True).click()
                    page.reload()
                    expect(page.locator('#participant-tools-status')).to_contain_text('Loaded 7 tools and 2 CAF guidance assets',timeout=30000)
                    restored=page.evaluate('experimentControls().conditions')
                    assert len(restored)==2
                    assert restored[0]['tools']==[] and restored[0]['guidance_assets']==[]
                    assert len(restored[1]['tools'])==7 and restored[1]['guidance_assets']==[]
                    # Stored preferences do not introduce resources absent from the loaded catalog.
                    page.evaluate("snapshot.condition_catalogs['participant-vm'].tools=['nmap'];snapshot.participant_guidance_assets=[];customConditions=[];resourceDefaultsLoaded=false;ensureParticipantCondition()")
                    restored=page.evaluate('experimentControls().conditions')
                    assert restored[0]['tools']==[] and restored[1]['tools']==['nmap']
                    # Wait for the fresh catalog before restoring against cached data.
                    page.evaluate('customConditions=[];resourceDefaultsLoaded=false;participantCatalogReady=false;ensureParticipantCondition()')
                    assert page.evaluate('customConditions.length')==0
                    page.evaluate('participantCatalogReady=true;ensureParticipantCondition()')
                    assert len(page.evaluate('experimentControls().conditions'))==2
                    # Another account never inherits the first account's defaults.
                    page.evaluate("sessionAccount=['pve','another@pve'];customConditions=[];resourceDefaultsLoaded=false;ensureParticipantCondition()")
                    assert page.evaluate('customConditions.length')==0
                    page.locator('[data-route=experiments]').click()
                    expect(page.locator('#delete-experiments')).to_be_disabled()
                    existing=page.locator('#runs tr').count()
                    assert existing>=2
                    # Active rows are excluded from select-all and retain a disabled checkbox.
                    page.evaluate("snapshot.runs.push({...snapshot.runs[0],output:'/fixture/active-selection-test',recorded_status:'queued',coordinator_active:true,sample_progress:null});renderExperiments(snapshot)")
                    active=page.get_by_label('Select experiment active-selection-test',exact=True)
                    expect(active).to_be_disabled()
                    first=page.locator('#runs .experiment-selector:enabled').first
                    first.check()
                    assert page.locator('#select-experiments').evaluate('(element)=>element.indeterminate')
                    first_id=first.get_attribute('data-run-id')
                    page.evaluate('renderExperiments(snapshot)')
                    expect(page.get_by_label('Select experiment '+first_id,exact=True)).to_be_checked()
                    page.locator('#select-experiments').check()
                    expect(page.locator('#experiment-selection-count')).to_have_text(str(existing)+' selected')
                    expect(active).not_to_be_checked()
                    page.locator('#debug-console').evaluate('(element)=>element.open=false')
                    page.screenshot(path=str(destination/'experiments-selected-mobile.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.remove_listener('dialog',dialog_handler)
                    dismiss_handler=lambda dialog:(dialogs.append(dialog.message),dialog.dismiss())
                    page.on('dialog',dismiss_handler)
                    page.locator('#delete-experiments').click()
                    assert 'permanently removes their saved inputs' in dialogs[-1]
                    expect(page.locator('#runs tr')).to_have_count(existing+1)
                    page.remove_listener('dialog',dismiss_handler)
                    page.on('dialog',dialog_handler)
                    page.locator('#delete-experiments').click()
                    expect(page.locator('#sample-message')).to_have_text('Deleted '+str(existing)+' experiments.',timeout=30000)
                    expect(page.locator('#runs tr')).to_have_count(0)
                    expect(page.locator('#delete-experiments')).to_be_disabled()
                    expect(page.locator('#no-runs')).to_be_visible()
                    assert not list(workspace.runs.glob('*/workflow.json'))
                    assert not errors,errors
                    browser.close()
            finally:
                release_push.set(); release_create.set(); dashboard.close()
    print('PASS: local save/VM percentage/experiment progress, failure/retry, hidden keys and sample execution, mobile layout')
    print(destination)

if __name__=='__main__': main()
