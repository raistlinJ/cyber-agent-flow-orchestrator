import json
import os
from pathlib import Path

import pytest

from cyber_agent_flow_orchestrator.judge_credentials import bind, restore, status, PREFIX
from cyber_agent_flow_orchestrator.workspaces import Workspace


def config(url='https://judge.invalid/v1'):
    return {'enabled': True, 'model': {'url': url, 'api_key_env': 'ADMIN_JUDGE_KEY'}}


def forget(monkeypatch, *configs):
    for cfg in configs:
        monkeypatch.delenv(cfg['model']['api_key_env'], raising=False)


def test_key_replacement_blank_clear_and_restart_keep_frozen_run_keys(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, 'one@pve')
    original = bind(workspace, config(), 'first-private-key', persist=True)
    first = original['model']['api_key_env']
    assert first.startswith(PREFIX) and os.environ[first] == 'first-private-key'
    assert 'first-private-key' not in json.dumps(original)
    assert bind(workspace, config(), persist=True) == original
    replacement = bind(workspace, config(), 'second-private-key', persist=True)
    second = replacement['model']['api_key_env']
    assert first != second
    assert bind(workspace, config(), persist=True) == replacement
    cleared = bind(workspace, config(), clear=True, persist=True)
    assert 'api_key_env' not in cleared['model']
    assert bind(workspace, config(), persist=True) == cleared
    # Simulate a process restart; historical experiments hydrate their original
    # immutable key even after a newer key was selected and then cleared.
    forget(monkeypatch, original, replacement)
    restore(workspace, {'judge': original})
    restore(workspace, {'judge': replacement})
    assert os.environ[first] == 'first-private-key'
    assert os.environ[second] == 'second-private-key'
    for path in (workspace.path / 'judge-credentials').glob('*.json'):
        assert path.stat().st_mode & 0o077 == 0
    assert not list(workspace.runs.rglob('*'))
    forget(monkeypatch, original, replacement)


def test_blank_preserves_admin_host_key_and_endpoint_isolation(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, 'one@pve')
    assert bind(workspace, config(), persist=True) == config()
    saved = bind(workspace, config(), 'private-key', persist=True)
    assert bind(workspace, config('https://other.invalid/v1'), persist=True) == config('https://other.invalid/v1')
    other = Workspace(tmp_path, 'two@pve')
    assert bind(other, config(), persist=True) == config()
    with pytest.raises(ValueError, match='unavailable for this account'):
        restore(other, {'judge': saved})
    altered = config('https://other.invalid/v1')
    altered['model']['api_key_env'] = saved['model']['api_key_env']
    with pytest.raises(ValueError, match='unavailable for this account'):
        restore(workspace, {'judge': altered})
    forget(monkeypatch, saved)


def test_validation_does_not_persist_entered_key(tmp_path):
    workspace = Workspace(tmp_path, 'one@pve')
    checked = bind(workspace, config(), 'do-not-persist')
    assert checked['model']['api_key_env'] not in os.environ
    assert not list((workspace.path / 'judge-credentials').glob('*.json'))


@pytest.mark.parametrize('key,clear', [('secret', True), (None, False), ('line\nbreak', False), ('x'*8193, False), ('', 'yes')])
def test_bad_input_never_echoes_secret(tmp_path, key, clear):
    with pytest.raises(ValueError) as exc:
        bind(Workspace(tmp_path, 'one@pve'), config(), key, clear, persist=True)
    assert 'secret' not in str(exc.value)


def test_symlink_credentials_are_rejected(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, 'one@pve')
    saved = bind(workspace, config(), 'private-key', persist=True)
    path = workspace.path / 'judge-credentials' / (saved['model']['api_key_env'] + '.json')
    target = tmp_path / 'target.json'
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ValueError, match='Cannot open'):
        restore(workspace, {'judge': saved})
    forget(monkeypatch, saved)


def test_status_never_returns_keys_and_uses_same_precedence_as_judge(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, 'one@pve')
    monkeypatch.setenv('ADMIN_JUDGE_KEY', 'host-private-key')
    monkeypatch.delenv('GUEST_ONLY_KEY', raising=False)
    runtime = {'judge': config(), 'model': {'url': config()['model']['url'], 'api_key_env': 'GUEST_ONLY_KEY'}}
    assert status(workspace, runtime) == [{'endpoint': config()['model']['url'], 'configured': True, 'source': 'host environment'}]
    saved = bind(workspace, config(), 'saved-private-key', persist=True)
    assert status(workspace, runtime) == [{'endpoint': config()['model']['url'], 'configured': True, 'source': 'saved judge key'}]
    assert 'private-key' not in json.dumps(status(workspace, runtime))
    assert PREFIX not in json.dumps(status(workspace, runtime))
    bind(workspace, config(), clear=True, persist=True)
    assert status(workspace, runtime)[0]['configured'] is False
    forget(monkeypatch, saved)
