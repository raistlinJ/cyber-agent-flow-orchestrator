"""Optional browser check: pip install playwright, then run with Chrome installed.
Uses a simulated dashboard; does not connect to Proxmox or start an experiment.
"""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def sample():
    stamp = datetime.now(timezone.utc).isoformat()
    items = []
    for role, label, vmid, cmd, age in [
        ('scenarioforge', 'ScenarioForge', 9402, '/opt/scenarioforge/.venv/bin/python -m webapp.app_backend', 2184),
        ('participant', 'Cyber-agent-flow', 9403, '/opt/cyber-agent-flow/venv/bin/python /var/lib/caf-eval/cyber_agent_flow_eval/worker.py', 143),
        ('core', 'CoreVM', 9401, '/opt/core/venv/bin/core-daemon', 5321)]:
        items.append(dict(role=role, label=label, vmid=vmid, name={'core':'corevm','participant':'participant-vm','scenarioforge':'app-vm'}[role],
                          present=True, power='running', guest_access='reachable', application_present=True,
                          observed_at=stamp, services=[], processes=[{'pid': vmid-8200, 'command': cmd, 'elapsed_seconds': age}], jobs=[]))
    items[1]['processes'].append({'pid': 1602, 'command': '/usr/bin/nmap -sV 10.77.0.12', 'elapsed_seconds': 23})
    items[1]['jobs'] = [dict(run='artifact-study-001', stage='trial-000003', vmid=9403, unit='caf-eval-6db820a1',
                             command='/opt/cyber-agent-flow/venv/bin/python cyber_agent_flow_eval/worker.py attempt',
                             elapsed_seconds=143, live_state='running', elapsed_source='guest process start')]
    return {'checked_at':stamp, 'refreshing':False, 'poll_seconds':10, 'workflow_id':'artifact-study · simulated preview',
            'vms':items, 'errors':[], 'runs':[
                {'workflow_id':'artifact-study-001', 'recorded_status':'evaluating', 'coordinator_active':True,
                 'evaluation':{'planned_trials':4, 'summary':{'trials_observed':3,'verified_successes':1}}},
                {'workflow_id':'baseline-smoke-001', 'recorded_status':'completed', 'coordinator_active':False,
                 'evaluation':{'planned_trials':2, 'summary':{'trials_observed':2,'verified_successes':2}}}]}


class FakeDashboard:
    def __init__(self): self.value = sample()
    def read(self): return self.value


def main():
    dashboard = FakeDashboard()
    destination = Path(sys.argv[1]) if len(sys.argv)>1 else Path(tempfile.mkdtemp(prefix='caf-dashboard-qa-'))
    destination.mkdir(parents=True, exist_ok=True)
    from https_fixture import secure_server, PASSWORD
    with tempfile.TemporaryDirectory(prefix='caf-https-browser-') as temporary, secure_server(dashboard, Path(temporary)) as secure:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width':1440, 'height':1150}, device_scale_factor=1, ignore_https_errors=True)
            errors=[]
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(secure['origin'])
            page.wait_for_url('**/login')
            page.screenshot(path=str(destination/'login-desktop.png'), full_page=True)
            page.get_by_label('Username').fill('operator')
            page.get_by_label('Password').fill('incorrect-password')
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.get_by_text('Invalid username or password').wait_for()
            page.get_by_label('Password').fill(PASSWORD)
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_selector('.card')
            assert page.locator('.card').count()==3
            assert page.get_by_text('VM 9401 · corevm').count()==1
            assert page.locator('.command .badge').inner_text()=='RUNNING' or page.locator('.command .badge').inner_text()=='running'
            before=page.locator('.command .clock').inner_text()
            page.wait_for_timeout(1100)
            assert page.locator('.command .clock').inner_text()!=before
            page.screenshot(path=str(destination/'dashboard-desktop.png'), full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(destination/'dashboard-mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            dashboard.value['vms'][1]['processes'][0]['command']='<img src=x onerror="window.pwned=true">'
            dashboard.value['vms'][0].update(guest_access='unavailable', processes=[], guest_error='Guest agent not responding')
            dashboard.value['vms'][2].update(power='missing', guest_access='not checked', processes=[], application_present=None)
            dashboard.value['vms'][1]['jobs'][0]['live_state']='unconfirmed'
            page.locator('#refresh').click()
            page.get_by_text('Guest agent not responding').wait_for()
            assert page.locator('.card img').count()==0
            assert not page.evaluate('Boolean(window.pwned)')
            assert 'unconfirmed' in page.locator('.command .badge').inner_text().lower()
            cookies=page.context.cookies()
            session=next(c for c in cookies if c['name']=='__Host-caf_session')
            assert session['secure'] and session['httpOnly'] and session['sameSite']=='Strict'
            page.get_by_role('button', name='Sign out', exact=True).click()
            page.wait_for_url('**/login')
            assert page.request.get(secure['origin']+'/api/status').status==401
            assert not errors, errors
            browser.close()
    print('Browser checks passed: HTTPS login, invalid password, secure cookie, logout, desktop/mobile, elapsed clock, failure states and inert command text.')
    print(destination)


if __name__=='__main__': main()
