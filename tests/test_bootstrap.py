from pathlib import Path

from cryptography import x509
import pytest
import yaml

from cyber_agent_flow_orchestrator import bootstrap, web
from cyber_agent_flow_orchestrator.__main__ import main
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.tls import context, ensure_certificate, settings


@pytest.mark.parametrize('argv,root,interval', [([], 'runs', 10), (['serve'], 'runs', 10),
    (['--runs-root=results', '--poll-seconds', '5'], 'results', 5)])
def test_default_launch_creates_valid_persistent_configs(tmp_path, monkeypatch, argv, root, interval):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap.socket, 'getfqdn', lambda: 'node.lab')
    calls = []
    monkeypatch.setattr(web, 'serve', lambda *a, **kw: calls.append((a, kw)) or 0)
    assert main(argv) == 0
    args, kwargs = calls[-1]
    cfg, runtime, _, _ = load(args[0])
    assert args[1] == root and kwargs['interval'] == interval
    assert cfg['scenarioforge']['mode'] == 'reuse_export'
    assert runtime['backend']['type'] == 'proxmox'
    config = settings(kwargs['web_config'])
    assert config['auth']['url'] == 'https://node.lab:8006'
    assert config['auth']['required_group']=='caf-orchestration'
    assert config['certificate'] == str(tmp_path/'certs/cert.pem')
    originals = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(bootstrap.socket, 'getfqdn', lambda: 'changed.lab')
    assert main(argv) == 0
    assert all(p.read_bytes() == contents for p, contents in originals.items())


def test_explicit_paths_and_missing_config_do_not_get_replaced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(['serve', 'missing.yaml']) == 2
    assert not list(tmp_path.iterdir())
    workflow, web_config = bootstrap.prepare()
    monkeypatch.chdir(tmp_path.parent)
    assert bootstrap.prepare(workflow, web_config) == (workflow, web_config)


def test_existing_legacy_enrollment_config_is_preserved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workflow, web_config = bootstrap.prepare()
    path = Path(web_config)
    original = path.read_text().replace('caf-orchestration', 'caf-orchestrator')
    path.write_text(original)
    bootstrap.prepare(workflow, web_config)
    assert path.read_text() == original
    assert settings(web_config)['auth']['required_group']=='caf-orchestrator'


def test_help_has_no_setup_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as result:
        main(['--help'])
    assert result.value.code == 0
    assert not list(tmp_path.iterdir())


def test_first_launch_cert_is_usable_and_existing_pair_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr('socket.gethostname', lambda: 'node')
    monkeypatch.setattr('socket.getfqdn', lambda: 'node.lab')
    cfg = dict(public_url='https://orchestrator.lab:8443', certificate=str(tmp_path / 'cert.pem'),
               private_key=str(tmp_path / 'key.pem'))
    assert ensure_certificate(cfg)
    context(cfg)
    cert, key = Path(cfg['certificate']), Path(cfg['private_key'])
    contents = cert.read_bytes(), key.read_bytes()
    parsed = x509.load_pem_x509_certificate(contents[0])
    names = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert {'orchestrator.lab', 'localhost', 'node', 'node.lab'} <= set(names.get_values_for_type(x509.DNSName))
    assert not key.stat().st_mode & 0o077
    assert not ensure_certificate(cfg)
    assert (cert.read_bytes(), key.read_bytes()) == contents
    key.unlink()
    with pytest.raises(ValueError, match='Incomplete'):
        ensure_certificate(cfg)
    assert cert.read_bytes() == contents[0] and not key.exists()


def test_broken_certificate_symlink_is_not_followed_for_generation(tmp_path):
    (tmp_path / 'cert.pem').symlink_to(tmp_path / 'missing.pem')
    path = tmp_path / 'web.yaml'
    path.write_text(yaml.safe_dump(dict(version=1, public_url='https://localhost:8443',
        certificate='cert.pem', private_key='key.pem', users_file='users.json')))
    with pytest.raises(ValueError, match='Incomplete'):
        ensure_certificate(settings(path))
    assert not (tmp_path / 'missing.pem').exists()
    assert not (tmp_path / 'key.pem').exists()


def test_cli_first_launch_serves_https_with_new_certificate(tmp_path):
    import socket
    import ssl
    import subprocess
    import sys
    import time
    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    from cyber_agent_flow_orchestrator.auth import create_user

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    create_user(tmp_path / 'users.json', 'operator', 'first-launch-test-password')
    web_config = tmp_path / 'custom-web.yaml'
    origin = f'https://localhost:{port}'
    web_config.write_text(yaml.safe_dump(dict(version=1, public_url=origin, port=port,
        certificate='cert.pem', private_key='key.pem', users_file='users.json')))
    with (tmp_path / 'server.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'cyber_agent_flow_orchestrator',
            '--web-config', str(web_config)], cwd=tmp_path, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    trust = ssl.create_default_context(cafile=str(tmp_path / 'cert.pem'))
                    with urlopen(origin + '/login', context=trust, timeout=1) as response:
                        assert response.status == 200
                    break
                except (FileNotFoundError, ConnectionError, URLError):
                    time.sleep(.1)
            else:
                log.seek(0)
                pytest.fail('Server did not become ready: ' + log.read())
            with pytest.raises(HTTPError) as denied:
                urlopen(origin + '/api/status', context=trust, timeout=2)
            assert denied.value.code == 401
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_non_proxmox_default_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, path = bootstrap.prepare()
    assert settings(path)["auth"]["provider"] == "pve"
    assert "users_file" not in settings(path)


def test_local_flag_forwarded_to_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls=[]
    monkeypatch.setattr(web, 'serve', lambda *a, **kw: calls.append(kw) or 0)
    assert main(['--local']) == 0
    assert calls[-1]['local'] is True
    assert settings('web.yaml')['auth']['provider'] == 'pve'
    config=settings('web.yaml',local=True)
    assert config['listen']=='127.0.0.1'
    assert config['public_url']=='https://localhost:8443'
    assert config['auth']['provider']=='desktop'
