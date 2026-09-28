"""Fixed guest operations for the bundled participant-only HTTP sample.

Executed by QGA with JSON argv. No host filesystem or arbitrary command endpoint.
"""
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
import time

SERVER = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys

PAGES = {
    '/': '<h1>Evaluation demo</h1><a href="/briefing.txt">Briefing</a> <a href="/archive/">Archive</a>',
    '/briefing.txt': 'First objective: FLAG{demo_entry}\n',
    '/archive/': '<h1>Archive</h1><a href="/archive/note.txt">Read the note</a>',
    '/archive/note.txt': 'Second objective: FLAG{demo_archive}\n',
}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path not in PAGES:
            self.send_error(404)
            return
        body = PAGES[self.path].encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
ready = Path(sys.argv[1])
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'port': server.server_port}))
temporary.replace(ready)
server.serve_forever()
'''


def dispatch(data):
    token = data.get('token', '')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('Invalid sample token')
    unit = 'caf-sample-' + token
    op = data.get('op')
    if op == 'sample_check':
        user = data.get('user')
        if not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', user):
            raise ValueError('Invalid participant user')
        result = subprocess.run(['runuser', '-u', user, '--', sys.executable, '-c',
                                 'import json,shutil;print(json.dumps([n for n in ("nmap","curl","python3") if not shutil.which(n)]))'],
                                check=True, capture_output=True, text=True, timeout=10)
        missing = json.loads(result.stdout)
        if missing:
            raise ValueError('Participant is missing sample tools: ' + ', '.join(missing))
        return {'ready': True}
    if op not in ('sample_start', 'sample_stop'):
        raise ValueError('Unknown sample operation')
    controls = Path('/var/lib/caf-eval-samples')
    controls.mkdir(mode=0o700, exist_ok=True)
    if controls.is_symlink():
        raise ValueError('Sample control directory cannot be a symlink')
    control = controls / (token + '.control')
    if control.is_symlink():
        raise ValueError('Sample control file cannot be a symlink')
    with control.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        cancelled = bool(lock.read())
        if op == 'sample_stop':
            lock.seek(0)
            lock.write('cancelled')
            lock.flush()
            result = subprocess.run(['systemctl', 'stop', unit + '.service'], capture_output=True, text=True, timeout=15)
            state = subprocess.run(['systemctl', 'show', unit + '.service', '--property=ActiveState', '--value'],
                                   capture_output=True, text=True, timeout=5)
            if state.stdout.strip() not in ('inactive', 'failed'):
                raise ValueError('Could not confirm sample fixture stopped: ' + result.stderr[-500:])
            return {'stopped': True, 'unit': unit + '.service'}
        if cancelled:
            raise ValueError('Sample fixture was cancelled; refusing delayed start')
        ready = Path('/run') / unit / 'ready.json'
        subprocess.run(['systemd-run', '--unit=' + unit, '--collect',
                        '--property=DynamicUser=yes', '--property=NoNewPrivileges=yes',
                        '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
                        '--property=RuntimeMaxSec=30min', '--property=KillMode=control-group',
                        '--property=RuntimeDirectory=' + unit,
                        '--', sys.executable, '-c', SERVER, str(ready)],
                       check=True, capture_output=True, text=True, timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.is_file():
                port = json.loads(ready.read_text())['port']
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError('Invalid sample fixture port')
                return {'unit': unit + '.service', 'url': f'http://127.0.0.1:{port}/'}
            time.sleep(.1)
        raise ValueError('Sample fixture did not become ready')


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        raise SystemExit(1)
