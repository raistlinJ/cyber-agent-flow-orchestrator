"""Thin command-line adapter over the application service API."""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].split('=', 1)[0] in ('--runs-root', '--web-config', '--poll-seconds', '--provision-config', '--local'):
        argv.insert(0, 'serve')
    parser = argparse.ArgumentParser(prog='cyber-agent-flow-orchestrator', description='Manage ScenarioForge and CAF evaluation workflows. With no arguments, start serve using editable defaults.')
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Validate host inputs and show guest actions without executing them')
    plan.add_argument('config')
    for name in ('run', 'resume'):
        launch = commands.add_parser(name, help='Run a workflow in the foreground' if name == 'run' else 'Resume a recorded workflow')
        launch.add_argument('config')
        launch.add_argument('--output', required=True)
        launch.add_argument('--resume', action='store_true', help=argparse.SUPPRESS if name == 'resume' else 'Resume existing output')
        launch.add_argument('--retry-steps', action='store_true', help='Explicitly rerun failed/interrupted preparation commands')
        launch.add_argument('--retry-failed', action='store_true', help='Retry failed evaluator trials')
    for name in ('user-run', 'user-resume', 'user-recover'):
        launch = commands.add_parser(name, help='PVE-authenticated workflow in the user workspace')
        if name == 'user-run':
            launch.add_argument('config', help='Host-managed workflow template')
        launch.add_argument('--username', required=True)
        launch.add_argument('--web-config', default='web.yaml')
        launch.add_argument('--runs-root', default='runs', help='Same root used by serve')
        launch.add_argument('--run-id', required=True)
        if name == 'user-resume':
            launch.add_argument('--retry-steps', action='store_true')
            launch.add_argument('--retry-failed', action='store_true')
    cleanup = commands.add_parser('recover', help='Stop and collect journaled jobs without launching new work')
    cleanup.add_argument('--output', required=True)
    listing = commands.add_parser('list', help='List workflow runs under a host directory')
    listing.add_argument('--root', default='runs')
    for name in ('status', 'results', 'export'):
        inspect = commands.add_parser(name, help={'status': 'Inspect workflow stages and evaluator progress',
                                                  'results': 'Show latest trial results and condition summaries',
                                                  'export': 'Write results to a new host directory'}[name])
        inspect.add_argument('output')
        if name in ('results', 'export'):
            inspect.add_argument('--all-attempts', action='store_true')
        if name == 'export':
            inspect.add_argument('--destination', required=True)
    logs = commands.add_parser('logs', help='Read collected stage or trial logs')
    logs.add_argument('output')
    selection = logs.add_mutually_exclusive_group(required=True)
    selection.add_argument('--stage')
    selection.add_argument('--trial')
    logs.add_argument('--attempt', type=int)
    logs.add_argument('--lines', type=int, default=100)
    web = commands.add_parser('serve', help='Start the login-protected dashboard and Python HTTPS proxy')
    web.add_argument('config', nargs='?', help='Workflow YAML (default: workflow.yaml; created on first launch)')
    web.add_argument('--runs-root', default='runs')
    web.add_argument('--web-config', help='Web settings (default: web.yaml; created on first launch)')
    web.add_argument('--local', action='store_true', help='Open without login; bind only to localhost')
    web.add_argument('--poll-seconds', type=int, default=10)
    web.add_argument('--provision-config', help='ScenarioForge Proxmox key=value config; import VM IDs and model into a separate profile')
    provision = commands.add_parser('import-provision', help='Create an editable workflow profile from a ScenarioForge Proxmox provision config')
    provision.add_argument('provision_config')
    provision.add_argument('--workflow', help='Base workflow; defaults to workflow.yaml')
    provision.add_argument('--output', required=True, help='New profile directory; existing files are never replaced')
    maintenance = commands.add_parser('user-app', help='Check, update or roll back a selected application through QGA')
    maintenance.add_argument('action', choices=['inspect', 'update', 'rollback'])
    maintenance.add_argument('--role', required=True, choices=['participant', 'scenarioforge'])
    maintenance.add_argument('--ref', help='Branch, tag or commit (default: configured update ref)')
    maintenance.add_argument('--config', default='workflow.yaml')
    maintenance.add_argument('--web-config', default='web.yaml')
    maintenance.add_argument('--runs-root', default='runs')
    maintenance.add_argument('--username', required=True)
    fusion = commands.add_parser('import-fusion', help='Create local workflow/WebUI settings from a provisioned Fusion lab')
    fusion.add_argument('--state-dir', default=str(Path.home()/'Library/Application Support/ScenarioForge/fusion-lab'))
    fusion.add_argument('--output', required=True, help='New directory for private local settings')
    user = commands.add_parser('create-user', help='Create a local login account using an interactive password prompt')
    user.add_argument('--file', required=True, help='Private JSON account file')
    user.add_argument('--username', required=True)
    user.add_argument('--replace', action='store_true', help='Replace an existing password and revoke its sessions')
    cert = commands.add_parser('create-cert', help='Create a self-signed development TLS certificate in Python')
    cert.add_argument('--hostname', action='append', required=True, help='DNS name or IP SAN; repeat as needed')
    cert.add_argument('--cert', required=True)
    cert.add_argument('--key', required=True)
    cert.add_argument('--days', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        from .compatibility import check_evaluator
        check_evaluator()
        from . import service
        if args.command == 'serve':
            from .bootstrap import prepare
            from .web import serve
            config, web_config = prepare(args.config, args.web_config)
            if args.runs_root == 'runs' and not any(arg == '--runs-root' or arg.startswith('--runs-root=') for arg in argv):
                from .config import load
                _, runtime, _, _ = load(config)
                if runtime['backend']['type'] == 'fusion':
                    args.runs_root = str(Path(config).parent/'runs')
            if args.provision_config:
                from .provision import import_profile
                config = import_profile(args.provision_config, config)
            print(f'Workflow: {config}\nWeb settings: {web_config}\nRuns: {args.runs_root}', flush=True)
            return serve(config, args.runs_root, web_config=web_config, interval=args.poll_seconds, **({"local": True} if args.local else {}))
        if args.command in ('user-run', 'user-resume', 'user-recover'):
            from . import user_execution
            access = user_execution.login(args.web_config, args.username)
            if args.command == 'user-recover':
                user_execution.recover(args.runs_root, args.run_id, access)
                return 0
            return user_execution.run(getattr(args, 'config', None), args.runs_root, args.run_id, access,
                                      resume=args.command == 'user-resume',
                                      retry_steps=getattr(args, 'retry_steps', False),
                                      retry_failed=getattr(args, 'retry_failed', False),
                                      progress=lambda message: print(message, flush=True))
        if args.command == 'user-app':
            import uuid
            from . import user_execution
            from .config import load
            from .tls import settings
            from .updates import UpdateManager, UpdateError
            from .workspaces import Workspace
            from cyber_agent_flow_eval import integration as ev
            web_settings = settings(args.web_config)
            if web_settings['updates'] is None:
                raise UpdateError('Application maintenance is disabled')
            access = user_execution.login(args.web_config, args.username)
            cfg, runtime, _, _ = load(args.config)
            manager = UpdateManager(cfg, runtime, args.runs_root, web_settings['updates'])
            try:
                reply = manager.submit(access, args.role, args.action, args.ref or manager.config[args.role]['ref'], uuid.uuid4().hex)
                manager.jobs[access.username].result()
                result = ev.read_json(Workspace(args.runs_root, access.username).path / 'updates' / reply['id'] / 'job.json')
                print(json.dumps(result, indent=2))
                return 0 if result['status'] == 'completed' else 1
            finally:
                manager.close()
        if args.command == 'import-provision':
            from .bootstrap import prepare
            from .provision import import_profile
            config, _ = prepare(args.workflow)
            result = {'workflow': import_profile(args.provision_config, config, output=args.output)}
        elif args.command == 'import-fusion':
            from .fusion_setup import prepare as prepare_fusion
            result = prepare_fusion(args.state_dir, args.output)
        elif args.command == 'create-user':
            import getpass
            from .auth import create_user
            password = getpass.getpass('Password (at least 12 characters): ')
            if password != getpass.getpass('Confirm password: '):
                raise ValueError('Passwords do not match')
            result = create_user(args.file, args.username, password, replace=args.replace)
        elif args.command == 'create-cert':
            from .tls import create_certificate
            result = create_certificate(args.cert, args.key, args.hostname, args.days)
        elif args.command == 'plan':
            result = service.plan(args.config)
        elif args.command == 'recover':
            service.recover(args.output)
            result = {'output': args.output, 'recovered': True}
        elif args.command in ('run', 'resume'):
            return service.run(args.config, args.output, resume=args.resume or args.command == 'resume',
                               retry_steps=args.retry_steps, retry_failed=args.retry_failed, progress=lambda message: print(message, flush=True))
        elif args.command == 'list':
            result = service.list_runs(args.root)
        elif args.command == 'status':
            result = service.status(args.output)
        elif args.command == 'results':
            result = service.results(args.output, all_attempts=args.all_attempts)
        elif args.command == 'logs':
            result = service.logs(args.output, stage=args.stage, trial=args.trial, attempt=args.attempt, lines=args.lines)
        else:
            result = service.export_results(args.output, args.destination, all_attempts=args.all_attempts)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print('Interrupted. Use recover before inspecting guest state or resuming.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
