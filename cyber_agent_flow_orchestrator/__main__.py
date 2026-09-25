"""CLI entry point. Planning performs no guest actions."""
import argparse
import json
from .config import load
from .workflow import run, recover, execute_command


def main(argv=None):
    parser = argparse.ArgumentParser(description='Orchestrate ScenarioForge and CAF evaluations from a Proxmox host')
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Validate host inputs and show guest actions without executing them')
    plan.add_argument('config')
    launch = commands.add_parser('run')
    launch.add_argument('config')
    launch.add_argument('--output', required=True)
    launch.add_argument('--resume', action='store_true')
    launch.add_argument('--retry-steps', action='store_true', help='Explicitly rerun failed/interrupted preparation commands')
    launch.add_argument('--retry-failed', action='store_true', help='Retry failed evaluator trials')
    cleanup = commands.add_parser('recover', help='Stop journaled jobs and recover trial outputs; never start new work')
    cleanup.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            cfg, runtime, _, identity = load(args.config)
            print(json.dumps({'workflow_id': cfg['id'], 'workflow_hash': identity,
                              'backend': runtime['backend'], 'scenarioforge': cfg['scenarioforge'],
                              'deploy_command': execute_command(cfg['scenarioforge'], runtime['backend'], '<run-id>') if cfg['scenarioforge']['mode'] == 'execute' else None,
                              'prepare': cfg['prepare'], 'artifacts': cfg['artifacts'], 'collect': cfg['collect'],
                              'conditions': [c['id'] for c in runtime['conditions']],
                              'repetitions': runtime.get('repetitions', 1),
                              'trial_count': 'exported tasks × conditions × repetitions; finalized after suite import'}, indent=2))
            return 0
        if args.command == 'recover':
            recover(args.output)
            return 0
        return run(args.config, args.output, resume=args.resume,
                   retry_steps=args.retry_steps, retry_failed=args.retry_failed)
    except KeyboardInterrupt:
        print('Interrupted. Use recover before inspecting guest state or resuming.')
        return 130
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
