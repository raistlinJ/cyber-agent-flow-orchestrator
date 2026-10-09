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


@web.middleware
async def api_errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if not request.path.startswith('/api/'):
            raise
        return web.json_response({'error': exc.text or exc.reason}, status=exc.status,
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'Strict-Transport-Security': 'max-age=31536000',
                     **{name: exc.headers[name] for name in ('Allow', 'Retry-After') if name in exc.headers}})


def upstream_timeout(path):
    # Sample creation imports a bundle and snapshots XML through multiple QGA calls.
    slow = path.endswith('/artifact') or path in (
        '/api/scenarios/upload', '/api/scenarios/list', '/api/scenarios/tasks', '/api/experiments/create',
        '/api/samples/run', '/api/model-config')
    return ClientTimeout(total=None, sock_connect=10, sock_read=300) if slow else ClientTimeout(total=60)


def application(upstream_port, proxy_key, config):
    app = web.Application(client_max_size=32 * 1024 * 1024, middlewares=[api_errors])
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
                limit = 32 * 1024 * 1024 if request.path == '/api/scenarios/upload' else 128 * 1024 if request.path == '/api/experiments/create' else 8192
                if request.content_length is not None and request.content_length > limit:
                    raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)
                body = await asyncio.wait_for(request.read(), timeout=60 if request.path == '/api/scenarios/upload' else 10)
                if len(body) > limit:
                    raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(body))
                async with request.app[CLIENT].request(request.method, upstream + request.rel_url.raw_path_qs,
                                                       headers=headers, data=body if request.method == 'POST' else None,
                                                       allow_redirects=False,
                                                       timeout=upstream_timeout(request.path)) as response:
                    if request.path.endswith('/artifact') and response.status == 200:
                        result = web.StreamResponse(status=response.status)
                        for key in ('Content-Type', 'Content-Length', 'Content-Disposition', 'Cache-Control',
                                    'Content-Security-Policy', 'X-Content-Type-Options'):
                            if key in response.headers:
                                result.headers[key] = response.headers[key]
                        result.headers['Strict-Transport-Security'] = 'max-age=31536000'
                        await result.prepare(request)
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            await result.write(chunk)
                        await result.write_eof()
                        return result
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
                raise web.HTTPGatewayTimeout(text='Dashboard request timed out. Refresh to check the current state before retrying.') from None
            except Exception:
                raise web.HTTPBadGateway(text='Dashboard unavailable') from None
    app.router.add_route('*', '/{tail:.*}', forward)
    return app


CLIENT = web.AppKey('client', ClientSession)


def run_https(config_path, runs_root, web_config, interval=10, local=False):
    config = settings(web_config, local=local)
    ensure_certificate(config)
    ssl_context = context(config)
    provider = None
    if config['auth']['provider'] == 'pve':
        from .pve_auth import PVEProvider
        provider = PVEProvider(config['auth'])
    if config['auth']['provider'] == 'fusion':
        from .auth import FusionProvider
        from .config import load
        _, runtime, _, _ = load(config_path)
        if runtime['backend']['type'] != 'fusion' or runtime['backend']['inventory_file'] != config['auth']['inventory_file']:
            raise ValueError('Fusion auth and workflow must use the same inventory')
        provider = FusionProvider(config['users_file'], config['auth']['inventory_file'])
    if config['auth']['provider'] == 'desktop':
        from .auth import DesktopProvider
        from .config import load
        _, runtime, _, _ = load(config_path)
        configured = config['auth'].get('inventory_file')
        if runtime['backend']['type'] == 'fusion' and configured != runtime['backend']['inventory_file']:
            raise ValueError('Desktop auth and workflow must use the same Fusion inventory')
        provider = DesktopProvider(configured)
    auth = Auth(config.get('users_file'), provider=provider, idle_seconds=config['session_idle_seconds'],
                absolute_seconds=config['session_max_seconds'])
    if provider is not None and (provider.name != 'desktop' or provider.inventory_file):
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
            mode = "single-user desktop session" if config["auth"]["provider"] == "desktop" else "login"
            print(f"Orchestrator: {config['public_url']} (HTTPS + {mode}; Ctrl-C to stop)", flush=True)
            web.run_app(application(backend.server_port, key, config), host=config['listen'], port=config['port'],
                        ssl_context=ssl_context, access_log=None, print=None, shutdown_timeout=5)
        finally:
            dashboard.close()
            backend.shutdown()
            thread.join(timeout=2)
    return 0
