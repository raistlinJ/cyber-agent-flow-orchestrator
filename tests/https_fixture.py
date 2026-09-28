"""Local HTTPS test harness. Credentials/certificates exist only in a temp dir."""
import asyncio
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import secrets
import socket
import ssl
import threading

from aiohttp import web

from cyber_agent_flow_orchestrator.auth import Auth, create_user
from cyber_agent_flow_orchestrator.proxy import application
from cyber_agent_flow_orchestrator.tls import create_certificate, context
from cyber_agent_flow_orchestrator.web import handler

PASSWORD = 'test-only long password!'


@contextmanager
def secure_server(dashboard, directory, *, auth=None):
    directory.mkdir(parents=True, exist_ok=True)
    users = directory / 'users.json'
    create_user(users, 'operator', PASSWORD)
    cert, key = directory / 'cert.pem', directory / 'key.pem'
    create_certificate(cert, key, ['localhost', '127.0.0.1'])
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    origin = f'https://localhost:{port}'
    config = {'authority': f'localhost:{port}', 'certificate': str(cert), 'private_key': str(key)}
    secret = secrets.token_urlsafe(48)
    auth = auth if auth is not None else Auth(users)
    backend = ThreadingHTTPServer(('127.0.0.1', 0), handler(dashboard, auth=auth, proxy_key=secret, origin=origin))
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    runner = web.AppRunner(application(backend.server_port, secret, config), access_log=None)
    async def start():
        await runner.setup()
        await web.SockSite(runner, sock, ssl_context=context(config)).start()
    try:
        asyncio.run_coroutine_threadsafe(start(), loop).result(timeout=10)
        yield {'origin': origin, 'ssl': ssl.create_default_context(cafile=str(cert)), 'auth': auth,
               'backend_port': backend.server_port, 'secret': secret, 'users': users, 'certificate': cert,
               'key': key, 'config': config}
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
        backend.shutdown()
        backend.server_close()
        backend_thread.join()
        sock.close()
