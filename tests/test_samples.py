import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import yaml

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import service, samples
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.sample_guest import SERVER
from cyber_agent_flow_orchestrator.tls import settings
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from test_pve_auth import pve, make_auth
from test_user_access import access, vm, Probe
from test_workflow import lab
from test_web import request
from https_fixture import secure_server, PASSWORD
from sample_fixture import install_backend


def setup_sample(pve, lab, tmp_path, monkeypatch, enabled=None):
    pve[0]['resources']['operator@pve'] = [vm(9403)]
    user = access(pve)
    workspace = Workspace(tmp_path / 'runs', user.username)
    workspace.save_roles(dict(scenarioforge=None, participant=9403, core=None), user)
    _, runtime, _, _ = load(lab[0])
    calls, agent, backend = install_backend(monkeypatch, tmp_path)
    return samples.SampleManager(runtime, tmp_path / 'runs', enabled), user, workspace, calls, agent, backend


@pytest.mark.parametrize('sample_id,trials', [('smoke', 1), ('tools-vs-helper', 6)])
def test_samples_use_real_scoring_private_results_and_cleanup(pve, lab, tmp_path, monkeypatch, sample_id, trials):
    manager, user, workspace, calls, _, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    try:
        reply = manager.submit(user, sample_id, 'a' * 32)
        manager.jobs[user.username].result(timeout=15)
        output = workspace.run_path(reply['run_id'])
        result = service.results(output)
        assert result['workflow']['recorded_status'] == 'completed'
        assert result['evaluation']['planned_trials'] == trials
        assert result['evaluation']['summary']['verified_successes'] == trials
        assert (output / 'evaluation/dataset.csv').is_file()
        assert output.stat().st_mode & 0o077 == 0
        study = ev.resolve(output / 'study.yaml')
        assert study['model'] == manager.runtime['model']
        assert study['engine'] == manager.runtime['engine']
        assert study['execution']['network_policy'] == {'allow': ['127.0.0.1/32'], 'disallow': []}
        assert study['backend']['before_trial'] == [] and 'app_vmid' not in study['backend']
        assert {row[0] for row in calls} == {9403}
        count = len(calls)
        assert manager.submit(user, sample_id, 'a' * 32)['run_id'] == reply['run_id']
        assert len(calls) == count  # Request retry is idempotent.
        if sample_id == 'tools-vs-helper':
            journal = ev.read_json(output / 'workflow.json')
            assert journal['sample_fixture']['stopped']
            assert [op for _, op, _ in calls].count('sample_start') == 1
            assert [op for _, op, _ in calls].count('sample_stop') == 1
            baseline, added = (c['catalog_snapshot']['tools'] for c in study['conditions'])
            assert added[:len(baseline)] == baseline
            assert added[-1]['name'] == 'http_flag_walk'
    finally:
        manager.close()


def test_invalid_disabled_samples_and_concurrent_launch(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, _, _ = setup_sample(pve, lab, tmp_path, monkeypatch, enabled=['smoke'])
    entered, release = threading.Event(), threading.Event()
    original = manager._run
    def blocked(*args):
        entered.set()
        assert release.wait(10)
        original(*args)
    monkeypatch.setattr(manager, '_run', blocked)
    try:
        with pytest.raises(samples.SampleRequestError): manager.submit(user, 'tools-vs-helper', 'a' * 32)
        with pytest.raises(samples.SampleRequestError): manager.submit(user, 'smoke', '../outside')
        assert not list(workspace.runs.iterdir())
        first = manager.submit(user, 'smoke', 'a' * 32)
        assert entered.wait(3)
        assert manager.submit(user, 'smoke', 'a' * 32)['run_id'] == first['run_id']
        with pytest.raises(samples.SampleBusy): manager.submit(user, 'smoke', 'b' * 32)
        assert not calls
    finally:
        release.set()
        manager.close()
        for future in manager.jobs.values(): future.result(timeout=10)


def test_live_progress_is_read_only_private_and_waits_for_cleanup(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, _, backend = setup_sample(pve, lab, tmp_path, monkeypatch)
    executing, release, cleaning, finish = (threading.Event() for _ in range(4))
    launch, cleanup = backend.launch, samples.stop_fixture
    def held_launch(self, directory, seconds):
        if not executing.is_set():
            ev.write_json(directory / 'transport.json', dict(phase='executing', updated_at=samples.now(),
                files_uploaded=7, files_total=7, bytes_uploaded=100, bytes_total=100,
                argv=['private-command'], path='/private/path', service_status={'SubState':'running','ExecMainPID':'123'}))
            executing.set()
            assert release.wait(15)
        return launch(self, directory, seconds)
    def held_cleanup(output, journal):
        cleaning.set()
        assert finish.wait(15)
        cleanup(output, journal)
    monkeypatch.setattr(backend, 'launch', held_launch)
    monkeypatch.setattr(samples, 'stop_fixture', held_cleanup)
    try:
        reply = manager.submit(user, 'tools-vs-helper', 'a' * 32)
        output = workspace.run_path(reply['run_id'])
        assert executing.wait(10)
        count = len(calls)
        live = service.status(output)['sample_progress']
        assert len(calls) == count
        assert live['active'] and live['finished_trials'] == 0 and live['percent'] == 0
        assert live['current_trial']['transport']['activity'] == 'Guest worker executing'
        assert live['current_trial']['transport']['service']['ExecMainPID'] == '123'
        assert 'private-command' not in json.dumps(live) and '/private/path' not in json.dumps(live)
        assert live['current_trial']['condition_id'] in ('baseline', 'added-helper')
        release.set()
        assert cleaning.wait(10)
        status = service.status(output)
        assert status['recorded_status'] == 'evaluating'
        assert status['sample_progress']['percent'] == 100
        assert status['sample_progress']['phase'] == 'cleanup'
        assert status['sample_progress']['active']
        finish.set()
        manager.jobs[user.username].result(timeout=10)
        status = service.status(output)
        assert status['recorded_status'] == 'completed'
        assert status['sample_progress']['verified_successes'] == 6
        assert not status['sample_progress']['active']
        assert status['sample_progress']['ended_at']
        assert any(event['kind'] == 'trial' for event in status['sample_progress']['events'])
    finally:
        release.set(); finish.set(); manager.close()
        for future in manager.jobs.values(): future.result(timeout=10)


def test_failed_fixture_start_still_attempts_cleanup(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, agent, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    original = agent.call
    def broken(self, vmid, op, **data):
        result = original(self, vmid, op, **data)
        if op == 'sample_start':
            raise ValueError('Fixture did not become ready')
        return result
    monkeypatch.setattr(agent, 'call', broken)
    try:
        reply = manager.submit(user, 'tools-vs-helper', 'a' * 32)
        manager.jobs[user.username].result(timeout=15)
        journal = ev.read_json(workspace.run_path(reply['run_id']) / 'workflow.json')
        assert journal['status'] == 'failed'
        assert 'did not become ready' in journal['error']
        assert journal['sample_fixture']['stopped']
        assert not any(op == 'trial' for _, op, _ in calls)
    finally:
        manager.close()


def test_revocation_before_queued_job_stops_all_guest_operations(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, _, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    original = manager._run
    def revoke(*args):
        pve[0]['resources']['operator@pve'] = []
        original(*args)
    monkeypatch.setattr(manager, '_run', revoke)
    try:
        reply = manager.submit(user, 'smoke', 'a' * 32)
        manager.jobs[user.username].result(timeout=15)
        assert not calls
        assert service.status(workspace.run_path(reply['run_id']))['recorded_status'] == 'failed'
    finally:
        manager.close()


def test_https_launch_csrf_owner_isolation_results_and_csv(pve, lab, tmp_path, monkeypatch):
    pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403), vm(9404)]
    from scenario_fixture import install_transport
    install_transport(monkeypatch, lab[2], lab[3])
    auth = make_auth(pve)
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        with secure_server(dash, tmp_path / 'web', auth=auth) as server:
            def login(name):
                code, headers, _ = request(server, '/api/login', {'username': name, 'password': PASSWORD})
                assert code == 200
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                session = json.loads(request(server, '/api/session', cookie=cookie)[2])
                return cookie, {'X-CSRF-Token': session['csrf']}
            cookie, headers = login('operator@pve')
            bob, bh = login('bob@pve')
            assert request(server, '/api/roles', dict(scenarioforge=9402, participant=9403, core=9404), cookie=cookie, headers=headers)[0] == 200
            body = {'sample_id': 'smoke', 'request_id': 'a' * 32}
            assert request(server, '/api/samples/run', body)[0] == 401
            assert request(server, '/api/samples/run', body, cookie=cookie)[0] == 403
            assert request(server, '/api/samples/run', dict(body, vmid=999), cookie=cookie, headers=headers)[0] == 400
            assert request(server, '/api/samples/run', body, cookie=bob, headers=bh)[0] == 400
            code, _, reply = request(server, '/api/samples/run', body, cookie=cookie, headers=headers)
            assert code == 202
            run_id = json.loads(reply)['run_id']
            dash.samples.jobs['operator@pve'].result(timeout=15)
            assert request(server, f'/api/runs/{run_id}/results', cookie=cookie)[0] == 200
            assert request(server, f'/api/runs/{run_id}/results', cookie=bob)[0] == 404
            code, response_headers, csv = request(server, f'/api/runs/{run_id}/dataset.csv', cookie=cookie)
            assert code == 200 and 'attachment' in response_headers['Content-Disposition']
            assert b'condition_id' in csv and b'baseline' in csv
            assert request(server, f'/api/runs/{run_id}/dataset.csv', cookie=bob)[0] == 404
            dash.samples.enabled = ()
            assert request(server, '/api/samples/run', dict(body, request_id='b' * 32), cookie=cookie, headers=headers)[0] == 400
    finally:
        dash.close()


def test_web_config_can_remove_samples(tmp_path):
    path = tmp_path / 'web.yaml'
    config = dict(version=1, public_url='https://localhost:8443', certificate='cert.pem', private_key='key.pem',
                  users_file='users.json', samples=[])
    path.write_text(yaml.safe_dump(config))
    assert settings(path)['samples'] == []
    config['samples'] = ['not-a-bundled-sample']
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='samples'): settings(path)


def test_real_demo_server_and_bundled_helper_find_flags(tmp_path):
    ready = tmp_path / 'ready.json'
    process = subprocess.Popen([sys.executable, '-c', SERVER, str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        url = 'http://127.0.0.1:' + str(json.loads(ready.read_text())['port']) + '/'
        assert b'Briefing' in urlopen(url, timeout=2).read()
        with pytest.raises(HTTPError): urlopen(url + 'etc/passwd', timeout=2)
        catalog = json.loads((Path(samples.__file__).parent / 'sample_data/with-http-helper.json').read_text())
        helper = catalog['tools'][-1]
        result = subprocess.run([sys.executable, *helper['base_args'], url], check=True, capture_output=True, text=True, timeout=10)
        assert json.loads(result.stdout)['flags'] == ['FLAG{demo_archive}', 'FLAG{demo_entry}']
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_guest_fixture_bounds_and_delayed_start_cancellation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cyber_agent_flow_orchestrator import sample_guest as guest
    paths = {'/var/lib/caf-eval-samples': tmp_path / 'controls', '/run': tmp_path / 'runtime'}
    monkeypatch.setattr(guest, 'Path', lambda path: paths.get(str(path), Path(path)))
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[0] == 'systemd-run':
            ready = Path(args[-1])
            ready.parent.mkdir(parents=True)
            ready.write_text('{"port":18480}')
        return SimpleNamespace(stdout='inactive\n', stderr='', returncode=0)
    monkeypatch.setattr(guest.subprocess, 'run', command)
    token = 'b' * 32
    assert guest.dispatch(dict(op='sample_start', token=token))['url'] == 'http://127.0.0.1:18480/'
    assert '--property=RuntimeMaxSec=30min' in calls[0]
    assert '--property=DynamicUser=yes' in calls[0]
    assert guest.dispatch(dict(op='sample_stop', token=token))['stopped']
    before = len(calls)
    with pytest.raises(ValueError, match='cancelled'):
        guest.dispatch(dict(op='sample_start', token=token))
    assert len(calls) == before
    with pytest.raises(ValueError, match='token'):
        guest.dispatch(dict(op='sample_stop', token='../bad'))


def test_shutdown_stops_between_trials_and_cleans_fixture(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, _, backend = setup_sample(pve, lab, tmp_path, monkeypatch)
    original = backend.launch
    def shutdown(self, *args):
        result = original(self, *args)
        manager.close()
        return result
    monkeypatch.setattr(backend, 'launch', shutdown)
    reply = manager.submit(user, 'tools-vs-helper', 'a' * 32)
    manager.jobs[user.username].result(timeout=15)
    journal = ev.read_json(workspace.run_path(reply['run_id']) / 'workflow.json')
    assert journal['status'] == 'interrupted'
    assert journal['sample_fixture']['stopped']
    assert [op for _, op, _ in calls].count('trial') == 1


def test_user_recovery_cleans_fixture_when_preparation_was_interrupted(pve, lab, tmp_path, monkeypatch):
    from cyber_agent_flow_orchestrator import user_execution
    manager, user, workspace, calls, agent, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    original = agent.call
    def broken(self, vmid, op, **data):
        if op in ('sample_start', 'sample_stop'):
            raise ValueError('Simulated lost guest response')
        return original(self, vmid, op, **data)
    monkeypatch.setattr(agent, 'call', broken)
    try:
        reply = manager.submit(user, 'tools-vs-helper', 'a' * 32)
        manager.jobs[user.username].result(timeout=15)
        output = workspace.run_path(reply['run_id'])
        assert not ev.read_json(output / 'workflow.json')['sample_fixture']['stopped']
        monkeypatch.setattr(agent, 'call', original)
        user_execution.recover(manager.root, reply['run_id'], user)
        assert ev.read_json(output / 'workflow.json')['sample_fixture']['stopped']
        assert [op for _, op, _ in calls].count('sample_stop') == 1
    finally:
        manager.close()


def test_legacy_transport_and_interrupted_trial_progress(tmp_path):
    from cyber_agent_flow_orchestrator.sample_progress import build
    output = tmp_path / 'run'
    directory = output / 'evaluation/trials/trial-000001/attempt-0001'
    ev.write_json(directory / 'transport.json', dict(stopped=False))
    stamp = samples.now()
    journal = dict(sample_id='smoke', created_at=stamp, updated_at=stamp)
    report = dict(planned_trials=1, attempts=[dict(trial_id='trial-000001', condition_id='no-tools',
        attempt=1, status='running', started_at=stamp, attempt_path='trials/trial-000001/attempt-0001')])
    active = build(output, journal, report, 'evaluating', True)
    assert 'detailed stage unavailable' in active['current_trial']['transport']['activity']
    stopped = build(output, journal, report, 'interrupted', False)
    assert not stopped['active'] and stopped['current_trial'] is None
    assert stopped['trials'][0]['status'] == 'unconfirmed'
    assert stopped['trials'][0]['elapsed_seconds'] is None
    assert stopped['percent'] == 0 and stopped['elapsed_seconds'] == 0
    report['attempts'][0].update(status='error', errors=['Connection failed; api_key=private-test-key'])
    failed = build(output, journal, report, 'completed_with_errors', False)
    assert failed['trials'][0]['errors'] == ['Connection failed; api_key=[redacted]']


def test_progress_journal_failure_still_cleans_up_fixture(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, _, _ = setup_sample(pve, lab, tmp_path, monkeypatch)
    original, failures = samples.ev.write_json, []
    def write(path, value):
        if Path(path).name == 'workflow.json' and value.get('phase') == 'cleanup' and not failures:
            failures.append(str(path))
            raise OSError('Unable to write cleanup progress')
        return original(path, value)
    monkeypatch.setattr(samples.ev, 'write_json', write)
    try:
        reply = manager.submit(user, 'tools-vs-helper', 'a' * 32)
        manager.jobs[user.username].result(timeout=15)
        assert failures and any(op == 'sample_stop' for _, op, _ in calls)
        assert ev.read_json(workspace.run_path(reply['run_id']) / 'workflow.json')['sample_fixture']['stopped']
    finally:
        manager.close()


def test_saved_experiments_stop_and_rerun_preserve_results(pve, lab, tmp_path, monkeypatch):
    manager, user, workspace, calls, _, backend = setup_sample(pve, lab, tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = backend.launch
    def launch(self, directory, seconds):
        entered.set()
        assert release.wait(15)
        return original(self, directory, seconds)
    monkeypatch.setattr(backend, 'launch', launch)
    other_manager = samples.SampleManager(manager.runtime, manager.root)
    try:
        with ev.lease(workspace.path / '.sample-submit.lock'):
            with pytest.raises(samples.SampleBusy): manager.create(user, 'tools-vs-helper', 'a' * 32)
        draft = manager.create(user, 'tools-vs-helper', 'a' * 32)
        assert service.status(workspace.run_path(draft['run_id']))['recorded_status'] == 'ready'
        assert not calls
        reply = manager.run_saved(user, draft['run_id'], 'b' * 32)
        assert reply['run_id'] == draft['run_id'] and entered.wait(10)
        assert manager.create(user, 'tools-vs-helper', 'a' * 32)['status'] != 'ready'
        with pytest.raises(samples.SampleBusy): other_manager.submit(user, 'smoke', 'c' * 32)
        with pytest.raises(samples.SampleRequestError): manager.stop(access(pve, 'another@pve'), reply['run_id'])
        pve[0]['resources']['operator@pve'] = []
        from cyber_agent_flow_orchestrator.access import AccessDenied
        with pytest.raises(AccessDenied): manager.stop(user, reply['run_id'])
        pve[0]['resources']['operator@pve'] = [vm(9403)]
        manager.stop(user, reply['run_id'])
        assert service.status(workspace.run_path(reply['run_id']))['recorded_status'] == 'stopping'
        release.set()
        manager.jobs[user.username].result(timeout=10)
        result = service.results(workspace.run_path(reply['run_id']))
        assert result['workflow']['recorded_status'] == 'cancelled'
        assert len(result['evaluation']['attempts']) == 1
        assert any(op == 'sample_stop' for _, op, _ in calls)
        count = len(calls)
        assert manager.run_saved(user, reply['run_id'], 'b' * 32)['run_id'] == reply['run_id']
        assert len(calls) == count  # A retried Run request must not start another run.
        again = manager.run_saved(user, reply['run_id'], 'd' * 32)
        assert again['run_id'] != reply['run_id']
        manager.jobs[user.username].result(timeout=10)
        assert service.status(workspace.run_path(reply['run_id']))['recorded_status'] == 'cancelled'
        assert service.status(workspace.run_path(again['run_id']))['recorded_status'] == 'completed'
    finally:
        release.set(); manager.close(); other_manager.close()
        for future in manager.jobs.values(): future.result(timeout=10)


def test_experiment_http_create_run_validation_and_owner_isolation(pve, lab, tmp_path, monkeypatch):
    from scenario_fixture import install_transport
    install_transport(monkeypatch, lab[2], lab[3])
    pve[0]['resources']['operator@pve'] = [vm(9402), vm(9403), vm(9404)]
    dash = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b, a: Probe(b, a, []))
    try:
        with secure_server(dash, tmp_path / 'web', auth=make_auth(pve)) as server:
            def login(name):
                _, headers, _ = request(server, '/api/login', {'username':name, 'password':PASSWORD})
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
                return cookie, {'X-CSRF-Token':csrf}
            cookie, headers = login('operator@pve')
            bob, bob_headers = login('bob@pve')
            data = dict(sample_id='smoke', request_id='a' * 32)
            assert request(server, '/api/experiments/create', data)[0] == 401
            assert request(server, '/api/experiments/create', data, cookie=cookie)[0] == 403
            assert request(server, '/api/experiments/create', dict(data, vmid=999), cookie=cookie, headers=headers)[0] == 400
            assert request(server, '/api/experiments/create', data, cookie=cookie, headers=headers)[0] == 400
            request(server, '/api/roles', dict(scenarioforge=9402,participant=9403,core=9404), cookie=cookie,headers=headers)
            status, _, body = request(server, '/api/experiments/create', data, cookie=cookie, headers=headers)
            assert status == 202
            run_id = json.loads(body)['run_id']
            run = dict(run_id=run_id, request_id='b' * 32)
            assert request(server, '/api/experiments/run', run, cookie=bob, headers=bob_headers)[0] == 400
            assert request(server, '/api/experiments/stop', {'run_id':run_id}, cookie=bob, headers=bob_headers)[0] == 400
            assert request(server, '/api/experiments/stop', {'run_id':run_id}, cookie=cookie)[0] == 403
            request(server, '/api/roles', dict(scenarioforge=9402, participant=9403, core=9404), cookie=cookie, headers=headers)
            assert request(server, '/api/experiments/run', run, cookie=cookie, headers=headers)[0] == 202
            dash.samples.jobs['operator@pve'].result(timeout=10)
            assert request(server, f'/api/runs/{run_id}/results', cookie=cookie)[0] == 200
            assert request(server, f'/api/runs/{run_id}/results', cookie=bob)[0] == 404
    finally:
        dash.close()
