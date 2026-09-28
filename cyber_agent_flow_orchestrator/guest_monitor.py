"""Read-only, stdlib-only Linux guest probe, sent through QEMU Guest Agent."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time


def redact(argv):
    result, hidden = [], False
    for arg in argv:
        if hidden:
            result.append('[redacted]')
            hidden = False
            continue
        if re.search(r'(?i)authorization\s*:', arg):
            result.append(re.sub(r'(?i)(authorization\s*:).*', r'\1 [redacted]', arg))
            continue
        if re.fullmatch(r'(?i)[a-z0-9_-]*(password|passwd|api[-_]?key|token|secret|authorization)[a-z0-9_-]*', arg.split('=', 1)[0]):
            if '=' in arg:
                result.append(arg.split('=', 1)[0] + '=[redacted]')
            else:
                result.append(arg)
                hidden = True
        else:
            arg = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[redacted]@', arg)
            arg = re.sub(r'(?i)([?&](?:token|api_key|password|secret)=)[^&\s]+', r'\1[redacted]', arg)
            result.append(arg)
    return shlex.join(result)[:2000]


def unit_status(unit):
    keys = ['LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainStartTimestampMonotonic']
    result = subprocess.run(['systemctl', 'show', unit, '--no-pager', *['--property=' + k for k in keys]],
                            capture_output=True, text=True, timeout=2)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if not values.get('LoadState'):
        raise ValueError(result.stderr.strip()[:300] or 'systemd status unavailable')
    return values


def inspect(data, proc_root="/proc"):
    proc_root = Path(proc_root)
    role, root = data['role'], data.get('root')
    uptime = float((proc_root / 'uptime').read_text().split()[0])
    ticks = os.sysconf('SC_CLK_TCK')
    services = []
    for unit in data.get('units', [])[:12]:
        try:
            values = unit_status(unit)
            services.append(dict(unit=unit, **values))
        except Exception as exc:
            services.append({'unit': unit, 'error': str(exc)})
    processes = {}
    for path in proc_root.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            argv = (path / 'cmdline').read_bytes()[:16384].decode(errors='replace').strip('\0').split('\0')
            if not argv or not argv[0]:
                continue
            try:
                cwd = str((path / 'cwd').resolve(strict=True))
            except OSError:
                cwd = ''
            processes[int(path.name)] = {'ppid': int(fields[1]), 'argv': argv, 'cwd': cwd,
                                         'elapsed_seconds': max(0, uptime - int(fields[19]) / ticks)}
        except (OSError, ValueError, IndexError):
            continue
    excluded = {os.getpid()}
    pid = os.getpid()
    while pid in processes and processes[pid]['ppid'] not in excluded:
        pid = processes[pid]['ppid']
        excluded.add(pid)
    selected = set()
    for pid, proc in processes.items():
        if pid in excluded:
            continue
        argv = proc['argv']
        in_root = root and (proc['cwd'] == root or proc['cwd'].startswith(root.rstrip('/') + '/') or
                            any(a.startswith(root.rstrip('/') + '/') for a in argv if a.startswith('/')))
        modules = {'scenarioforge': {'webapp.app_backend', 'scenarioforge.cli'},
                   'participant': {'cyber_agent_flow_eval.worker'}, 'core': {'core.daemon'}}[role]
        worker = role == 'participant' and any(a.endswith('/cyber_agent_flow_eval/worker.py') for a in argv)
        daemon = role == 'core' and any(Path(a).name == 'core-daemon' for a in argv[:2])
        if in_root or modules.intersection(argv) or worker or daemon:
            selected.add(pid)
    for unit in services:
        if unit.get('ActiveState') in ('active', 'activating') and unit.get('SubState') not in ('exited', 'dead'):
            pid = int(unit.get('MainPID', 0))
            if pid in processes and pid not in excluded:
                selected.add(pid)
    # Include tool children of the application/worker even when they change cwd.
    for _ in range(20):
        children = {pid for pid, proc in processes.items() if proc['ppid'] in selected and pid not in excluded}
        before = len(selected)
        selected.update(children)
        if len(selected) == before:
            break
    result = [{'pid': pid, 'command': redact(processes[pid]['argv']),
               'elapsed_seconds': round(processes[pid]['elapsed_seconds'], 1)} for pid in sorted(selected)[:40]]
    marker = {'scenarioforge': 'scenarioforge/cli.py', 'participant': 'mcp_client.py'}.get(role)
    present = (Path(root, marker).is_file() if root and marker else
               bool(shutil.which('core-daemon')) or any(s.get('unit') == data.get('service_unit') and s.get('LoadState') == 'loaded' for s in services))
    return {'application_present': present, 'root': root, 'processes': result, 'services': services,
            'processes_truncated': len(selected) > 40, 'guest_uptime_seconds': uptime, 'guest_monotonic_seconds': time.monotonic()}


if __name__ == '__main__':
    try:
        print(json.dumps(inspect(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)
