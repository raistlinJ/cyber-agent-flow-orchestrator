import base64
import hashlib
import json

import pytest

from cyber_agent_flow_eval.proxmox import GuestAgent, CHUNK
from cyber_agent_flow_orchestrator.diagnostics import Trace, clean


@pytest.mark.parametrize('mismatch', [False, True])
def test_transfer_trace_tracks_acknowledgements_and_verification_without_payloads(tmp_path, mismatch):
    content = b'PRIVATE_FILE_CONTENT' * (CHUNK // 19 + 2)
    trace = Trace(tmp_path)
    observed = []

    class Agent(GuestAgent):
        def qm(self, args):
            if args[1] == 'exec':
                data = json.loads(args[-1])
                observed.append((data['op'], trace.transfer['sent_bytes']))
                if data['op'] == 'write':
                    self.output = {'written': len(base64.b64decode(data['content']))}
                else:
                    self.output = {'size': len(content), 'sha256': 'wrong' if mismatch else hashlib.sha256(content).hexdigest()}
                return {'pid': 123}
            return {'exited': True, 'exitcode': 0, 'out-data': json.dumps(self.output)}

    agent = trace.instrument(Agent(dict(command_timeout=5, poll_seconds=.01,
                                       guest_python='/usr/bin/python3', max_transfer_bytes=100000)))
    agent.script = 'PRIVATE_HELPER_SCRIPT'
    if mismatch:
        with pytest.raises(ValueError, match='checksum mismatch'):
            agent.put(101, '/tmp/source.bundle', content)
    else:
        agent.put(101, '/tmp/source.bundle', content)
    record = json.loads(trace.path.read_text())
    assert observed == [('write', 0), ('write', CHUNK), ('stat', len(content))]
    assert record['transfer']['sent_bytes'] == len(content)
    assert record['transfer']['verified'] is not mismatch
    assert record['transfer']['percent'] == 100
    text = trace.path.read_text()
    assert 'qm guest exec' in text and 'exitcode' in text and 'Acknowledged' in text
    assert 'PRIVATE_HELPER_SCRIPT' not in text and 'PRIVATE_FILE_CONTENT' not in text
    assert base64.b64encode(content[:100]).decode() not in text


def test_command_failures_are_recorded_without_argv_payloads(tmp_path):
    trace = Trace(tmp_path)
    class Agent(GuestAgent):
        def qm(self, args):
            raise ValueError('Secret full argv and payload PRIVATE_BUNDLE')
    agent = trace.instrument(Agent(dict(command_timeout=1, guest_python='python3')))
    with pytest.raises(ValueError):
        agent.call(101, 'write', path='/tmp/source', offset=0, content='PRIVATE_BUNDLE')
    text = trace.path.read_text()
    assert 'qm failed' in text and 'write failed' in text
    assert 'PRIVATE_BUNDLE' not in text


def test_trace_is_bounded_and_masks_common_credentials(tmp_path):
    value = clean('https://alice:PRIVATE@host/path?token=QUERYSECRET\n'
                  'Authorization: Bearer BEARERSECRET\n'
                  '{"password": "PASSWORDSECRET", "api_key": "KEYSECRET"}\n'
                  'PVE:PRIVATECOOKIE\n\x1b[31merror')
    for secret in ('PRIVATE', 'QUERYSECRET', 'BEARERSECRET', 'PASSWORDSECRET', 'KEYSECRET', 'PRIVATECOOKIE', '\x1b'):
        assert secret not in value
    trace = Trace(tmp_path)
    for number in range(120):
        trace.emit('response', str(number) + 'x' * 4000)
    trace.flush(force=True)
    record = json.loads(trace.path.read_text())
    assert len(record['events']) == 100 and record['total_events'] == 120
    assert record['events'][0]['sequence'] == 21
    assert max(len(row['message']) for row in record['events']) <= 1200
    assert trace.path.stat().st_mode & 0o777 == 0o600


def test_optional_trace_write_failure_does_not_interrupt_operation(tmp_path, monkeypatch):
    trace = Trace(tmp_path)
    def fail(*args):
        raise OSError('Disk unavailable')
    monkeypatch.setattr('cyber_agent_flow_orchestrator.diagnostics.ev.write_json', fail)
    trace.emit('status', 'Guest operation continues', force=True)
    assert trace.write_failed
