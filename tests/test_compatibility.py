import os
from pathlib import Path
import subprocess
import sys

import pytest

from cyber_agent_flow_orchestrator.compatibility import main


def test_actual_installed_api_imports_without_starting_server(capsys):
    assert main() == 0
    assert 'startup imports verified' in capsys.readouterr().out


@pytest.mark.parametrize('command', [[], ['-m', 'cyber_agent_flow_orchestrator.compatibility']])
def test_incomplete_evaluator_fails_clearly_even_if_metadata_says_040(tmp_path, command):
    package = tmp_path / 'cyber_agent_flow_eval'
    package.mkdir()
    (package / '__init__.py').write_text('__version__ = "0.4.0"\n')
    root = Path(__file__).parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(root)]))
    argv = command or ['-m', 'cyber_agent_flow_orchestrator']
    # Disable editable-install meta finders so the deliberately incomplete
    # fixture is used instead of this workstation's working evaluator checkout.
    result = subprocess.run([sys.executable, '-S', *argv], cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert 'Incompatible evaluator' in result.stderr
    assert str(package) in result.stderr
    assert 'integration' in result.stderr
    assert 'uv sync alone cannot restore missing source files' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not (tmp_path / 'web.yaml').exists()
