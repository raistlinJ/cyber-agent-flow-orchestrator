import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPSHandler, HTTPRedirectHandler

import pytest

from cyber_agent_flow_orchestrator.web import Dashboard
from test_workflow import lab
from test_monitor import Probe
from https_fixture import PASSWORD, secure_server


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@pytest.fixture
def server(lab, tmp_path):
    dashboard = Dashboard(lab[0], tmp_path / 'runs', probe=Probe())
    dashboard.refresh()
    with secure_server(dashboard, tmp_path / 'secure') as state:
        state['dashboard'] = dashboard
        yield state


def request(server, path, body=None, cookie=None, headers=None, method=None):
    extra = dict(headers or {})
    if cookie:
        extra['Cookie'] = cookie
    if body is not None:
        extra.setdefault('Origin', server['origin'])
        extra.setdefault('Content-Type', 'application/json')
    data = json.dumps(body).encode() if body is not None else None
    req = Request(server['origin'] + path, data=data, headers=extra, method=method)
    opener = build_opener(HTTPSHandler(context=server['ssl']), NoRedirect())
    try:
        response = opener.open(req, timeout=10)
    except HTTPError as response:
        return response.code, response.headers, response.read()
    return response.status, response.headers, response.read()


def login(server):
    code, headers, body = request(server, '/api/login', {'username': 'operator', 'password': PASSWORD})
    assert code == 200, body
    return headers['Set-Cookie'].split(';', 1)[0], headers['Set-Cookie']


def test_anonymous_api_blocked_and_login_assets_available(server):
    assert request(server, '/api/status')[0] == 401
    for path in ('/app.js', '/run', '/run.js', '/run_render.js', '/run_windows.js'):
        assert request(server, path)[0] == 401
    code, headers, _ = request(server, '/')
    assert code == 302 and headers['Location'] == '/login'
    assert b'Welcome back.' in request(server, '/login')[2]
    assert request(server, '/login.js')[0] == 200
    assert request(server, '/style.css')[0] == 200


def test_login_cookie_dashboard_and_logout_revocation(server):
    cookie, attributes = login(server)
    for value in ('__Host-caf_session=', 'Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
        assert value in attributes
    code, headers, body = request(server, '/', cookie=cookie)
    assert code == 200 and b'aria-label="Workspace pages"' in body
    assert request(server, '/run?view=results&run=example', cookie=cookie)[0] == 200
    for path in ('/run.js', '/run_render.js', '/run_windows.js'):
        assert request(server, path, cookie=cookie)[0] == 200
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert headers['Strict-Transport-Security'] == 'max-age=31536000'
    assert json.loads(request(server, '/api/status', cookie=cookie)[2])['vms'][0]['guest_access'] == 'reachable'
    csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
    assert request(server, '/api/logout', {}, cookie=cookie)[0] == 403
    assert request(server, '/api/logout', {}, cookie=cookie, headers={'X-CSRF-Token': csrf})[0] == 200
    assert request(server, '/api/status', cookie=cookie)[0] == 401


def test_cookies_are_not_shared_between_proxy_clients(server):
    cookie, _ = login(server)
    assert request(server, '/api/status', cookie=cookie)[0] == 200
    assert request(server, '/api/status')[0] == 401


@pytest.mark.parametrize('headers', [{'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}])
def test_rejects_invalid_host_and_cross_origin(server, headers):
    assert request(server, '/api/login', {'username': 'operator', 'password': PASSWORD}, headers=headers)[0] == 403


def test_rejects_originless_login_and_direct_backend_bypass(server):
    assert request(server, '/api/login', {'username': 'operator', 'password': PASSWORD}, headers={'Origin': ''})[0] == 403
    cookie, _ = login(server)
    req = Request(f'http://127.0.0.1:{server["backend_port"]}/api/status', headers={
        'Host': server['config']['authority'], 'X-Forwarded-Proto': 'https', 'Cookie': cookie})
    with pytest.raises(HTTPError) as error:
        urlopen(req)
    assert error.value.code == 403


def test_credentials_rotation_revokes_existing_session(server):
    from cyber_agent_flow_orchestrator.auth import create_user
    cookie, _ = login(server)
    create_user(server['users'], 'operator', 'replacement long password!', replace=True)
    assert request(server, '/api/status', cookie=cookie)[0] == 401


def test_login_rate_limit_uses_real_peer_not_spoofed_forwarding_header(server):
    for i in range(8):
        code, _, body = request(server, '/api/login', {'username': 'operator', 'password': 'wrong'},
                               headers={'X-Forwarded-For': f'192.0.2.{i}'})
        assert code == 401 and json.loads(body)['error'] == 'Invalid username or password'
    assert request(server, '/api/login', {'username': 'operator', 'password': PASSWORD})[0] == 429


def test_status_requests_are_cached_and_unsupported_actions_rejected(server):
    cookie, _ = login(server)
    calls = len(server['dashboard'].probe.calls)
    for _ in range(3):
        assert request(server, '/api/status', cookie=cookie)[0] == 200
    assert len(server['dashboard'].probe.calls) == calls
    for path in ['/workflow.json', '/../config.yaml', '/api/run']:
        assert request(server, path, cookie=cookie)[0] == 404
    csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
    assert request(server, '/api/run', {}, cookie=cookie, headers={'X-CSRF-Token': csrf})[0] == 404
    assert request(server, '/api/status', method='DELETE', cookie=cookie)[0] == 405


def test_self_signed_cert_must_be_explicitly_trusted(server):
    with pytest.raises(URLError):
        urlopen(server['origin'] + '/login', context=ssl.create_default_context(), timeout=5)
    assert request(server, '/login')[0] == 200


def test_large_request_is_rejected(server):
    assert request(server, '/api/login', {'padding': 'a' * 9000})[0] == 413
