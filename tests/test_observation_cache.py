from copy import deepcopy

import pytest

from cyber_agent_flow_orchestrator.observation_cache import CachedProbe, ObservationCache
from cyber_agent_flow_orchestrator import monitor, service
from cyber_agent_flow_eval import integration as ev
from test_monitor import Probe, configuration
from test_workflow import lab


def test_repeat_refresh_avoids_guest_calls_and_preserves_observation_time():
    clock = [0]
    cache = ObservationCache(clock=lambda: clock[0])
    probe = Probe()
    wrapped = CachedProbe(probe, cache)
    definition = dict(role='participant', root='/opt/caf', unit=None)
    initial = wrapped.guest(9403, definition, [])
    clock[0] = 59
    reused = wrapped.guest(9403, definition, [])
    assert probe.calls == [9403]
    assert reused['guest_cached'] and reused['guest_observed_at'] == initial['guest_observed_at']
    reused['processes'].clear()
    assert wrapped.guest(9403, definition, [])['processes']
    clock[0] = 60
    assert not wrapped.guest(9403, definition, [])['guest_cached']
    assert probe.calls == [9403, 9403]
    assert not CachedProbe(probe, cache, fresh=True).guest(9403, definition, [])['guest_cached']
    assert len(probe.calls) == 3


def test_active_jobs_expire_after_five_seconds_and_new_job_is_immediate():
    clock = [0]
    probe = Probe(); cache = ObservationCache(clock=lambda: clock[0])
    wrapped = CachedProbe(probe, cache)
    definition = dict(role='participant', root='/opt/caf', unit=None)
    wrapped.guest(9403, definition, [])
    assert not wrapped.guest(9403, definition, ['caf-worker-1'])['guest_cached']
    clock[0] = 4
    assert wrapped.guest(9403, definition, ['caf-worker-1'])['guest_cached']
    clock[0] = 5
    assert not wrapped.guest(9403, definition, ['caf-worker-1'])['guest_cached']
    assert not wrapped.guest(9403, definition, ['caf-worker-2'])['guest_cached']
    assert len(probe.calls) == 4


def test_offline_vm_invalidates_previous_guest_data():
    probe = Probe(); cache = ObservationCache(); wrapped = CachedProbe(probe, cache)
    definition = dict(role='participant', root='/opt/caf', unit=None)
    wrapped.guest(9403, definition, [])
    probe.vms[9403]['power'] = 'stopped'
    CachedProbe(probe, cache, fresh=True).vm(9403)
    probe.vms[9403]['power'] = 'running'
    assert not wrapped.guest(9403, definition, [])['guest_cached']


def test_failed_guest_probe_does_not_poison_cache():
    probe = Probe(); probe.fail_guest = True
    wrapped = CachedProbe(probe, ObservationCache())
    definition = dict(role='participant', root='/opt/caf', unit=None)
    with pytest.raises(ValueError): wrapped.guest(9403, definition, [])
    probe.fail_guest = False
    assert not wrapped.guest(9403, definition, [])['guest_cached']


def test_warm_snapshot_skips_expensive_guest_work_and_duplicate_run_reads(lab, tmp_path, monkeypatch):
    cfg, runtime = configuration(lab)
    probe = Probe(); cache = ObservationCache()
    def forbidden(*args, **kwargs):
        raise AssertionError('User dashboard already reads live run summaries')
    monkeypatch.setattr(service, 'list_runs', forbidden)
    first = monitor.snapshot(cfg, runtime, tmp_path, CachedProbe(probe, cache), include_runs=False)
    second = monitor.snapshot(cfg, runtime, tmp_path, CachedProbe(probe, cache), include_runs=False)
    assert len(probe.calls) == 2  # Only the first scan enters either running VM.
    assert second['vms'][0]['guest_cached']
    assert first['vms'][0]['observed_at'] == second['vms'][0]['observed_at']


def test_terminal_run_cache_invalidates_on_change_and_never_caches_active(lab, tmp_path, monkeypatch):
    root = tmp_path / 'runs'; run = root / 'demo'
    ev.write_json(run / 'workflow.json', {'status': 'running'})
    calls = []
    def status(path):
        calls.append(path)
        state = ev.read_json(path / 'workflow.json')['status']
        return dict(output=str(path), recorded_status=state, coordinator_active=False)
    monkeypatch.setattr(service, 'status', status)
    cache = {}
    assert service.list_runs(root, cache=cache)[0]['recorded_status'] == 'running'
    service.list_runs(root, cache=cache)
    assert len(calls) == 2 and not cache
    ev.write_json(run / 'workflow.json', {'status': 'completed'})
    service.list_runs(root, cache=cache)
    reused = service.list_runs(root, cache=cache)
    assert len(calls) == 3
    reused[0]['recorded_status'] = 'corrupt'
    assert service.list_runs(root, cache=cache)[0]['recorded_status'] == 'completed'
    ev.write_json(run / 'workflow.json', {'status': 'failed'})
    assert service.list_runs(root, cache=cache)[0]['recorded_status'] == 'failed'
    assert len(calls) == 4
