"""Python TLS reverse proxy to a fixed, private, authenticated dashboard backend."""
import asyncio
import logging
from http.server import ThreadingHTTPServer
import secrets
import threading

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web

from .auth import Auth
from .tls import settings, context, ensure_certificate
from .web import Dashboard, handler


def application(upstream_port, proxy_key, config):
    app = web.Application(client_max_size=8192)
    slots = asyncio.Semaphore(64)
    upstream = f'http://127.0.0.1:{upstream_port}'

    async def client_lifetime(app):
        # A three-VM role save performs several bounded PVE calls. The whole
        # request must allow their combined duration, including initial roles.
        async with ClientSession(timeout=ClientTimeout(total=60), cookie_jar=DummyCookieJar(),
                                 trust_env=False, auto_decompress=False) as session:
            app[CLIENT] = session
            yield
    app.cleanup_ctx.append(client_lifetime)

    async def forward(request):
        if request.host != config['authority']:
            raise web.HTTPForbidden(text='Invalid Host')
        if request.method not in ('GET', 'POST'):
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
        if request.headers.get('Upgrade'):
            raise web.HTTPBadRequest(text='Protocol upgrades are not supported')
        if slots.locked():
            raise web.HTTPServiceUnavailable(text='Server busy')
        # The client cannot select an upstream or supply trusted proxy headers.
        headers = {k: request.headers[k] for k in ('Cookie', 'Origin', 'Content-Type', 'X-CSRF-Token') if k in request.headers}
        headers.update({'Host': config['authority'], 'X-Orchestrator-Proxy-Key': proxy_key,
                        'X-Forwarded-Proto': 'https', 'X-Forwarded-For': request.remote or 'unknown'})
        async with slots:
            try:
                body = await asyncio.wait_for(request.read(), timeout=10)
                async with request.app[CLIENT].request(request.method, upstream + request.rel_url.raw_path_qs,
                                                       headers=headers, data=body if request.method == 'POST' else None,
                                                       allow_redirects=False) as response:
                    content = await response.read()
                    result = web.Response(status=response.status, body=content)
                    for key in ('Content-Type', 'Content-Disposition', 'Set-Cookie', 'Location', 'Cache-Control', 'Content-Security-Policy',
                                'X-Content-Type-Options', 'Referrer-Policy', 'Retry-After'):
                        for value in response.headers.getall(key, []):
                            result.headers.add(key, value)
                    result.headers['Strict-Transport-Security'] = 'max-age=31536000'
                    return result
            except web.HTTPException:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                logging.getLogger(__name__).warning('Dashboard backend timed out: %s %s', request.method, request.path)
                raise web.HTTPGatewayTimeout(text='Dashboard request timed out') from None
            except Exception:
                raise web.HTTPBadGateway(text='Dashboard unavailable') from None
    app.router.add_route('*', '/{tail:.*}', forward)
    return app


CLIENT = web.AppKey('client', ClientSession)


def run_https(config_path, runs_root, web_config, interval=10):
    config = settings(web_config)
    ensure_certificate(config)
    ssl_context = context(config)
    provider = None
    if config['auth']['provider'] == 'pve':
        from .pve_auth import PVEProvider
        provider = PVEProvider(config['auth'])
    auth = Auth(config.get('users_file'), provider=provider, idle_seconds=config['session_idle_seconds'],
                absolute_seconds=config['session_max_seconds'])
    if provider is not None:
        from .user_dashboard import UserDashboard
        dashboard = UserDashboard(config_path, runs_root, interval, samples=config['samples'], updates=config['updates'] if config['updates'] is not None else False)
    else:
        dashboard = Dashboard(config_path, runs_root, interval)
    key = secrets.token_urlsafe(48)
    with ThreadingHTTPServer(('127.0.0.1', 0), handler(dashboard, auth=auth, proxy_key=key, origin=config['public_url'])) as backend:
        thread = threading.Thread(target=backend.serve_forever, daemon=True, name='private-dashboard')
        thread.start()
        dashboard.start()
        try:
            print(f"Orchestrator: {config['public_url']} (HTTPS + login; Ctrl-C to stop)", flush=True)
            web.run_app(application(backend.server_port, key, config), host=config['listen'], port=config['port'],
                        ssl_context=ssl_context, access_log=None, print=None, shutdown_timeout=5)
        finally:
            dashboard.close()
            backend.shutdown()
            thread.join(timeout=2)
    return 0
