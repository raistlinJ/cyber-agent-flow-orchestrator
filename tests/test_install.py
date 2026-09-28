"""Installer tests use a local Git repository; no remote downloads."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

spec = importlib.util.spec_from_file_location('caf_install', Path(__file__).parents[1] / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / 'source'
    path.mkdir()
    (path / 'pyproject.toml').write_text('[project]\nname="cyber-agent-flow-eval"\nversion="0.4.0"\n')
    (path / 'cyber_agent_flow_eval').mkdir()
    (path / 'cyber_agent_flow_eval/__init__.py').write_text('')
    subprocess.run(['git', 'init', '-b', 'main', str(path)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-m', 'fixture'], check=True, capture_output=True)
    return path


def test_accept_download_checks_out_requested_revision(checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    prompts = []
    monkeypatch.setattr('builtins.input', lambda prompt: prompts.append(prompt) or 'yes')
    destination = tmp_path / 'download'
    assert installer.ensure_evaluator(destination, str(checkout), 'main')
    assert installer.valid_checkout(destination)
    assert len(prompts) == 1 and '[y/N]' in prompts[0]
    revision = lambda p: subprocess.check_output(['git', '-C', str(p), 'rev-parse', 'HEAD'])
    assert revision(destination) == revision(checkout)


@pytest.mark.parametrize('answer', ['n', '', 'anything else'])
def test_decline_leaves_no_download_or_sync(tmp_path, monkeypatch, answer):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: answer)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: pytest.fail('Must not run git or uv'))
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/bin/' + name)
    project = tmp_path / 'orchestrator'
    project.mkdir()
    assert installer.main([], project=project) == 1
    assert not (tmp_path / 'cyber-agent-flow-eval').exists()


def test_existing_checkout_is_preserved_and_sync_runs(checkout, tmp_path, monkeypatch):
    destination = tmp_path / 'cyber-agent-flow-eval'
    checkout.rename(destination)
    (destination / 'local-edit').write_text('keep this')
    project = tmp_path / 'orchestrator'
    project.mkdir()
    calls = []
    monkeypatch.setattr('builtins.input', lambda prompt: pytest.fail('Existing checkout must not prompt'))
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: calls.append((a, k)) or subprocess.CompletedProcess(a, 0))
    assert installer.main(['--', '--group', 'dev'], project=project) == 0
    assert calls == [((['/usr/bin/uv', 'sync', '--group', 'dev'],), {'cwd': project}),
                     ((['/usr/bin/uv', 'run', '--no-sync', 'python', '-m',
                        'cyber_agent_flow_orchestrator.compatibility'],), {'cwd': project})]
    assert (destination / 'local-edit').read_text() == 'keep this'


def test_noninteractive_requires_explicit_download_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    with pytest.raises(ValueError, match='--yes'):
        installer.ensure_evaluator(tmp_path / 'missing', 'unused', 'main')
    assert not (tmp_path / 'missing').exists()


def test_failed_download_removes_only_its_new_directory(checkout, tmp_path):
    destination = tmp_path / 'download'
    with pytest.raises(subprocess.CalledProcessError):
        installer.ensure_evaluator(destination, str(checkout), 'no-such-ref', yes=True)
    assert not destination.exists()
    destination.mkdir()
    (destination / 'keep').write_text('existing data')
    with pytest.raises(ValueError, match='was not changed'):
        installer.ensure_evaluator(destination, str(checkout), 'main', yes=True)
    assert (destination / 'keep').read_text() == 'existing data'


def test_sync_failure_is_reported_and_download_is_preserved(checkout, tmp_path, monkeypatch):
    checkout.rename(tmp_path / 'cyber-agent-flow-eval')
    project = tmp_path / 'orchestrator'
    project.mkdir()
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 7))
    assert installer.main([], project=project) == 7
    assert installer.valid_checkout(tmp_path / 'cyber-agent-flow-eval')


def test_failed_import_check_does_not_report_installed(checkout, tmp_path, monkeypatch, capsys):
    checkout.rename(tmp_path / 'cyber-agent-flow-eval')
    project = tmp_path / 'orchestrator'
    project.mkdir()
    monkeypatch.setattr(installer.shutil, 'which', lambda name: '/usr/bin/' + name)
    statuses = iter([0, 2])
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, next(statuses)))
    assert installer.main([], project=project) == 2
    output = capsys.readouterr()
    assert 'Installed. Start' not in output.out
    assert 'not ready' in output.err
    assert installer.valid_checkout(tmp_path / 'cyber-agent-flow-eval')
