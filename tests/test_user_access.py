"""Multi-user isolation using real HTTPS/PVE clients and fake guest operations."""
import json
from pathlib import Path
import socket
import threading
import time

import pytest

from cyber_agent_flow_eval.proxmox import GuestAgent, authorized_operations
from cyber_agent_flow_orchestrator.access import AccessDenied
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.monitor import ProxmoxProbe
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from cyber_agent_flow_orchestrator import user_execution, service, workflow
from test_pve_auth import pve, make_auth
from test_workflow import lab
from https_fixture import secure_server, PASSWORD
from test_web import request


def vm(vmid, **extra):
    return dict(vmid=vmid, type='qemu', node=socket.gethostname().split('.')[0], name=f'vm-{vmid}', status='running', **extra)


def access(pve, username='operator@pve'):
    auth = make_auth(pve)
    token = auth.login(username, PASSWORD, username)
    return auth.access(token)


class Probe:
    def __init__(self, backend, access, calls):
        self.access, self.calls = access, calls
    def vm(self, vmid):
        self.access.require_vm(vmid)
        self.calls.append(('vm', self.access.username, vmid))
        return {'present': True, 'power': 'running', 'name': f'vm-{vmid}'}
    def guest(self, vmid, definition, units):
        self.access.qm(['guest', 'exec', vmid])
        self.calls.append(('guest', self.access.username, vmid))
        return {'application_present': True, 'processes': [], 'services': [], 'guest_monotonic_seconds': 10}


def observed(dashboard, user):
    for _ in range(100):
        result = dashboard.read(user)
        if result['checked_at']:
            return result
        time.sleep(.02)
    raise AssertionError('No observation completed')


def test_effective_vm_scope_templates_other_nodes_and_revocation(pve):
    state = pve[0]
    state['resources']['operator@pve'] = [vm(101, pool='alice'), dict(vm(102), node='another-node'), vm(103, template=1), dict(vm(104), type='lxc')]
    user = access(pve)
    assert [r['vmid'] for r in user.inventory()] == [101]
    user.require_vm(101)  # VM.Audit=0 is a propagation flag, still a grant.
    for value in (102, 103, 104, 999, True, '101'):
        with pytest.raises(AccessDenied): user.require_vm(value)
    state['permissions'][('operator@pve', 101)] = {}
    with pytest.raises(AccessDenied): user.require_vm(101)
    state['permissions'].clear()
    state['resources']['operator@pve'] = []
    with pytest.raises(AccessDenied): user.require_vm(101)


def test_transport_guard_runs_before_every_host_command(pve, monkeypatch):
    state = pve[0]
    state['resources']['operator@pve'] = [vm(101)]
    user = access(pve)
    calls = []
    class Result:
        returncode = 0
        stdout = '{}'
    monkeypatch.setattr('cyber_agent_flow_eval.proxmox.subprocess.run', lambda *a, **k: calls.append(a) or Result())
    with authorized_operations(user.qm):
        agent = GuestAgent({'command_timeout': 1})
    agent.qm(['guest', 'exec', 101])
    assert len(calls) == 1
    state['resources']['operator@pve'] = []
    with pytest.raises(AccessDenied): agent.qm(['guest', 'exec-status', 101, 1])
    assert len(calls) == 1
    assert GuestAgent({}).authorize is None  # Context restored for trusted admin runs.


def test_monitor_power_check_is_guarded_before_qm(pve, monkeypatch):
    user = access(pve)
    calls = []
    monkeypatch.setattr('cyber_agent_flow_orchestrator.monitor.subprocess.run', lambda *a, **k: calls.append(a))
    with pytest.raises(AccessDenied): ProxmoxProbe({}, user).vm(999)
    assert not calls


def test_roles_private_persistent_and_reject_forged_ids(pve, tmp_path):
    pve[0]['resources']['operator@pve'] = [vm(101), vm(102), vm(103)]
    alice, bob = access(pve), access(pve, 'bob@pve')
    first, second = Workspace(tmp_path, alice.username), Workspace(tmp_path, bob.username)
    roles = {'scenarioforge': 101, 'participant': 102, 'core': 103}
    first.save_roles(roles, alice)
    assert Workspace(tmp_path, alice.username).roles() == roles
    assert second.roles() == dict.fromkeys(roles)
    assert first.path.stat().st_mode & 0o077 == 0
    with pytest.raises(AccessDenied): second.save_roles(roles, bob)
    with pytest.raises(ValueError): first.save_roles(dict(roles, core=101), alice)
    with pytest.raises(ValueError): first.save_roles(dict(roles, username='bob@pve'), alice)
    for name in ('../alice', '/tmp/run', 'a/b', '..'):
        with pytest.raises(AccessDenied): second.run_path(name)
    second.run_path('alias').symlink_to(first.runs, target_is_directory=True)
    with pytest.raises(AccessDenied): second.run_path('alias')


def test_per_user_dashboard_cache_and_revocation(pve, lab, tmp_path):
    pve[0]['resources']['operator@pve'] = [vm(101), vm(102)]
    pve[0]['resources']['bob@pve'] = [vm(201), vm(202)]
    alice, bob = access(pve), access(pve, 'bob@pve')
    calls = []
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, calls))
    try:
        assert dash.read(alice)['roles'] == dict.fromkeys(('scenarioforge', 'participant', 'core'))
        dash.select(alice, {'scenarioforge': 101, 'participant': 102, 'core': None})
        dash.select(bob, {'scenarioforge': 201, 'participant': 202, 'core': None})
        assert {r['vmid'] for r in observed(dash, alice)['vms']} == {101, 102, None}
        assert {r['vmid'] for r in observed(dash, bob)['vms']} == {201, 202, None}
        assert all(n in ({101, 102} if name == alice.username else {201, 202}) for _, name, n in calls)
        pve[0]['resources']['operator@pve'] = []
        revoked = dash.read(alice)
        assert not [r for r in revoked['vms'] if r['vmid']]
        assert all(v is None for v in revoked['roles'].values())
        pve[0]['failure'] = 500
        with pytest.raises(ValueError): dash.read(bob)
    finally:
        dash.close()


def test_https_roles_csrf_and_cross_user_results(pve, lab, tmp_path):
    pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403)]
    pve[0]['resources']['bob@pve'] = [vm(201), vm(202)]
    auth = make_auth(pve)
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        alice = Workspace(dash.root, 'operator@pve')
        output = alice.run_path('private-run')
        workflow.run(lab[0], output, agent=lab[2])
        with secure_server(dash, tmp_path / 'web', auth=auth) as server:
            def login(name):
                code, headers, _ = request(server, '/api/login', {'username': name, 'password': PASSWORD})
                assert code == 200
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                session = json.loads(request(server, '/api/session', cookie=cookie)[2])
                return cookie, {'X-CSRF-Token': session['csrf']}
            ac, ah = login('operator@pve')
            bc, bh = login('bob@pve')
            roles = {'scenarioforge': 9402, 'participant': 9403, 'core': None}
            assert request(server, '/api/roles', roles, cookie=ac)[0] == 403
            assert request(server, '/api/roles', roles, cookie=bc, headers=bh)[0] == 403
            assert request(server, '/api/roles', roles, cookie=ac, headers=ah)[0] == 200
            assert request(server, '/api/runs/private-run/results', cookie=ac)[0] == 200
            code, _, body = request(server, '/api/runs/private-run/results', cookie=bc)
            assert code == 404 and b'fixture' not in body
            assert request(server, '/api/runs/%2e%2e/results', cookie=bc)[0] in (403, 404)
            assert json.loads(request(server, '/api/status', cookie=bc)[2])['roles'] == dict.fromkeys(roles)
            pve[0]['resources']['operator@pve'] = []
            assert request(server, '/api/roles', roles, cookie=ac, headers=ah)[0] == 403
    finally:
        dash.close()


def test_user_run_remaps_roles_private_inputs_and_installs_guard(pve, lab, tmp_path, monkeypatch):
    pve[0]['resources']['operator@pve'] = [vm(101), vm(102)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles({'scenarioforge': 101, 'participant': 102, 'core': None}, user)
    calls = []
    def execute(config, output, **kwargs):
        cfg, runtime, _, _ = load(config)
        assert runtime['backend']['app_vmid'] == 101
        assert runtime['backend']['participant_vmid'] == 102
        assert cfg['artifacts'][0]['vmid'] == 102
        assert Path(output).parent == workspace.runs
        assert GuestAgent({}).authorize is not None
        calls.append(config)
        return 0
    monkeypatch.setattr(service, 'run', execute)
    assert user_execution.run(lab[0], tmp_path / 'runs', 'trial', user) == 0
    assert calls
    assert user_execution.run(None, tmp_path / 'runs', 'trial', user, resume=True) == 0
    pve[0]['resources']['operator@pve'] = []
    with pytest.raises(AccessDenied): user_execution.run(None, tmp_path / 'runs', 'trial', user, resume=True)
    assert len(calls) == 2


def test_three_vm_authorization_shares_inventory_but_checks_each_acl(pve, monkeypatch):
    state = pve[0]
    state['resources']['operator@pve'] = [vm(101), vm(102), vm(103)]
    user = access(pve)
    state['requests'].clear()
    user.require_vms([101, 102, 103])
    paths = [row[0] for row in state['requests']]
    assert paths.count('/api2/json/cluster/resources?type=vm') == 1
    assert paths.count('/api2/json/access/users?full=1') == 2
    assert len([path for path in paths if '/access/permissions?' in path]) == 3
    assert len(paths) == 6  # Previously fifteen serial PVE calls for three roles.
    state['permissions'][('operator@pve', 103)] = {}
    with pytest.raises(AccessDenied):
        user.require_vms([101, 102, 103])
    state['permissions'].clear()
    original = user.provider.request
    def revoke_after_last_acl(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path.endswith('/vms/103'):
            state['groups'] = ''
        return response
    monkeypatch.setattr(user.provider, 'request', revoke_after_last_acl)
    with pytest.raises(AccessDenied):
        user.require_vms([101, 102, 103])


def test_three_selected_guests_do_not_block_https_status(pve, lab, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    pve[0]['resources']['operator@pve'] = [vm(101), vm(102), vm(103)]
    auth = make_auth(pve)
    started, release = threading.Event(), threading.Event()
    class SlowProbe(Probe):
        def guest(self, vmid, definition, units):
            started.set()
            if not release.wait(10):
                raise TimeoutError('Test probe was not released')
            return super().guest(vmid, definition, units)
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: SlowProbe(b, a, []))
    try:
        with secure_server(dash, tmp_path / 'web', auth=auth) as server:
            code, headers, _ = request(server, '/api/login', {'username': 'operator@pve', 'password': PASSWORD})
            assert code == 200
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            session = json.loads(request(server, '/api/session', cookie=cookie)[2])
            roles = dict(scenarioforge=101, participant=102, core=103)
            pve[0]['requests'].clear()
            assert request(server, '/api/roles', roles, cookie=cookie,
                           headers={'X-CSRF-Token': session['csrf']})[0] == 200
            assert len(pve[0]['requests']) == 9
            assert request(server, '/api/status', cookie=cookie)[0] == 200
            assert started.wait(3)
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    response = pool.submit(request, server, '/api/status', cookie=cookie).result(timeout=3)
                    assert response[0] == 200
                    body = json.loads(response[2])
                    assert body['roles'] == roles and body['refreshing']
                    assert not release.is_set()
                finally:
                    release.set()
    finally:
        release.set()
        dash.close()
