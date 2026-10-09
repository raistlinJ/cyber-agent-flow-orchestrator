"""Check the evaluator's actual host API, not just its package version."""
import importlib
import inspect
import sys


def check_evaluator():
    location = 'the configured cyber-agent-flow-eval checkout'
    try:
        package = importlib.import_module('cyber_agent_flow_eval')
        location = str(package.__file__)
        modules = {
            'integration': ('GuestAgent', 'ProxmoxBackend', 'TargetReservation', 'StrictLoader',
                            'read_runtime', 'recover', 'import_config', 'load_suite', 'require_ready',
                            'resolve_backend', 'resolve', 'source_identity', 'lease', 'run',
                            'digest', 'fields', 'identifier', 'positive', 'read_json', 'write_json'),
            'reporting': ('active', 'status', 'results', 'logs', 'tail'),
            'proxmox': ('authorized_operations', 'UPLOAD_CHUNK'),
            'guest_agent': ('MAINTENANCE_LOCK', 'MAINTENANCE_PENDING', 'RPC_INPUT_LIMIT', 'SUPPORTED_OPERATIONS'),
            'fusion': ('vm_lock_path',),
            'rubric': ('validate_rubric', 'participant_scaffold', 'aggregate'),
            'studies': ('compare', 'summarize'),
            'usage': ('validate_pricing',),
        }
        for name, symbols in modules.items():
            module = importlib.import_module('cyber_agent_flow_eval.' + name)
            for symbol in symbols:
                if not hasattr(module, symbol):
                    raise ImportError(f'cyber_agent_flow_eval.{name}.{symbol} is missing')
        runner = importlib.import_module('cyber_agent_flow_eval.runner')
        if 'prepare_trial' not in inspect.signature(runner.run).parameters:
            raise ImportError('Evaluator runner.run lacks per-trial preparation support')
        guest = importlib.import_module('cyber_agent_flow_eval.guest_agent')
        required = {'preflight', 'start', 'stop', 'status', 'hook', 'stat', 'write', 'hint_request', 'hint_reply'}
        if not required.issubset(guest.SUPPORTED_OPERATIONS):
            missing = ', '.join(sorted(required - guest.SUPPORTED_OPERATIONS))
            raise ImportError('Guest helper operations are missing: ' + missing)
    except ImportError as exc:
        raise RuntimeError(
            f'Incompatible evaluator at {location}: {exc}. '
            'This orchestrator requires the evaluator host API (0.5.0+), including rubric scoring, studies, pricing, integration.py, '
            'reporting.py, Proxmox authorization, guest maintenance locking, preflight/reset-hook cleanup, shared VM locks and stdin uploads. Update the evaluator checkout '
            'to a revision containing those files, then rerun python3 install.py. '
            'uv sync alone cannot restore missing source files.'
        ) from exc


def main():
    try:
        check_evaluator()
        # Exercise startup imports without launching HTTPS, creating certificates,
        # logging into PVE, or running any guest commands.
        for name in ('__main__', 'service', 'proxy', 'user_execution'):
            importlib.import_module('cyber_agent_flow_orchestrator.' + name)
    except (ImportError, RuntimeError) as exc:
        print(f'Installation compatibility check failed: {exc}', file=sys.stderr)
        return 2
    print('Evaluator host API and orchestrator startup imports verified.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
