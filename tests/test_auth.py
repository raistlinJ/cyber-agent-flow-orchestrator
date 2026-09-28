import json
import os
from pathlib import Path

import pytest
import yaml

from cyber_agent_flow_orchestrator.auth import Auth, create_user, read_users, token_from_cookie
from cyber_agent_flow_orchestrator.tls import create_certificate, context, settings
from https_fixture import PASSWORD


def test_accounts_are_private_argon2_hashes_and_existing_user_requires_replace(tmp_path):
    path = tmp_path / 'users.json'
    create_user(path, 'operator', PASSWORD)
    assert path.stat().st_mode & 0o077 == 0
    assert PASSWORD not in path.read_text()
    assert read_users(path)['operator'].startswith('$argon2id$')
    with pytest.raises(ValueError, match='already exists'):
        create_user(path, 'operator', PASSWORD)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='owner only'):
        read_users(path)


def test_idle_and_absolute_expiration_logout_and_restart(tmp_path):
    path = tmp_path / 'users.json'
    create_user(path, 'operator', PASSWORD)
    clock = [0]
    auth = Auth(path, idle_seconds=10, absolute_seconds=25, clock=lambda: clock[0])
    token = auth.login('operator', PASSWORD, 'peer')
    assert auth.session(token)['username'] == 'operator'
    clock[0] = 11
    assert auth.session(token) is None
    token = auth.login('operator', PASSWORD, 'peer')
    for now in (19, 27, 35):
        clock[0] = now
        assert auth.session(token)
    clock[0] = 36
    assert auth.session(token) is None
    token = auth.login('operator', PASSWORD, 'peer')
    assert Auth(path).session(token) is None
    auth.logout(token)
    assert auth.session(token) is None


def test_bad_user_and_bad_password_have_same_result_and_forged_tokens_fail(tmp_path):
    path = tmp_path / 'users.json'
    create_user(path, 'operator', PASSWORD)
    auth = Auth(path)
    assert auth.login('unknown', PASSWORD, 'peer') is None
    assert auth.login('operator', 'wrong', 'peer') is None
    assert auth.session('x' * 43) is None
    assert token_from_cookie('broken') is None


def test_certificate_generation_private_permissions_san_and_no_overwrite(tmp_path):
    from cryptography import x509
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    create_certificate(cert, key, ['localhost', '127.0.0.1'])
    assert key.stat().st_mode & 0o077 == 0
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    assert parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == ['localhost']
    context({'certificate': str(cert), 'private_key': str(key)})
    with pytest.raises(ValueError, match='new paths'):
        create_certificate(cert, key, ['localhost'])
    key.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        context({'certificate': str(cert), 'private_key': str(key)})


def test_web_config_requires_https_and_resolves_relative_paths(tmp_path):
    path = tmp_path / 'web.yaml'
    data = dict(version=1, public_url='https://LOCALHOST:8443', certificate='cert.pem', private_key='key.pem', users_file='users.json')
    path.write_text(yaml.safe_dump(data))
    resolved = settings(path)
    assert resolved['users_file'] == str(tmp_path / 'users.json')
    assert resolved['public_url'] == 'https://localhost:8443'
    for origin in ['http://localhost:8443', 'https://localhost:8443/path', 'https://user:pass@localhost:8443', 'https://localhost:9999']:
        path.write_text(yaml.safe_dump(dict(data, public_url=origin)))
        with pytest.raises(ValueError):
            settings(path)


def test_cli_user_creation_never_prints_password(tmp_path, monkeypatch, capsys):
    import getpass
    from cyber_agent_flow_orchestrator.__main__ import main
    monkeypatch.setattr(getpass, 'getpass', lambda prompt: PASSWORD)
    path = tmp_path / 'users.json'
    assert main(['create-user', '--file', str(path), '--username', 'admin']) == 0
    output = capsys.readouterr().out
    assert PASSWORD not in output and '$argon2' not in output
    assert json.loads(output)['username'] == 'admin'
