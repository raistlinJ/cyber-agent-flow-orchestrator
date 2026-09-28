#!/usr/bin/env python3
"""Interactive bootstrap for the standard sibling-checkout uv installation.

Uses only the Python standard library, so it works before `uv sync`.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

DEFAULT_EVAL_URL = 'https://github.com/raistlinJ/cyber-agent-flow-eval.git'
DEFAULT_EVAL_REF = 'main'


def valid_checkout(path):
    return (path / 'pyproject.toml').is_file() and (path / 'cyber_agent_flow_eval/__init__.py').is_file()


def ensure_evaluator(destination, repository, ref, *, yes=False):
    if destination.exists() or destination.is_symlink():
        if not valid_checkout(destination):
            raise ValueError(f'{destination} already exists but is not an evaluator checkout; it was not changed')
        print(f'Using existing evaluator: {destination} (no fetch, pull or checkout)', flush=True)
        return True
    if not shutil.which('git'):
        raise ValueError('git is required to download the evaluator; install git and retry')
    print(f'cyber-agent-flow-eval is missing at {destination}.\n'
          f'Repository: {repository}\nRevision: {ref}', flush=True)
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError('No interactive terminal. Rerun interactively, clone the evaluator manually, '
                             'or use --yes to authorize the download')
        try:
            answer = input('Download cyber-agent-flow-eval and continue installation? [y/N] ').strip().lower()
        except EOFError:
            answer = ''
        if answer not in ('y', 'yes'):
            print('Download declined. Installation stopped without running uv sync.', flush=True)
            return False
    # Claim a new directory exclusively; never clean up a pre-existing checkout.
    destination.mkdir()
    try:
        subprocess.run(['git', 'clone', '--no-checkout', '--', repository, str(destination)], check=True)
        subprocess.run(['git', '-C', str(destination), 'fetch', '--depth=1', 'origin', ref], check=True)
        subprocess.run(['git', '-C', str(destination), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
        if not valid_checkout(destination):
            raise ValueError('Downloaded repository/revision is not a cyber-agent-flow-eval checkout')
    except BaseException:
        shutil.rmtree(destination)
        raise
    return True


def main(argv=None, *, project=None):
    parser = argparse.ArgumentParser(description='Ask before downloading a missing evaluator, then run uv sync.')
    parser.add_argument('--yes', action='store_true', help='Authorize downloading a missing evaluator without prompting')
    parser.add_argument('--eval-url', default=DEFAULT_EVAL_URL, help='Evaluator Git repository URL')
    parser.add_argument('--eval-ref', default=DEFAULT_EVAL_REF, help='Evaluator Git branch, tag or commit (default: main)')
    parser.add_argument('sync_args', nargs=argparse.REMAINDER, help='Optional uv sync arguments after --')
    args = parser.parse_args(argv)
    project = Path(project or __file__).resolve()
    if project.is_file():
        project = project.parent
    try:
        uv = shutil.which('uv')
        if not uv:
            raise ValueError('uv is required; install uv and rerun python3 install.py')
        if not args.eval_ref or args.eval_ref.startswith('-') or any(c.isspace() for c in args.eval_ref):
            raise ValueError('--eval-ref must name a Git branch, tag or commit')
        destination = project.parent / 'cyber-agent-flow-eval'
        if not ensure_evaluator(destination, args.eval_url, args.eval_ref, yes=args.yes):
            return 1
        sync_args = args.sync_args[1:] if args.sync_args[:1] == ['--'] else args.sync_args
        print('Installing orchestrator dependencies with uv sync...', flush=True)
        result = subprocess.run([uv, 'sync', *sync_args], cwd=project)
        if result.returncode == 0:
            result = subprocess.run([uv, 'run', '--no-sync', 'python', '-m',
                                     'cyber_agent_flow_orchestrator.compatibility'], cwd=project)
            if result.returncode:
                print('Dependencies synced, but the installation is not ready. '
                      'The evaluator checkout was preserved; update it to a compatible revision.', file=sys.stderr)
                return result.returncode
            print('Installed. Start from the orchestrator directory:\n'
                  '  uv run cyber-agent-flow-orchestrator\n'
                  'Missing certificates are created on first launch; existing pairs are preserved.', flush=True)
        return result.returncode
    except KeyboardInterrupt:
        print('\nInstallation interrupted.', file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
