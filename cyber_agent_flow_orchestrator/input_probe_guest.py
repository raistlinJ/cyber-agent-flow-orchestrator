"""Read-only guest input checks and synthetic model probe; no trial execution."""
import json
import os
from pathlib import Path
import pwd
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def env_values(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        values = shlex.split(value, comments=True, posix=True)
        result[name.strip()] = ' '.join(values)
    return result


def access(path, user, writable=False, executable=False):
    path = Path(path)
    if not path.is_absolute() or (path.is_symlink() and not executable) or not path.exists():
        raise ValueError('Required guest path unavailable: ' + str(path))
    flag = '-w' if writable else '-x' if executable else '-r'
    result = subprocess.run(['runuser', '-u', user, '--', 'test', flag, str(path)],
                            capture_output=True, timeout=5)
    if result.returncode:
        raise ValueError('Guest account ' + user + ' lacks ' + flag + ' access to ' + str(path))
    if path.is_dir():
        result = subprocess.run(['runuser','-u',user,'--','test','-x',str(path)],capture_output=True,timeout=5)
        if result.returncode:
            raise ValueError('Guest account '+user+' cannot enter directory '+str(path))


def check_model(model, key, timeout=60, tools=False):
    provider, base = model['provider'], model['url'].rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Model URL must be HTTP(S) without embedded credentials, query or fragment')
    prompt = 'Synthetic connectivity check only. Reply with the single word READY. No real tools or task data are involved.'
    headers = {'Content-Type': 'application/json'}
    tool = {'type':'function','function':{'name':'caf_preflight_echo','description':'Synthetic non-executable capability check','parameters':{'type':'object','properties':{'value':{'type':'string'}}}}}
    if provider == 'ollama_direct':
        endpoint = base if base.endswith('/api/chat') else base + '/api/chat'
        body = {'model':model['name'],'messages':[{'role':'user','content':prompt}], 'stream':False,
                'options':{'temperature':0,'num_predict':256}}
        if tools: body['tools'] = [tool]
    elif provider == 'claude':
        endpoint = base if base.endswith('/v1/messages') else base + ('/messages' if base.endswith('/v1') else '/v1/messages')
        headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
        body = {'model':model['name'],'messages':[{'role':'user','content':prompt}],'max_tokens':256}
        if tools: body['tools'] = [{'name':'caf_preflight_echo','description':tool['function']['description'],'input_schema':tool['function']['parameters']}]
    elif provider in ('openai', 'litellm'):
        endpoint = base if base.endswith('/chat/completions') else base + ('/chat/completions' if base.endswith('/v1') else '/v1/chat/completions')
        body = {'model':model['name'],'messages':[{'role':'user','content':prompt}], 'max_tokens':256}
        if tools: body['tools'] = [tool]
        if parsed.hostname == 'api.openai.com': body['max_completion_tokens'] = body.pop('max_tokens')
    else:
        raise ValueError('Unsupported participant model provider')
    if key and provider != 'claude': headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers)
    context = ssl.create_default_context() if model.get('ssl_verify', True) else ssl._create_unverified_context()
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read(512001)
        if len(raw) > 512000: raise ValueError('Model probe response exceeded size limit')
        data = json.loads(raw)
        if provider == 'claude':
            usable = any(b.get('type') in ('text','tool_use') and (b.get('text') or b.get('name')) for b in data.get('content', []))
        else:
            message = data['message'] if provider == 'ollama_direct' else data['choices'][0]['message']
            usable = bool(message.get('content') or message.get('tool_calls'))
        if not usable: raise ValueError('Model returned no usable text or tool call within the small preflight probe; check model readiness and generation limits')
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Participant model returned HTTP {exc.code}; check its endpoint, model name and guest API key') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('Participant model request failed from the VM; check DNS, routes, TLS settings and model server availability') from None
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise ValueError('Participant model returned an incompatible response') from None
    return {'seconds':round(time.monotonic()-started,2), 'tools_tested':tools,
            'message':'Participant authenticated and generated a usable synthetic response from the VM'}


def dispatch(data):
    role, user = data['role'], data['user']
    pwd.getpwnam(user)
    if role == 'participant':
        access(data['engine']['path'], user)
        access(data['engine']['python'], user, executable=True)
        variable = data['model'].get('api_key_env')
        environment = {}
        try:
            result = subprocess.run(['systemctl','show-environment'],capture_output=True,text=True,timeout=5)
            if result.returncode == 0: environment = env_values(result.stdout)
        except OSError:
            pass  # The engine compatibility check reports missing systemd separately.
        if data.get('environment_file'):
            path = Path(data['environment_file'])
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024*1024:
                raise ValueError('Participant credential environment file is missing or unsafe; Apply model settings again')
            environment.update(env_values(path.read_text()))
        key = environment.get(variable, '') if variable else ''
        if variable and not key:
            raise ValueError('Participant credential variable ' + variable + ' is missing from its guest service environment; Apply model settings again')
        return check_model(data['model'], key, tools=data.get('tools', False))
    if role == 'scenarioforge':
        root = Path(data['root'])
        access(root, user, writable=True)
        access(data['python'], user, executable=True)
        access(root / 'scenarioforge/cli.py', user)
        config = root / '.scenarioforge.env'
        access(config, user)
        values = env_values(config.read_text())
        return {'message':'ScenarioForge checkout, Python and configuration are accessible', 'core': {
            'user':values.get('CORE_SSH_USERNAME') or values.get('CORETG_VM_MODE_SSH_USERNAME') or 'corevm',
            'repo':values.get('CORE_REMOTE_STATIC_REPO') or '/tmp/scenarioforge',
            'base':values.get('CORE_REMOTE_BASE_DIR') or '/tmp/scenarioforge',
            'interface':values.get('CORETG_VM_MODE_HITL_CORE_IFX_NAME') if values.get('CORETG_VM_MODE_HITL_ENABLED','').lower() in ('true','1') else None}}
    if role == 'scenarioforge-connectivity':
        values = env_values((Path(data['root']) / '.scenarioforge.env').read_text())
        if values.get('CORETG_WEBUI_MODE') != 'vm':
            return {'status':'skip','message':'ScenarioForge uses its native CORE connection; exported runtime readiness will validate it'}
        settings = {
            'host':values.get('CORE_SSH_HOST') or values.get('CORETG_VM_MODE_SSH_HOST') or values.get('CORE_HOST'),
            'port':int(values.get('CORE_SSH_PORT') or values.get('CORETG_VM_MODE_SSH_PORT') or 22),
            'username':values.get('CORE_SSH_USERNAME') or values.get('CORETG_VM_MODE_SSH_USERNAME') or 'corevm',
            'password':values.get('CORE_SSH_PASSWORD') or values.get('CORETG_VM_MODE_SSH_PASSWORD') or None,
            'key_filename':values.get('CORE_SSH_KEY_PATH') or None,
            'core_host':values.get('CORE_HOST') or '127.0.0.1',
            'core_port':int(values.get('CORE_PORT') or 50051),
        }
        script = """import json,sys,paramiko
settings=json.load(sys.stdin)
client=paramiko.SSHClient()
client.load_system_host_keys()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(settings['host'],port=settings['port'],username=settings['username'],password=settings['password'],key_filename=settings['key_filename'],timeout=10,auth_timeout=10,banner_timeout=10)
    channel=client.get_transport().open_channel('direct-tcpip',(settings['core_host'],settings['core_port']),('127.0.0.1',0),timeout=10)
    channel.close()
    print('ready')
except Exception as exc:
    print(type(exc).__name__)
    raise SystemExit(1)
finally:
    client.close()
"""
        result = subprocess.run(['runuser','-u',user,'--',data['python'],'-c',script],input=json.dumps(settings),
                                capture_output=True,text=True,timeout=45)
        if result.returncode or result.stdout.strip() != 'ready':
            raise ValueError('ScenarioForge cannot authenticate to CORE or reach its gRPC port using the saved .scenarioforge.env settings; check CORE SSH credentials, host and port')
        return {'message':'ScenarioForge authenticated to CORE over SSH and reached the configured gRPC port'}
    if role == 'core':
        access(data['core']['repo'], user, writable=True)
        access(Path(data['core']['repo']) / 'scenarioforge/__init__.py', user)
        base = Path(data['core']['base']) / 'runs'
        while not base.exists() and base.parent != base: base = base.parent
        access(base, user, writable=True)
        interface = data['core'].get('interface')
        if interface and not (Path('/sys/class/net') / interface).is_dir():
            raise ValueError('Configured CORE HITL interface does not exist: ' + interface)
        return {'message':'CORE runtime repository and upload workspace are writable; configured HITL interface exists'}
    raise ValueError('Unknown input-check role')


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(sys.argv[1]))))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Guest input checks unavailable; verify files, service account and configuration'
        print(json.dumps({'error':message}))
        raise SystemExit(1)
