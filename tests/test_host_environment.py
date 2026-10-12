import os
from pathlib import Path

import pytest
import yaml

from cyber_agent_flow_orchestrator import bootstrap, proxy
from cyber_agent_flow_orchestrator.host_environment import load_environment
from cyber_agent_flow_orchestrator.tls import settings


def secret_file(tmp_path, contents):
    path = tmp_path / 'host-secrets.env'
    path.write_text(contents)
    path.chmod(0o600)
    return path


def test_loads_stored_key_without_shell_evaluation_and_preserves_override(tmp_path, monkeypatch):
    monkeypatch.delenv('TEST_STORED_KEY', raising=False)
    monkeypatch.setenv('TEST_OVERRIDE_KEY', 'shell-key')
    path = secret_file(tmp_path, '# private\nexport TEST_STORED_KEY="literal-$(uname)-$HOME"\nTEST_OVERRIDE_KEY=stored-key\n')
    load_environment(path)
    assert os.environ['TEST_STORED_KEY'] == 'literal-$(uname)-$HOME'
    assert os.environ['TEST_OVERRIDE_KEY'] == 'shell-key'


@pytest.mark.parametrize('contents', [
    'TEST_VALID_KEY=value\nBROKEN',
    'TEST_VALID_KEY=value\nTEST_OTHER_KEY="sensitive-but-unclosed',
    'TEST_VALID_KEY=value\nTEST_VALID_KEY=duplicate',
    'TEST_VALID_KEY=value\nBAD-KEY=secret',
])
def test_bad_file_never_leaks_contents_or_partly_loads(tmp_path, monkeypatch, contents):
    monkeypatch.delenv('TEST_VALID_KEY', raising=False)
    with pytest.raises(ValueError) as exc:
        load_environment(secret_file(tmp_path, contents))
    assert 'sensitive' not in str(exc.value)
    assert 'TEST_VALID_KEY' not in os.environ


@pytest.mark.parametrize('unsafe', ['permissions', 'symlink', 'directory', 'size'])
def test_rejects_unsafe_host_file(tmp_path, unsafe):
    path = secret_file(tmp_path, 'TEST_KEY=private\n')
    if unsafe == 'permissions':
        path.chmod(0o644)
    elif unsafe == 'symlink':
        target = path
        path = tmp_path / 'linked.env'
        path.symlink_to(target)
    elif unsafe == 'directory':
        path = tmp_path
    else:
        path.write_bytes(b'x' * 65537)
    with pytest.raises(ValueError):
        load_environment(path)


def test_serve_loads_configured_host_file_before_starting_dashboard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('TEST_STARTUP_JUDGE_KEY', raising=False)
    workflow, web = bootstrap.prepare()
    secret_file(tmp_path, 'TEST_STARTUP_JUDGE_KEY=stored-secret\n')
    config = yaml.safe_load(Path(web).read_text())
    config['host_environment_file'] = 'host-secrets.env'
    Path(web).write_text(yaml.safe_dump(config))
    assert settings(web)['host_environment_file'] == str(tmp_path / 'host-secrets.env')
    # Stop before binding or contacting guests; credential loading must precede
    # creation of dashboard workers that run synthetic preflight and judging.
    class ReachedStartup(Exception):
        pass
    def before_tls(config):
        assert os.environ['TEST_STARTUP_JUDGE_KEY'] == 'stored-secret'
        raise ReachedStartup
    monkeypatch.setattr(proxy, 'ensure_certificate', before_tls)
    with pytest.raises(ReachedStartup):
        proxy.run_https(workflow, tmp_path / 'runs', web)
