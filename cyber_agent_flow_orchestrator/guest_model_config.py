"""Fixed QGA model configuration operations. Secrets enter on stdin, never leave."""
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

LOCK = Path('/run/caf-application-maintenance.lock')
SECRETS = Path('/var/lib/caf-model-config')
ENV_KEYS = dict(provider='CORETG_AI_PROVIDER', url='CORETG_AI_BASE_URL', model='CORETG_AI_MODEL', ssl_verify='CORETG_AI_VERIFY_SSL')


def validate(role, value):
    if role not in ('participant', 'scenarioforge') or not isinstance(value, dict) or set(value) != {'provider', 'url', 'model', 'ssl_verify'}:
        raise ValueError('Supply provider, URL, model and TLS verification only')
    allowed = ('ollama_direct', 'openai', 'litellm', 'claude') if role == 'participant' else ('ollama', 'openai', 'litellm')
    if value['provider'] not in allowed or type(value['ssl_verify']) is not bool:
        raise ValueError('Invalid provider or TLS setting')
    for key in ('url', 'model'):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 2048 or any(ord(c) < 32 for c in value[key]):
            raise ValueError('URL and model must be nonempty single-line values')
    parsed = urlsplit(value['url'])
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTP(S) base URL without credentials, query or fragment')
    return dict(value)


def env_values(text):
    result = {}
    for line in text.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
        if not match:
            continue
        key, value = match.groups()
        if value.startswith(('"', "'")):
            try: value = str(ast.literal_eval(value))
            except (ValueError, SyntaxError): raise ValueError('Unsupported quoted environment value') from None
        else:
            value = value.split(' #', 1)[0].rstrip()
        result[key] = value
    return result


def replace_env(text, changes):
    remaining = dict(changes)
    lines = []
    for line in text.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        key = match.group(1) if match else None
        if key in changes:
            if key in remaining:
                lines.append(key + '=' + json.dumps(str(remaining.pop(key))))
        else: lines.append(line)
    lines.extend(key + '=' + json.dumps(str(value)) for key, value in remaining.items())
    return '\n'.join(lines) + '\n'


def atomic(path, content, owner):
    fd, temporary = tempfile.mkstemp(prefix='.caf-model-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def dispatch(data):
    role, op = data.get('role'), data.get('action')
    if role not in ('participant', 'scenarioforge') or op not in ('read', 'save', 'use') or (op == 'use' and role != 'participant'):
        raise ValueError('Unsupported model configuration operation')
    root = Path(data['root'])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError('Application root must be an existing directory without symlinks')
    path = root / ('configs/cli.json' if role == 'participant' else '.scenarioforge.env')
    if path.resolve() != path:
        raise ValueError('Configuration symlinks are not supported')
    with LOCK.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Application maintenance is in progress; retry later') from None
        content = b''
        if path.exists():
            if not path.is_file(): raise ValueError('Configuration must be a regular file')
            with path.open('rb') as stream: content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024: raise ValueError('Application configuration is too large')
        identity = hashlib.sha256(content).hexdigest()
        if op != 'read' and data.get('revision') != identity:
            raise ValueError('Configuration changed in the VM. Pull it again before saving or using it.')
        text = content.decode('utf-8')
        config = json.loads(text or '{}') if role == 'participant' else env_values(text)
        if not isinstance(config, dict): raise ValueError('Application configuration must be an object')
        key_name = 'api_key' if role == 'participant' else 'CORETG_AI_API_KEY'
        secret = config.get(key_name, '')
        if not isinstance(secret, str): raise ValueError('Invalid stored API key')
        if role == 'participant':
            values = dict(provider=config.get('provider', 'ollama_direct'), url=config.get('url', 'http://localhost:11434'),
                          model=config.get('model', ''), ssl_verify=config.get('ssl_verify', True))
        else:
            values = {key: config.get(env, '') for key, env in ENV_KEYS.items()}
            values['provider'] = values['provider'] or 'litellm'
            values['ssl_verify'] = str(config.get(ENV_KEYS['ssl_verify'], 'true')).lower() not in ('false', '0', 'no', 'off')
        if values['url']:
            parsed = urlsplit(values['url'])
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Remove embedded credentials or query parameters from the saved model URL before pulling it')
        variable = config.get('api_key_env', 'MCP_API_KEY') if role == 'participant' else 'CORETG_AI_API_KEY'
        if not isinstance(variable, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', variable):
            raise ValueError('Invalid API key environment variable name')
        backup = None
        if op == 'save':
            values = validate(role, data['settings'])
            replacement = data.get('api_key')
            if replacement is not None:
                if not isinstance(replacement, str) or len(replacement) > 8192 or any(ord(c) < 32 for c in replacement):
                    raise ValueError('API key must be a single-line value')
                secret = replacement
            if role == 'participant':
                config.update(values)
                if replacement is not None: config['api_key'] = secret
                updated = (json.dumps(config, indent=2) + '\n').encode()
            else:
                changes = {ENV_KEYS[k]: str(v).lower() if type(v) is bool else v for k, v in values.items()}
                if replacement is not None: changes[key_name] = secret
                updated = replace_env(text, changes).encode()
            owner = path.stat() if path.exists() else root.stat()
            if not path.parent.exists():
                path.parent.mkdir(mode=0o700)
                os.chown(path.parent, owner.st_uid, owner.st_gid)
            if content:
                backup = path.with_name(path.name + '.caf-model-' + uuid.uuid4().hex + '.bak')
                atomic(backup, content, owner)
            atomic(path, updated, owner)
            identity = hashlib.sha256(updated).hexdigest()
        result = dict(settings=values, revision=identity, path=str(path), exists=path.exists(), api_key_set=bool(secret),
                      api_key_env=variable,
                      backup=str(backup) if backup else None)
        if role == 'participant' and op in ('save', 'use'):
            validate(role, values)
            # A unique, immutable secret snapshot keeps subsequent trials reproducible.
            # Environment-only keys remain supplied by the host runtime's guest environment_file.
            if secret:
                SECRETS.mkdir(mode=0o700, parents=True, exist_ok=True)
                if SECRETS.is_symlink() or SECRETS.stat().st_uid != os.geteuid():
                    raise ValueError('Unsafe guest model credential directory')
                SECRETS.chmod(0o700)
                target = SECRETS / (uuid.uuid4().hex + '.env')
                atomic(target, ('CAF_EVAL_MODEL_API_KEY=' + json.dumps(secret, ensure_ascii=False) + '\n').encode(), SECRETS.stat())
                result.update(environment_file=str(target), api_key_env='CAF_EVAL_MODEL_API_KEY')
        return result


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.read(1024 * 1024)) if len(sys.argv) == 1 else json.loads(sys.argv[1])
        print(json.dumps(dispatch(request)))
    except Exception as exc:
        # Parsing errors may contain file fragments; never echo credential-bearing input.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, (json.JSONDecodeError, UnicodeError)) else 'Unable to read or save the application model configuration'
        print(json.dumps({'error': message}))
        raise SystemExit(1)
