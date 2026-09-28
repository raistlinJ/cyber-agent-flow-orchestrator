"""Contract tests against an isolated HTTPS PVE API double; never a live host."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs

import pytest
import yaml

from cyber_agent_flow_orchestrator.auth import Auth
from cyber_agent_flow_orchestrator.pve_auth import PVEProvider
from cyber_agent_flow_orchestrator.tls import create_certificate, context, settings
from https_fixture import secure_server, PASSWORD
from test_web import request


@contextmanager
def pve_server(tmp_path):
    cert, key = tmp_path / 'pve.pem', tmp_path / 'pve.key'
    create_certificate(cert, key, ['localhost'])
    state = {'groups': 'caf-orchestrator', 'enable': 1, 'expire': 0, 'tfa': False,
             'requests': [], 'failure': None, 'realm_otp': False, 'resources': {}, 'permissions': {}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, code, data):
            body = json.dumps({'data': data}).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            if code == 302:
                self.send_header('Location', 'https://localhost:1/stolen')
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            data = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
            state['requests'].append((self.path, data, self.headers.get('Cookie')))
            if state['failure']:
                return self.respond(state['failure'], 'PRIVATE UPSTREAM BODY')
            if self.path != '/api2/json/access/ticket':
                return self.respond(404, None)
            challenge = data.get('tfa-challenge')
            if challenge:
                if challenge != 'PVE:!tfa!opaque' or data.get('password') != 'totp:123456':
                    return self.respond(401, None)
            elif data.get('password') != PASSWORD or (state['realm_otp'] and data.get('otp') != '123456'):
                return self.respond(401, None)
            ticket = 'PVE:!tfa!opaque' if state['tfa'] and not challenge else 'PVE:' + data['username'] + ':opaque'
            result = {'ticket': ticket, 'username': data['username']}
            if state['tfa'] and not challenge:
                result['NeedTFA'] = 1
            self.respond(200, result)
        def do_GET(self):
            state['requests'].append((self.path, None, self.headers.get('Cookie')))
            if state['failure']:
                return self.respond(state['failure'], 'PRIVATE UPSTREAM BODY')
            cookie = self.headers.get('Cookie', '')
            if not cookie.startswith('PVEAuthCookie=PVE:') or not cookie.endswith(':opaque'):
                return self.respond(401, None)
            username = cookie[len('PVEAuthCookie=PVE:'):-len(':opaque')]
            if self.path == '/api2/json/access/users?full=1':
                return self.respond(200, [{'userid': username, 'groups': state['groups'],
                                          'enable': state['enable'], 'expire': state['expire']}])
            if self.path == '/api2/json/cluster/resources?type=vm':
                return self.respond(200, state['resources'].get(username, []))
            if self.path.startswith('/api2/json/access/permissions?path='):
                path = self.path.split('path=', 1)[1]
                vmid = int(path.rsplit('/', 1)[1])
                allowed = any(v.get('vmid') == vmid for v in state['resources'].get(username, []))
                permissions = state['permissions'].get((username, vmid), {'VM.Audit': 0} if allowed else {})
                return self.respond(200, {path: permissions})
            self.respond(404, None)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.socket = context({'certificate': cert, 'private_key': key}).wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'provider': 'pve', 'url': f'https://localhost:{server.server_port}', 'ca_file': str(cert),
              'required_group': 'caf-orchestrator', 'realms': ['pve', 'pam']}
    try:
        yield state, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def pve(tmp_path):
    with pve_server(tmp_path) as value:
        yield value


def make_auth(pve, **kwargs):
    return Auth(provider=PVEProvider(pve[1]), **kwargs)


def test_pve_login_group_gate_and_no_password_retention(pve):
    state, _ = pve
    auth = make_auth(pve)
    assert auth.login('operator@pve', 'wrong', 'peer') is None
    state['groups'] = 'caf-orchestrator-extra,other'
    assert auth.login('operator@pve', PASSWORD, 'peer') is None
    state['groups'] = 'other,caf-orchestrator'
    token = auth.login('operator@pve', PASSWORD, 'peer')
    session = auth.session(token)
    assert session['role'] == 'orchestrator' and session['provider'] == 'pve'
    assert session['username'] == 'operator@pve'
    assert 'credential' not in session and 'ticket' not in session
    assert PASSWORD not in repr(auth.sessions) + repr(auth.challenges)
    assert auth.absolute == 7200
    assert state['requests'][-1][2].startswith('PVEAuthCookie=')


@pytest.mark.parametrize('change', [{'groups': ''}, {'enable': 0}, {'expire': 1}])
def test_removing_group_disabling_or_expiring_account_revokes_session(pve, change):
    auth = make_auth(pve)
    token = auth.login('operator@pve', PASSWORD, 'peer')
    assert auth.session(token)
    pve[0].update(change)
    assert auth.session(token) is None
    assert not auth.sessions


@pytest.mark.parametrize('failure', [401, 403, 500, 302])
def test_upstream_failure_never_grants_access_or_follows_redirect(pve, failure):
    auth = make_auth(pve)
    token = auth.login('operator@pve', PASSWORD, 'peer')
    pve[0]['failure'] = failure
    if failure in (401, 403):
        assert auth.session(token) is None
    else:
        with pytest.raises(ValueError, match='service unavailable') as error:
            auth.session(token)
        assert 'PRIVATE' not in str(error.value)


def test_tls_ca_and_hostname_verification_cannot_be_disabled(pve):
    config = dict(pve[1])
    config.pop('ca_file')
    with pytest.raises(ValueError, match='service unavailable'):
        PVEProvider(config).authenticate('operator@pve', PASSWORD)
    config = dict(pve[1], url=pve[1]['url'].replace('localhost', '127.0.0.1'))
    with pytest.raises(ValueError, match='service unavailable'):
        PVEProvider(config).authenticate('operator@pve', PASSWORD)
    assert not pve[0]['requests']


def test_unapproved_realms_and_invalid_usernames_never_reach_pve(pve):
    auth = make_auth(pve)
    for name in ['operator', 'operator@unknown', 'operator@pve\n', 'operator@pve!token']:
        assert auth.login(name, PASSWORD, 'peer') is None
    assert not pve[0]['requests']


def test_totp_requires_completion_single_use_and_peer_binding(pve):
    pve[0]['tfa'] = True
    auth = make_auth(pve)
    challenge = auth.login('operator@pve', PASSWORD, 'peer')
    assert challenge['requires_totp'] and not auth.sessions
    assert 'PVE:' not in json.dumps(challenge)
    assert auth.session(challenge['challenge_id']) is None
    token = auth.login('', '', 'peer', otp='123456', challenge_id=challenge['challenge_id'])
    assert auth.session(token)
    assert auth.login('', '', 'peer', otp='123456', challenge_id=challenge['challenge_id']) is None
    pending = auth.login('operator@pve', PASSWORD, 'peer')
    assert auth.login('', '', 'other-peer', otp='123456', challenge_id=pending['challenge_id']) is None
    pending = auth.login('operator@pve', PASSWORD, 'peer')
    assert auth.login('', '', 'peer', otp='wrong', challenge_id=pending['challenge_id']) is None
    assert auth.login('', '', 'peer', otp='123456', challenge_id=pending['challenge_id']) is None


def test_challenge_expiry_and_group_check_after_second_factor(pve):
    pve[0]['tfa'] = True
    now = [0]
    auth = make_auth(pve, clock=lambda: now[0])
    pending = auth.login('operator@pve', PASSWORD, 'peer')
    now[0] = 180
    assert auth.login('', '', 'peer', otp='123456', challenge_id=pending['challenge_id']) is None
    pending = auth.login('operator@pve', PASSWORD, 'peer')
    pve[0]['groups'] = ''
    assert auth.login('', '', 'peer', otp='123456', challenge_id=pending['challenge_id']) is None
    assert not auth.sessions


def test_realm_enforced_otp_forwarded(pve):
    pve[0]['realm_otp'] = True
    auth = make_auth(pve)
    assert auth.login('operator@pve', PASSWORD, 'peer') is None
    assert auth.login('operator@pve', PASSWORD, 'peer', otp='123456')
    assert pve[0]['requests'][-2][1]['otp'] == '123456'


def test_logout_during_group_check_cannot_restore_session(pve):
    auth = make_auth(pve)
    token = auth.login('operator@pve', PASSWORD, 'peer')
    original = auth.provider.validate
    def validate(record):
        result = original(record)
        auth.logout(token)
        return result
    auth.provider.validate = validate
    assert auth.session(token) is None


def test_https_webui_pve_login_totp_revocation_and_no_fallback(pve, tmp_path):
    class Dashboard:
        scoped = True
        def read(self, access):
            access.current()
            return {'private_lab_data': True}
    auth = make_auth(pve)
    with secure_server(Dashboard(), tmp_path / 'web', auth=auth) as server:
        assert json.loads(request(server, '/api/auth')[2])['provider'] == 'pve'
        # Even though the test fixture creates a local operator, PVE mode cannot use it.
        assert request(server, '/api/login', {'username': 'operator', 'password': PASSWORD})[0] == 401
        pve[0]['tfa'] = True
        code, headers, body = request(server, '/api/login', {'username': 'operator@pve', 'password': PASSWORD})
        assert code == 202 and 'Set-Cookie' not in headers
        pending = json.loads(body)
        assert 'PVE:' not in body.decode() and PASSWORD not in body.decode()
        assert request(server, '/api/status')[0] == 401
        data = {'challenge_id': pending['challenge_id'], 'otp': '123456'}
        assert request(server, '/api/login/totp', data, headers={'Origin': ''})[0] == 403
        code, headers, _ = request(server, '/api/login/totp', data)
        assert code == 200
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        assert 'PVE:' not in cookie
        assert request(server, '/api/status', cookie=cookie)[0] == 200
        session = json.loads(request(server, '/api/session', cookie=cookie)[2])
        assert session['role'] == 'orchestrator'
        pve[0]['failure'] = 500
        code, _, body = request(server, '/api/status', cookie=cookie)
        assert code == 503 and b'private_lab_data' not in body and b'PRIVATE' not in body
        assert request(server, '/api/logout', {}, cookie=cookie, headers={'X-CSRF-Token': session['csrf']})[0] == 200
        assert request(server, '/api/status', cookie=cookie)[0] == 401
        pve[0]['failure'] = None
        pve[0]['tfa'] = False
        code, headers, _ = request(server, '/api/login', {'username': 'operator@pve', 'password': PASSWORD})
        assert code == 200
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        pve[0]['groups'] = ''
        assert request(server, '/api/status', cookie=cookie)[0] == 401


def test_pve_config_requires_explicit_group_and_rejects_unsafe_options(tmp_path):
    path = tmp_path / 'web.yaml'
    base = dict(version=1, public_url='https://localhost:8443', certificate='cert.pem', private_key='key.pem')
    auth = dict(provider='pve', url='https://pve.lab:8006', required_group='caf-orchestrator', ca_file='pve-ca.pem')
    path.write_text(yaml.safe_dump(dict(base, auth=auth)))
    resolved = settings(path)
    assert resolved['auth']['ca_file'] == str(tmp_path / 'pve-ca.pem')
    assert 'users_file' not in resolved
    for change in [dict(url='http://pve.lab:8006'), dict(url='https://pve.lab:0'), dict(url='https://user:pass@pve.lab:8006'),
                   dict(url='https://pve.lab:8006/path'), dict(required_group=''), dict(realms=[]),
                   dict(verify_tls=False), dict(provider='unknown')]:
        path.write_text(yaml.safe_dump(dict(base, auth=dict(auth, **change))))
        with pytest.raises(ValueError): settings(path)
    path.write_text(yaml.safe_dump(dict(base, auth=auth, users_file='users.json')))
    with pytest.raises(ValueError, match='no local fallback'): settings(path)
    auth.pop('required_group')
    path.write_text(yaml.safe_dump(dict(base, auth=auth)))
    with pytest.raises(ValueError): settings(path)
