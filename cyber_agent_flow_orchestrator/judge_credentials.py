"""Private, owner-scoped host keys; run inputs contain only immutable references."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from cyber_agent_flow_eval import integration as ev
from .auth import private_file
from .workspaces import private_directory

PREFIX = 'CAF_JUDGE_WEB_'


def endpoint(url):
    return url.rstrip('/')


def read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError('Cannot open stored judge credential') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 32768):
            raise ValueError('Stored judge credential must be an owned, private regular file')
    except Exception:
        os.close(fd)
        raise
    with os.fdopen(fd, 'r') as stream:
        try:
            value = json.loads(stream.read(32769))
        except (ValueError, UnicodeError):
            raise ValueError('Invalid stored judge credential') from None
    if not isinstance(value, dict):
        raise ValueError('Invalid stored judge credential')
    return value


def validate_key(key, clear):
    if (not isinstance(key, str) or len(key.encode()) > 8192
            or any(ord(c) < 32 or ord(c) == 127 for c in key)):
        raise ValueError('Judge API key must be at most 8192 bytes without control characters')
    if type(clear) is not bool or (clear and key):
        raise ValueError('Enter a judge API key or select Clear stored key, not both')


def bind(workspace, config, key='', clear=False, *, persist=False):
    validate_key(key, clear)
    config = deepcopy(config)
    if not config['enabled']:
        if key or clear:
            raise ValueError('Enable the judge before changing its API key')
        return config
    url = endpoint(config['model']['url'])
    directory = private_directory(workspace.path / 'judge-credentials')
    preference = directory / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    if preference.is_symlink():
        raise ValueError('Stored judge credential preferences cannot be symlinks')
    with ev.lease(directory / '.lock'):
        if clear:
            selected = {'endpoint': url, 'cleared': True}
        elif key:
            selected = {'endpoint': url, 'api_key_env': PREFIX + uuid.uuid4().hex}
            if persist:
                private_file(directory / (selected['api_key_env'] + '.json'),
                             json.dumps(dict(selected, api_key=key)).encode())
        else:
            selected = read_private(preference)
        if selected:
            if selected.get('endpoint') != url:
                raise ValueError('Stored judge credential endpoint mismatch')
            config['model'].pop('api_key_env', None)
            if not selected.get('cleared'):
                variable = selected.get('api_key_env', '')
                if not isinstance(variable, str) or not re.fullmatch(PREFIX + '[0-9a-f]{32}', variable):
                    raise ValueError('Invalid stored judge credential reference')
                config['model']['api_key_env'] = variable
            if persist and (key or clear):
                private_file(preference, json.dumps(selected).encode(), replace=preference.exists())
    if persist:
        restore(workspace, {'judge': config})
    return config


def restore(workspace, runtime):
    model = runtime.get('judge', {}).get('model', {})
    variable = model.get('api_key_env', '')
    if not variable.startswith(PREFIX):
        return  # Administrator-provided host environment credential.
    if not re.fullmatch(PREFIX + '[0-9a-f]{32}', variable):
        raise ValueError('Invalid stored judge credential reference')
    directory = private_directory(workspace.path / 'judge-credentials')
    value = read_private(directory / (variable + '.json'))
    if (not value or value.get('api_key_env') != variable
            or value.get('endpoint') != endpoint(model['url'])):
        raise ValueError('Saved judge key is unavailable for this account and endpoint; enter a key when creating a new experiment')
    key = value.get('api_key')
    validate_key(key, False)
    if not key:
        raise ValueError('Saved judge key is empty')
    os.environ[variable] = key


def status(workspace, runtime):
    """Expose endpoint/key presence to the form, never values or references."""
    rows = {}
    for model in (runtime.get('judge', {}).get('model', {}), runtime.get('model', {})):
        if model.get('url') and model.get('api_key_env'):
            url = endpoint(model['url'])
            rows.setdefault(url, dict(endpoint=url, configured=bool(os.environ.get(model['api_key_env'])), source='host environment'))
    directory = private_directory(workspace.path / 'judge-credentials')
    for path in sorted(directory.glob('*.json')):
        if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
            continue
        try:
            selected = read_private(path)
            if not selected or not isinstance(selected.get('endpoint'), str):
                continue
            url = selected['endpoint']
            if hashlib.sha256(url.encode()).hexdigest() + '.json' != path.name:
                continue
            configured = False
            variable = selected.get('api_key_env', '')
            if not selected.get('cleared') and isinstance(variable, str) and re.fullmatch(PREFIX + '[0-9a-f]{32}', variable):
                key = read_private(directory / (variable + '.json'))
                configured = bool(key and key.get('endpoint') == url and key.get('api_key_env') == variable and key.get('api_key'))
            rows[url] = dict(endpoint=url, configured=configured, source='saved judge key')
        except ValueError:
            continue  # A damaged credential must not hide the dashboard.
    return list(rows.values())
