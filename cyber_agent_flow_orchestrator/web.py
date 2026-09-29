"""Authenticated private dashboard behind the Python HTTPS proxy. Live probes run off the HTTP request path."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from .model_config import ModelConfigError
from .config import load
from .monitor import snapshot


class Dashboard:
    def __init__(self, config, runs_root, interval=10, probe=None):
        if not 2 <= interval <= 300:
            raise ValueError('Poll interval must be between 2 and 300 seconds')
        self.cfg, self.runtime, _, _ = load(config)
        self.root, self.interval, self.probe = Path(runs_root).resolve(), interval, probe
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.value = {'checked_at': None, 'workflow_id': self.cfg['id'], 'vms': [], 'runs': [], 'errors': [], 'refreshing': True}
        self.thread = None

    def read(self):
        with self.lock:
            return deepcopy(self.value)

    def refresh(self):
        with self.lock:
            self.value['refreshing'] = True
        try:
            value = snapshot(self.cfg, self.runtime, self.root, self.probe)
            value.update(refreshing=False, poll_seconds=self.interval)
            with self.lock:
                self.value = value
        except Exception as exc:
            with self.lock:
                self.value.update(refreshing=False, errors=[{'error': str(exc)}])

    def start(self):
        def poll():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=poll, daemon=True, name='lab-monitor')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


def handler(dashboard, *, auth, proxy_key, origin):
    """Authentication is mandatory, including on the private HTTP backend."""
    import secrets
    from .access import AccessDenied
    from .workspaces import Workspace
    from .samples import SampleBusy, SampleRequestError
    from .updates import UpdateError
    from .auth import LoginLimited, token_from_cookie, session_cookie
    assets = Path(__file__).with_name('static')
    authority = urlsplit(origin).netloc
    public_assets = {'/task_editor.js': ('task_editor.js', 'text/javascript'), '/http.js': ('http.js', 'text/javascript'), '/loading.js': ('loading.js', 'text/javascript'), '/login': ('login.html', 'text/html'), '/login.js': ('login.js', 'text/javascript'),
                     '/style.css': ('style.css', 'text/css')}
    protected_assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                        '/run': ('run.html', 'text/html'), '/run.js': ('run.js', 'text/javascript'),
                        '/run_config.js': ('run_config.js', 'text/javascript'), '/run_render.js': ('run_render.js', 'text/javascript'), '/run_windows.js': ('run_windows.js', 'text/javascript')}
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            pass

        def respond(self, status, value=b'', mime='application/json', **headers):
            body = json.dumps(value, allow_nan=False).encode() if not isinstance(value, bytes) else value
            self.send_response(status)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_artifact(self, stream, mime, filename):
            import os
            import shutil
            safe_name = ''.join(c if c.isalnum() or c in '._-' else '_' for c in filename)
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
            self.send_header('Content-Disposition', 'attachment; filename="' + safe_name + '"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile, 1024 * 1024)

        def trusted(self, *, post=False):
            key = self.headers.get('X-Orchestrator-Proxy-Key', '')
            if not secrets.compare_digest(key.encode(), proxy_key.encode()):
                self.respond(403, {'error': 'Use the HTTPS listener'})
                return False
            if self.headers.get('Host') != authority or self.headers.get('X-Forwarded-Proto') != 'https':
                self.respond(403, {'error': 'Invalid request origin'})
                return False
            supplied_origin = self.headers.get('Origin')
            if (post and supplied_origin != origin) or (supplied_origin and supplied_origin != origin):
                self.respond(403, {'error': 'Invalid request origin'})
                return False
            return True

        def current(self, *, revalidate=True):
            token = token_from_cookie(self.headers.get('Cookie'))
            return token, auth.session(token, revalidate=revalidate)

        def asset(self, entry):
            name, mime = entry
            self.respond(200, (assets / name).read_bytes(), mime)

        def do_GET(self):
            if not self.trusted():
                return
            try:
                path = urlsplit(self.path).path
                if path == '/api/auth':
                    self.respond(200, auth.info())
                    return
                if path in public_assets:
                    if path == '/login' and self.current()[1]:
                        self.respond(302, b'', **{'Location': '/'})
                    else:
                        self.asset(public_assets[path])
                    return
                token, session = self.current()
                if not session:
                    if path == '/':
                        self.respond(302, b'', **{'Location': '/login'})
                    else:
                        self.respond(401, {'error': 'Login required'})
                    return
                if path == '/api/session':
                    self.respond(200, session)
                elif path == '/api/status':
                    if auth.provider.name == 'pve':
                        if not getattr(dashboard, 'scoped', False):
                            raise AccessDenied('A per-user dashboard is required for PVE login')
                        refresh = parse_qs(urlsplit(self.path).query).get('refresh')
                        options = {'observe': False} if refresh == ['0'] else {'force': refresh == ['1']}
                        self.respond(200, dashboard.read(auth.access(token, revalidate=False), **options))
                    else:
                        self.respond(200, dashboard.read())
                elif path.startswith('/api/runs/') and getattr(dashboard, 'scoped', False):
                    parts = path.split('/')
                    if len(parts) != 5 or parts[4] not in ('status', 'results', 'dataset.csv', 'artifact'):
                        self.respond(404, {'error': 'Not found'})
                        return
                    try:
                        access = auth.access(token, revalidate=False)
                        if parts[4] == 'artifact':
                            artifact_id = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                            with dashboard.artifact(access, parts[3], artifact_id) as (stream, mime, filename):
                                access.current()
                                self.send_artifact(stream, mime, filename)
                            return
                        if parts[4] == 'dataset.csv':
                            value = dashboard.dataset(access, parts[3])
                        else:
                            value = dashboard.run_detail(access, parts[3], parts[4])
                    except (FileNotFoundError, KeyError, ValueError):
                        self.respond(404, {'error': 'Run not found or results unavailable'})
                        return
                    if parts[4] == 'dataset.csv':
                        self.respond(200, value, 'text/csv', **{'Content-Disposition': f'attachment; filename="{parts[3]}.csv"'})
                    else:
                        self.respond(200, value)
                elif path in {f'/demo-{name}.{ext}' for name in ('smoke', 'tools-vs-helper') for ext in ('xml', 'zip')}:
                    filename = path[1:]
                    with (assets / filename).open('rb') as stream:
                        self.send_artifact(stream, 'application/zip' if filename.endswith('.zip') else 'application/xml', filename)
                elif path in protected_assets:
                    self.asset(protected_assets[path])
                else:
                    self.respond(404, {'error': 'Not found'})
            except AccessDenied:
                self.respond(403, {'error': 'Access not granted'})
            except (ValueError, OSError, KeyError):
                self.respond(503, {'error': 'Authentication or dashboard service unavailable'})

        def upload_scenario(self):
            from .scenario_upload import MAX_UPLOAD
            try:
                token, session = self.current()
                if not session:
                    self.respond(401, {'error': 'Login required'})
                    return
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), session['csrf'].encode()):
                    self.respond(403, {'error': 'Invalid CSRF token'})
                    return
                if not getattr(dashboard, 'scoped', False) or auth.provider.name != 'pve':
                    self.respond(404, {'error': 'Scenario upload is not enabled'})
                    return
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
                    self.respond(415, {'error': 'Binary XML or ZIP upload required'})
                    return
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    self.respond(400, {'error': 'Invalid request framing'})
                    return
                size = int(self.headers['Content-Length'])
                if not 0 < size <= MAX_UPLOAD:
                    self.respond(413, {'error': 'Upload must be between 1 byte and 32 MiB'})
                    return
                content = self.rfile.read(size)
                if len(content) != size:
                    raise SampleRequestError('Incomplete upload')
                result = dashboard.scenarios.upload(auth.access(token, revalidate=False), content)
                self.respond(200, result)
            except AccessDenied:
                self.respond(403, {'error': 'Access not granted'})
            except (SampleRequestError, ValueError) as exc:
                self.respond(400, {'error': str(exc)})
            except (OSError, KeyError):
                self.respond(503, {'error': 'Scenario import is unavailable'})

        def do_POST(self):
            if not self.trusted(post=True):
                return
            if urlsplit(self.path).path == '/api/scenarios/upload':
                self.upload_scenario()
                return
            try:
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    self.respond(415, {'error': 'JSON required'})
                    return
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    self.respond(400, {'error': 'Invalid request framing'})
                    return
                size = int(self.headers['Content-Length'])
                if not 0 <= size <= (128 * 1024 if urlsplit(self.path).path == '/api/experiments/create' else 8192):
                    self.respond(413, {'error': 'Request too large'})
                    return
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Expected JSON object')
            except (ValueError, OSError):
                self.respond(400, {'error': 'Invalid JSON request'})
                return
            try:
                path = urlsplit(self.path).path
                if path in ('/api/login', '/api/login/totp'):
                    username, password = data.get('username', ''), data.get('password', '')
                    otp, challenge_id = data.get('otp', ''), data.get('challenge_id')
                    if (not isinstance(username, str) or not isinstance(password, str) or len(username) > 64
                            or len(password.encode()) > 1024 or not isinstance(otp, str) or len(otp) > 128
                            or (path == '/api/login/totp' and (not isinstance(challenge_id, str)
                                or len(challenge_id) != 43 or not otp))
                            or (path == '/api/login' and challenge_id is not None)):
                        self.respond(400, {'error': 'Invalid login request'})
                        return
                    token = auth.login(username, password, self.headers.get('X-Forwarded-For', 'unknown'),
                                       otp=otp, challenge_id=challenge_id)
                    if not token:
                        self.respond(401, {'error': 'Login failed or orchestrator access not granted' if auth.provider.name == 'pve'
                                                  else 'Invalid username or password'})
                        return
                    if isinstance(token, dict):
                        self.respond(202, token)
                        return
                    auth.logout(token_from_cookie(self.headers.get('Cookie')))
                    self.respond(200, {'ok': True}, **{'Set-Cookie': session_cookie(token, auth.absolute)})
                    return
                # Logout remains possible during a PVE outage. It only destroys
                # the caller's local session and still requires Origin + CSRF.
                token, session = self.current(revalidate=path != '/api/logout')
                if not session:
                    self.respond(401, {'error': 'Login required'})
                    return
                supplied = self.headers.get('X-CSRF-Token', '')
                if not secrets.compare_digest(supplied.encode(), session['csrf'].encode()):
                    self.respond(403, {'error': 'Invalid CSRF token'})
                    return
                if path == '/api/logout':
                    auth.logout(token)
                    self.respond(200, {'ok': True}, **{'Set-Cookie': session_cookie('', 0)})
                elif path == '/api/roles' and getattr(dashboard, 'scoped', False):
                    try:
                        Workspace.validate_roles(data)
                    except ValueError as exc:
                        self.respond(400, {'error': str(exc)})
                        return
                    self.respond(200, dashboard.select(auth.access(token, revalidate=False), data))
                elif path == '/api/samples/run' and getattr(dashboard, 'scoped', False) and auth.provider.name == 'pve':
                    if (set(data) != {'sample_id', 'request_id'} or not isinstance(data['sample_id'], str)
                            or not isinstance(data['request_id'], str)):
                        self.respond(400, {'error': 'Supply a sample ID and request ID only'})
                        return
                    self.respond(202, dashboard.run_sample(auth.access(token, revalidate=False), data['sample_id'], data['request_id']))
                elif path == '/api/scenarios/list' and getattr(dashboard, 'scoped', False) and auth.provider.name == 'pve':
                    if set(data) != {'query'} or not isinstance(data['query'], str):
                        raise SampleRequestError('Supply scenario search text only')
                    self.respond(200, dashboard.scenarios.catalogue(auth.access(token, revalidate=False), data['query']))
                elif path == '/api/scenarios/tasks' and getattr(dashboard, 'scoped', False) and auth.provider.name == 'pve':
                    if set(data) != {'selection_id'}:
                        raise SampleRequestError('Supply a scenario selection ID only')
                    self.respond(200, dashboard.scenarios.tasks(auth.access(token, revalidate=False), data['selection_id']))
                elif path in ('/api/experiments/create', '/api/experiments/run', '/api/experiments/stop') and getattr(dashboard, 'scoped', False) and auth.provider.name == 'pve':
                    action = path.rsplit('/', 1)[1]
                    expected = {'sample_id', 'request_id'} if action == 'create' else {'run_id', 'request_id'} if action == 'run' else {'run_id'}
                    if action == 'create' and 'selection_id' in data:
                        expected = {'selection_id', 'request_id', 'allowed_targets', 'disallowed_targets'}
                        if 'evaluation' in data:
                            expected.add('evaluation')
                        if 'tasks' in data:
                            expected.add('tasks')
                    if action == 'create' and 'provide_progressive_hints' in data:
                        expected.add('provide_progressive_hints')
                        if type(data['provide_progressive_hints']) is not bool:
                            raise SampleRequestError('provide_progressive_hints must be a boolean')
                    if set(data) != expected or any(not isinstance(v, str) for k, v in data.items() if k not in ('evaluation', 'tasks', 'provide_progressive_hints')):
                        raise SampleRequestError('Invalid experiment request fields')
                    self.respond(202, dashboard.experiment(auth.access(token, revalidate=False), action, data))
                elif path == '/api/model-config' and getattr(dashboard, 'scoped', False) and auth.provider.name == 'pve':
                    self.respond(200, dashboard.model_configs.exchange(auth.access(token, revalidate=False), data))
                elif path == '/api/applications' and getattr(dashboard, 'scoped', False) and auth.provider.name == 'pve':
                    if (set(data) - {'process_confirmation'} != {'role', 'action', 'ref', 'request_id'}
                            or any(not isinstance(v, str) for v in data.values())):
                        raise UpdateError('Supply role, action, ref, request_id and optional process_confirmation only')
                    self.respond(202, dashboard.maintain(auth.access(token, revalidate=False), data))
                else:
                    self.respond(404, {'error': 'No such action is enabled'})
            except AccessDenied:
                self.respond(403, {'error': 'Access not granted'})
            except SampleBusy as exc:
                self.respond(409, {'error': str(exc)})
            except (SampleRequestError, ModelConfigError) as exc:
                self.respond(400, {'error': str(exc)})
            except UpdateError as exc:
                self.respond(400, {'error': str(exc)})
            except LoginLimited:
                self.respond(429, {'error': 'Too many login attempts; try again shortly'}, **{'Retry-After': '60'})
            except (ValueError, OSError, KeyError):
                self.respond(503, {'error': 'Authentication service unavailable'})
    return Handler


def serve(config, runs_root, *, web_config, interval=10):
    from .proxy import run_https
    return run_https(config, runs_root, web_config, interval=interval)
