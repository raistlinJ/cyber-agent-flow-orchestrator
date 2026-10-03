"""Local operator credentials and bounded, revocable browser sessions."""
from collections import defaultdict, deque
import hashlib
from http.cookies import SimpleCookie, CookieError
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

from argon2 import PasswordHasher, exceptions
from cyber_agent_flow_eval.runner import lease

COOKIE = '__Host-caf_session'


def private_file(path, content, *, replace=False):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)  # Exclusive publication; never overwrite a key/user file.
    finally:
        Path(temp).unlink(missing_ok=True)


def read_users(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('Credential file must be readable/writable by its owner only (chmod 600)')
    if path.stat().st_size > 65536:
        raise ValueError('Credential file is too large')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('users'), dict) or not data['users']:
        raise ValueError('Invalid or empty credential file')
    for name, value in data['users'].items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', name) or not isinstance(value, str) or not value.startswith('$argon2id$'):
            raise ValueError('Invalid credential entry')
    return data['users']


def create_user(path, username, password, *, replace=False):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', username):
        raise ValueError('Username must contain 1–64 letters, digits, dots, underscores, @ or hyphens')
    if not isinstance(password, str) or len(password) < 12 or len(password.encode()) > 1024:
        raise ValueError('Password must have at least 12 characters and at most 1024 UTF-8 bytes')
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lease(path.with_suffix(path.suffix + '.lock')):
        users = read_users(path) if path.exists() else {}
        if username in users and not replace:
            raise ValueError('User already exists; use --replace to change their password')
        users[username] = PasswordHasher().hash(password)
        private_file(path, (json.dumps({'version': 1, 'users': users}, indent=2) + '\n').encode(), replace=path.exists())
    return {'username': username, 'file': str(path)}


class LoginLimited(ValueError):
    pass


class LocalProvider:
    name = 'local'

    def __init__(self, path):
        self.path = Path(path)
        read_users(self.path)
        self.hasher = PasswordHasher()
        self.dummy = self.hasher.hash(secrets.token_urlsafe(32))

    def authenticate(self, username, password, *, otp='', challenge=None):
        users = read_users(self.path)
        encoded = users.get(username, self.dummy)
        try:
            verified = self.hasher.verify(encoded, password)
        except exceptions.Argon2Error:
            verified = False
        if verified and username in users and not challenge:
            return {'username': username, 'credential': encoded}
        return None

    def validate(self, record):
        return read_users(self.path).get(record['username']) == record['credential']


class Auth:
    def __init__(self, path=None, *, provider=None, idle_seconds=1800, absolute_seconds=28800, clock=time.monotonic):
        self.provider = provider if provider is not None else LocalProvider(path)
        # PVE tickets expire after two hours. Reauthentication is explicit; never
        # retain passwords or silently renew a ticket to extend the session.
        self.idle, self.absolute, self.clock = idle_seconds, min(absolute_seconds, 7200) if self.provider.name == 'pve' else absolute_seconds, clock
        self.sessions = {}
        self.challenges = {}
        self.attempts = defaultdict(deque)
        self.global_attempts = deque()
        self.lock = threading.Lock()
        self.verifying = threading.BoundedSemaphore(2)

    def _key(self, token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _prune(self, now):
        self.sessions = {k: v for k, v in self.sessions.items()
                         if now - v['created'] < self.absolute and now - v['seen'] < self.idle}
        self.challenges = {k: v for k, v in self.challenges.items() if now - v['created'] < 180}
        for key in list(self.attempts):
            while self.attempts[key] and self.attempts[key][0] <= now - 60:
                self.attempts[key].popleft()
            if not self.attempts[key]:
                del self.attempts[key]
        while self.global_attempts and self.global_attempts[0] <= now - 60:
            self.global_attempts.popleft()

    def login(self, username, password, address, *, otp='', challenge_id=None):
        with self.lock:
            now = self.clock()
            self._prune(now)
            if len(self.global_attempts) >= 30 or len(self.attempts.get(address, ())) >= 8:
                raise LoginLimited('Too many login attempts; try again shortly')
            self.global_attempts.append(now)
            self.attempts[address].append(now)
        if not self.verifying.acquire(blocking=False):
            raise LoginLimited('Login service is busy; try again shortly')
        try:
            challenge = None
            if challenge_id is not None:
                with self.lock:
                    pending = self.challenges.pop(self._key(challenge_id), None)
                if not pending or pending['address'] != address:
                    return None
                username, password, challenge = pending['username'], '', pending['credential']
            record = self.provider.authenticate(username, password, otp=otp, challenge=challenge)
            if not record:
                return None
        finally:
            self.verifying.release()
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self._prune(now)
            if record.pop('pending', False):
                if len(self.challenges) >= 64:
                    self.challenges.pop(next(iter(self.challenges)))
                self.challenges[self._key(token)] = dict(record, created=now, address=address)
                return {'challenge_id': token, 'requires_totp': True}
            if len(self.sessions) >= 256:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[self._key(token)] = dict(record, created=now, seen=now, csrf=secrets.token_urlsafe(32))
        return token

    def session(self, token, *, revalidate=True):
        if not token or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        with self.lock:
            self._prune(self.clock())
            record = self.sessions.get(self._key(token))
            if not record:
                return None
        # Network/disk validation is outside the global lock. Check identity and
        # expiry again afterwards so an in-flight check cannot undo logout.
        if revalidate and not self.provider.validate(record):
            with self.lock:
                self.sessions.pop(self._key(token), None)
            return None
        with self.lock:
            self._prune(self.clock())
            if self.sessions.get(self._key(token)) is not record:
                return None
            record['seen'] = self.clock()
            return {'username': record['username'], 'csrf': record['csrf'], 'role': 'orchestrator',
                    'provider': self.provider.name}

    def pve_record(self, token, *, revalidate=True):
        """Server-only ticket access; never include this record in an HTTP response."""
        if self.provider.name not in ('pve', 'fusion', 'desktop') or not self.session(token, revalidate=revalidate):
            return None
        with self.lock:
            self._prune(self.clock())
            record = self.sessions.get(self._key(token))
            return dict(record) if record else None

    def access(self, token, *, revalidate=True):
        # HTTP handlers have already validated the session. They can skip only
        # this constructor's duplicate check; every access operation and guest
        # command still uses the fully revalidating callback below.
        from .access import AccessDenied, PVEAccess
        record = self.pve_record(token, revalidate=revalidate)
        if not record:
            raise AccessDenied('Login required')
        if self.provider.name in ('fusion', 'desktop'):
            from .access import FusionAccess
            return FusionAccess(self.provider, record, session_check=lambda: self.pve_record(token))
        return PVEAccess(self.provider, record, session_check=lambda: self.pve_record(token))

    def desktop_session(self):
        if self.provider.name != 'desktop':
            raise ValueError('Desktop session is not enabled')
        with self.lock:
            now = self.clock()
            self._prune(now)
            token = getattr(self, '_desktop_token', None)
            if token and self._key(token) in self.sessions:
                return token
            token = secrets.token_urlsafe(32)
            self.sessions[self._key(token)] = dict(username=self.provider.username,
                credential=self.provider.identity, created=now, seen=now, csrf=secrets.token_urlsafe(32))
            self._desktop_token = token
            return token

    def info(self):
        return {'provider': self.provider.name, 'totp': self.provider.name == 'pve'}

    def logout(self, token):
        if token:
            with self.lock:
                self.sessions.pop(self._key(token), None)


def token_from_cookie(value):
    if not value or len(value) > 8192:
        return None
    jar = SimpleCookie()
    try:
        jar.load(value)
        return jar[COOKIE].value if COOKIE in jar else None
    except CookieError:
        return None


def session_cookie(token, max_age):
    jar = SimpleCookie()
    jar[COOKIE] = token
    jar[COOKIE]['path'] = '/'
    jar[COOKIE]['secure'] = True
    jar[COOKIE]['httponly'] = True
    jar[COOKIE]['samesite'] = 'Strict'
    jar[COOKIE]['max-age'] = str(max_age)
    return jar.output(header='').strip()


class FusionProvider(LocalProvider):
    name = 'fusion'

    def __init__(self, path, inventory_file):
        if not Path(path).is_file():
            raise ValueError(f'Create a local operator account first: cyber-agent-flow-orchestrator create-user --file {path} --username operator')
        super().__init__(path)
        self.inventory_file = inventory_file
        from cyber_agent_flow_eval.fusion import inventory
        inventory(inventory_file)


class DesktopProvider:
    """One OS user; browser session credentials never prompt for a password."""
    name = 'desktop'

    def __init__(self, inventory_file=None):
        import getpass
        self.username = getpass.getuser()
        self.identity = secrets.token_urlsafe(32)
        self.inventory_file = inventory_file
        if inventory_file:
            from cyber_agent_flow_eval.fusion import inventory
            inventory(inventory_file)

    def authenticate(self, *args, **kwargs):
        return None

    def validate(self, record):
        return record.get('username') == self.username and record.get('credential') == self.identity
