from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import guest_monitor, monitor
from cyber_agent_flow_orchestrator.config import load
from test_workflow import lab


class Probe:
    def __init__(self):
        self.vms = {9402: {'present': True, 'power': 'running'}, 9403: {'present': True, 'power': 'running'},
                    9401: {'present': False, 'power': 'missing'}}
        self.calls = []
        self.fail_guest = False

    def vm(self, vmid):
        return self.vms[vmid]

    def guest(self, vmid, definition, units):
        self.calls.append(vmid)
        if self.fail_guest:
            raise ValueError('Guest agent unreachable')
        return {'application_present': True, 'guest_uptime_seconds': 900, 'guest_monotonic_seconds': 700,
                'processes': [{'pid': 31, 'command': '/usr/bin/python application.py', 'elapsed_seconds': 120}],
                'services': [{'unit': unit, 'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
                              'MainPID': '31', 'ExecMainStartTimestampMonotonic': '580000000'} for unit in units]}


def configuration(lab):
    path = lab[0]
    cfg = yaml.safe_load(path.read_text())
    cfg['monitoring'] = {'core_vmid': 9401}
    path.write_text(yaml.safe_dump(cfg))
    cfg, runtime, _, _ = load(path)
    return cfg, runtime


def test_distinguishes_vm_presence_guest_access_and_application(lab, tmp_path):
    cfg, runtime = configuration(lab)
    probe = Probe()
    data = monitor.snapshot(cfg, runtime, tmp_path / 'no-runs', probe)
    assert data['vms'][0]['guest_access'] == 'reachable'
    assert data['vms'][2]['power'] == 'missing'
    assert data['vms'][2]['application_present'] is None
    assert 9401 not in probe.calls
    probe.fail_guest = True
    data = monitor.snapshot(cfg, runtime, tmp_path / 'no-runs', probe)
    assert data['vms'][0]['present'] is True and data['vms'][0]['guest_access'] == 'unavailable'
    assert data['vms'][0]['application_present'] is None


def test_unknown_host_and_unconfigured_core_are_not_reported_as_missing(lab, tmp_path):
    cfg, runtime, _, _ = load(lab[0])
    class Broken(Probe):
        def vm(self, vmid):
            raise FileNotFoundError('qm unavailable')
    data = monitor.snapshot(cfg, runtime, tmp_path, Broken())
    assert data['vms'][0]['power'] == 'unknown' and data['vms'][0]['present'] is None
    assert data['vms'][2]['power'] == 'not configured'


def test_paused_guest_is_not_probed(lab, tmp_path):
    cfg, runtime = configuration(lab)
    probe = Probe()
    probe.vms[9402]['qmp_status'] = 'paused'
    monitor.snapshot(cfg, runtime, tmp_path, probe)
    assert 9402 not in probe.calls


def test_live_job_elapsed_uses_guest_clock_and_stale_jobs_stay_unconfirmed(lab, tmp_path):
    cfg, runtime = configuration(lab)
    root = tmp_path / 'runs'
    record = {'workflow': cfg, 'workflow_hash': 'hash', 'runtime': runtime, 'status': 'running',
              'stages': {'artifact-generate': {'status': 'running', 'attempts': [
                  {'vmid': 9403, 'unit': 'caf-orchestrator-demo', 'stopped': False,
                   'argv': ['/opt/generate', '--api-key', 'SECRET'], 'started_at': datetime.now(timezone.utc).isoformat()}]}}}
    ev.write_json(root / 'demo/workflow.json', record)
    data = monitor.snapshot(cfg, runtime, root, Probe())
    job = data['vms'][1]['jobs'][0]
    assert job['live_state'] == 'running' and job['elapsed_seconds'] == 120
    assert 'SECRET' not in json.dumps(data)
    probe = Probe()
    probe.fail_guest = True
    data = monitor.snapshot(cfg, runtime, root, probe)
    assert data['vms'][1]['jobs'][0]['live_state'] == 'unconfirmed'


def test_qm_status_parsing_and_presence_errors(lab, monkeypatch):
    cfg, runtime = configuration(lab)
    probe = monitor.ProxmoxProbe(runtime['backend'])
    replies = iter([subprocess.CompletedProcess([], 0, 'status: running\nqmpstatus: paused\nname: lab\n', ''),
                    subprocess.CompletedProcess([], 2, '', "Configuration file 'nodes/pve/qemu-server/9402.conf' does not exist"),
                    subprocess.CompletedProcess([], 2, '', 'Permission denied')])
    monkeypatch.setattr(monitor.subprocess, 'run', lambda *a, **k: next(replies))
    assert probe.vm(9402)['qmp_status'] == 'paused'
    assert probe.vm(9402)['present'] is False
    assert probe.vm(9402)['present'] is None


def test_guest_process_matching_includes_tool_children_and_excludes_probe(tmp_path, monkeypatch):
    root = tmp_path / 'proc'
    root.mkdir()
    (root / 'uptime').write_text('1000.0 0')
    app = tmp_path / 'caf'
    app.mkdir()
    (app / 'mcp_client.py').touch()
    def process(pid, ppid, argv, cwd):
        folder = root / str(pid)
        folder.mkdir()
        fields = ['0'] * 25
        fields[0], fields[1], fields[19] = 'S', str(ppid), '90000'
        (folder / 'stat').write_text(f'{pid} (python) ' + ' '.join(fields))
        (folder / 'cmdline').write_bytes('\0'.join(argv).encode() + b'\0')
        (folder / 'cwd').symlink_to(cwd)
    process(100, 1, ['/usr/bin/python', str(app / 'mcp_kali.py')], app)
    process(101, 100, ['/usr/bin/nmap', '10.77.0.1'], tmp_path)
    process(102, 1, ['/usr/bin/unrelated'], tmp_path)
    process(500, 1, ['/usr/bin/python', '-c', 'probe'], app)
    monkeypatch.setattr(guest_monitor.os, 'getpid', lambda: 500)
    monkeypatch.setattr(guest_monitor.os, 'sysconf', lambda key: 100)
    result = guest_monitor.inspect({'role': 'participant', 'root': str(app)}, root)
    assert result['application_present']
    assert [p['pid'] for p in result['processes']] == [100, 101]
    assert result['processes'][0]['elapsed_seconds'] == 100


def test_command_redaction():
    text = guest_monitor.redact(['curl', 'https://user:SECRET@host/path?token=HIDDEN', '--api-key=KEY', '-H', 'Authorization: Bearer TOKEN'])
    assert all(value not in text for value in ['SECRET', 'HIDDEN', 'KEY', 'TOKEN'])
    assert '[redacted]' in text


@pytest.mark.parametrize('monitoring', [{'core_vmid': 9403}, {'core_vmid': 1}, {'caf_service': '--anything'}, {'typo': True}])
def test_monitor_configuration_rejects_ambiguous_or_invalid_targets(lab, monitoring):
    cfg = yaml.safe_load(lab[0].read_text())
    cfg['monitoring'] = monitoring
    lab[0].write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):
        load(lab[0])
