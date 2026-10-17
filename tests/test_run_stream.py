from copy import deepcopy
import json
import time
from urllib.request import Request, urlopen

import pytest

from cyber_agent_flow_orchestrator.run_stream import RunStreams, StreamUnavailable, changes, frame, signature, state_frame
from cyber_agent_flow_orchestrator.proxy import upstream_timeout
from cyber_agent_flow_orchestrator.workspaces import Workspace
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve, make_auth
from test_web import request
from test_workflow import lab


def apply(value, operations):
    value = deepcopy(value)
    for row in operations:
        path = row['path']
        parent = value
        for key in path[:-1]: parent = parent[key]
        if row['op'] == 'append':
            target = parent[path[-1]] if path else value
            target.extend(deepcopy(row['value']))
        elif row['op'] == 'remove': del parent[path[-1]]
        elif not path: value = deepcopy(row['value'])
        else: parent[path[-1]] = deepcopy(row['value'])
    return value


def test_deltas_round_trip_streamed_text_appends_rollover_and_removed_fields():
    previous = {'sample_progress': {'trials': [{'transcript': {'events': [{'text': 'partial'}]}}]}, 'remove': 1, 'bool': True}
    value = {'sample_progress': {'trials': [{'transcript': {'events': [{'text': 'partial response'}]}}]}, 'bool': 1}
    assert apply(previous, changes(previous, value)) == value
    next_value = deepcopy(value)
    next_value['sample_progress']['trials'][0]['transcript']['events'].append({'text': 'new tool result'})
    delta = changes(value, next_value)
    assert delta[0]['op'] == 'append'
    assert apply(value, delta) == next_value
    rollover = deepcopy(next_value)
    rollover['sample_progress']['trials'][0]['transcript']['events'] = [{'text': 'new tool result'}]
    assert apply(next_value, changes(next_value, rollover)) == rollover
    for old, new in (([], {}), (1, 2), (None, True), ({}, {'__proto__': {'safe': True}})):
        assert apply(old, changes(old, new)) == new
    assert frame('response', {'text': 'a\nb'}).count(b'\n\n') == 1
    payload = state_frame(None, value, None, 3)
    assert b'event: snapshot\n' in payload and b'id: 3\n' in payload
    assert b'event: update\n' in state_frame({'long': 'x'*1000, 'v': 1}, {'long': 'x'*1000, 'v': 2}, 1, 2)


def test_shared_feed_coalesces_updates_and_closes_without_readers():
    value = {'recorded_status': 'running', 'text': 'one'}
    calls, version = [], [1]
    hub = RunStreams(interval=.01, refresh=60, max_clients=2, max_feeds=1)
    def read(): calls.append(1); return deepcopy(value)
    with hub.subscribe(('alice', 'run'), read, lambda: version[0]) as one:
        revision, received = one.wait(0, 1)
        assert received['text'] == 'one'
        with hub.subscribe(('alice', 'run'), read, lambda: version[0]) as two:
            assert two is one and len(calls) == 1
            with pytest.raises(StreamUnavailable):
                with hub.subscribe(('bob', 'run'), read, lambda: 1): pass
            value['text'] = 'latest'; version[0] += 1
            changed, received = two.wait(revision, 1)
            assert changed > revision and received['text'] == 'latest'
            assert one.wait(revision, .1) == (changed, received)
            assert len(calls) == 2
        assert not one.stop.is_set()
    one.thread.join(1)
    assert not one.thread.is_alive() and hub.clients == 0 and not hub.feeds
    hub.close()


def test_feed_errors_are_private_and_periodic_refresh_catches_lock_release():
    hub = RunStreams(interval=.01, refresh=.02)
    calls = []
    def read():
        calls.append(1)
        if len(calls) > 1: return {'recorded_status': 'interrupted'}
        return {'recorded_status': 'running'}
    with hub.subscribe('x', read, lambda: 1) as feed:
        revision, _ = feed.wait(0, 1)
        _, value = feed.wait(revision, 1)
        assert value['recorded_status'] == 'interrupted'
    def bad(): raise ValueError('PRIVATE HOST PATH AND KEY')
    with hub.subscribe('bad', bad, lambda: 1) as feed:
        with pytest.raises(StreamUnavailable, match='Live updates unavailable') as error: feed.wait(0, 1)
        assert 'PRIVATE' not in str(error.value)
    hub.close()


def test_signature_tracks_telemetry_not_artifacts_or_symlink_trees(tmp_path):
    run = tmp_path/'run'; attempt=run/'evaluation/trials/trial-1/attempt-0001'
    attempt.mkdir(parents=True)
    (run/'workflow.json').write_text('{}')
    first = signature(run)
    (attempt/'live-events.jsonl').write_text('first')
    assert signature(run) != first
    artifacts = attempt/'artifacts'; artifacts.mkdir(); (artifacts/'large.bin').write_bytes(b'x')
    before = signature(run); (artifacts/'large.bin').write_bytes(b'changed')
    assert signature(run) == before
    outside = tmp_path/'outside'; outside.mkdir(); (outside/'secret.json').write_text('secret')
    (attempt/'progress-checks').symlink_to(outside, target_is_directory=True)
    assert not any('secret' in row[0] for row in signature(run))
    timeout = upstream_timeout('/api/runs/example/stream')
    assert timeout.total is None and timeout.sock_read == 30


class StreamDashboard:
    scoped = True
    def __init__(self):
        self.hub = RunStreams(interval=.01, refresh=60)
        self.values = {owner: {'recorded_status': 'running', 'owner': owner, 'text': 'first', 'padding': 'x'*1000} for owner in ['operator@pve', 'bob@pve']}
        self.versions = dict.fromkeys(self.values, 1)
    def run_stream(self, access, run_id):
        from cyber_agent_flow_orchestrator.access import AccessDenied
        access.current()
        if run_id == 'missing': raise FileNotFoundError()
        if run_id != 'demo': raise AccessDenied()
        owner = access.username
        return self.hub.subscribe((owner, run_id), lambda: deepcopy(self.values[owner]), lambda: self.versions[owner])


def login_pve(server, username='operator@pve'):
    code, headers, body = request(server, '/api/login', {'username': username, 'password': PASSWORD})
    assert code == 200, body
    return headers['Set-Cookie'].split(';', 1)[0]


def connect(server, cookie):
    return urlopen(Request(server['origin']+'/api/runs/demo/stream', headers={'Cookie': cookie}), context=server['ssl'], timeout=5)


def read_event(response):
    record = {}
    while True:
        line = response.readline()
        assert line, record
        if line == b'\n':
            if 'event' in record: return record['event'], json.loads(record['data'])
            record = {}; continue
        key, _, value = line.decode().partition(':')
        record[key] = value.strip()


def test_https_stream_is_incremental_owner_scoped_and_finishes(pve, tmp_path):
    dashboard = StreamDashboard()
    try:
        with secure_server(dashboard, tmp_path/'web', auth=make_auth(pve)) as server:
            assert request(server, '/api/runs/demo/stream')[0] == 401
            alice, bob = login_pve(server), login_pve(server, 'bob@pve')
            assert request(server, '/api/runs/missing/stream', cookie=alice)[0] == 404
            assert request(server, '/api/runs/other/stream', cookie=alice)[0] == 403
            with connect(server, alice) as first, connect(server, bob) as second:
                assert first.headers['Content-Type'].startswith('text/event-stream')
                assert first.headers['X-Accel-Buffering'] == 'no'
                kind, payload = read_event(first)
                assert kind == 'snapshot' and payload['value']['owner'] == 'operator@pve'
                assert read_event(second)[1]['value']['owner'] == 'bob@pve'
                dashboard.values['operator@pve']['text'] = 'new streamed text'
                dashboard.versions['operator@pve'] += 1
                kind, payload = read_event(first)
                assert kind == 'update' and 'new streamed text' in json.dumps(payload)
                dashboard.values['operator@pve']['recorded_status'] = 'completed'
                dashboard.versions['operator@pve'] += 1
                assert read_event(first)[0] == 'update'
                assert read_event(first)[0] == 'complete'
    finally:
        dashboard.hub.close()


def test_open_stream_revokes_when_session_logs_out(pve, tmp_path):
    dashboard = StreamDashboard()
    try:
        with secure_server(dashboard, tmp_path/'web', auth=make_auth(pve)) as server:
            cookie = login_pve(server)
            with connect(server, cookie) as response:
                assert read_event(response)[0] == 'snapshot'
                csrf = json.loads(request(server, '/api/session', cookie=cookie)[2])['csrf']
                assert request(server, '/api/logout', {}, cookie=cookie, headers={'X-CSRF-Token': csrf})[0] == 200
                kind, payload = read_event(response)
                assert kind == 'access-error' and payload['status'] == 401
    finally:
        dashboard.hub.close()


def test_real_dashboard_stream_reads_owned_host_files_without_guest_calls(pve, lab, tmp_path, monkeypatch):
    from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
    from cyber_agent_flow_orchestrator import service
    from test_user_access import access, Probe
    owner, other = access(pve), access(pve, 'bob@pve')
    dashboard = UserDashboard(lab[0], tmp_path/'runs', 2, lambda b,a: Probe(b,a,[]))
    try:
        path = Workspace(tmp_path/'runs', owner.username).run_path('demo');path.mkdir();(path/'workflow.json').write_text('{}')
        reads=[]
        monkeypatch.setattr(service, 'status', lambda root, **kw: reads.append(root) or {'recorded_status': 'running'})
        with dashboard.run_stream(owner, 'demo') as feed:
            assert feed.wait(0, 1)[1]['recorded_status'] == 'running'
            assert reads == [path]
        with pytest.raises(FileNotFoundError): dashboard.run_stream(other, 'demo')
        (path/'evaluation').symlink_to(tmp_path, target_is_directory=True)
        from cyber_agent_flow_orchestrator.access import AccessDenied
        with pytest.raises(AccessDenied): dashboard.run_stream(owner, 'demo')
    finally: dashboard.close()


def test_open_stream_revalidates_provider_revocation(pve, tmp_path, monkeypatch):
    monkeypatch.setattr('cyber_agent_flow_orchestrator.run_stream.AUTH_REFRESH_SECONDS', .02)
    monkeypatch.setattr('cyber_agent_flow_orchestrator.run_stream.WAIT_SECONDS', .02)
    dashboard = StreamDashboard()
    try:
        with secure_server(dashboard, tmp_path/'web', auth=make_auth(pve)) as server:
            cookie = login_pve(server)
            with connect(server, cookie) as response:
                assert read_event(response)[0] == 'snapshot'
                pve[0]['groups'] = 'other'
                kind, payload = read_event(response)
                assert kind == 'access-error' and payload['status'] == 401
    finally: dashboard.hub.close()
