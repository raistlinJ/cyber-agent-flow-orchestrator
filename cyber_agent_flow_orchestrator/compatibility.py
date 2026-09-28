"""Check the evaluator's actual host API, not just its package version."""
import importlib
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
            'proxmox': ('authorized_operations',),
        }
        for name, symbols in modules.items():
            module = importlib.import_module('cyber_agent_flow_eval.' + name)
            for symbol in symbols:
                if not hasattr(module, symbol):
                    raise ImportError(f'cyber_agent_flow_eval.{name}.{symbol} is missing')
    except ImportError as exc:
        raise RuntimeError(
            f'Incompatible evaluator at {location}: {exc}. '
            'This orchestrator requires the evaluator host API (0.4.0+), including integration.py, '
            'reporting.py and the Proxmox authorization support. Update the evaluator checkout '
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
