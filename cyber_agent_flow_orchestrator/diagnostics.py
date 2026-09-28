"""Bounded, private maintenance traces; never record RPC scripts or file payloads."""
from collections import deque
from datetime import datetime, timezone
import base64
import json
import logging
import re
import time

from cyber_agent_flow_eval import integration as ev
from .guest_monitor import redact


def clean(value):
    text = str(value)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)[^\r\n]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)(["\']?(?:password|passwd|api[-_]?key|token|secret|ticket|cookie)["\']?\s*[:=]\s*)(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;&}]+)', r'\1[redacted]', text)
    text = re.sub(r'PVE(?:AuthCookie|APIToken)?=[^\s;]+|PVE:[^\s"\']+', '[redacted]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)[:1200]


class Trace:
    def __init__(self, directory):
        self.path = directory / 'console.json'
        self.events = deque(maxlen=100)
        self.sequence = 0
        self.transfer = None
        self.last_write = 0
        self.write_failed = False

    def emit(self, kind, message, *, force=False):
        self.sequence += 1
        self.events.append(dict(sequence=self.sequence, at=datetime.now(timezone.utc).isoformat(),
                                kind=kind, message=clean(message)))
        self.flush(force=force)

    def flush(self, *, force=False):
        if force or time.monotonic() - self.last_write >= 1:
            try:
                ev.write_json(self.path, dict(events=list(self.events), transfer=self.transfer,
                                             total_events=self.sequence))
                self.path.chmod(0o600)
                self.write_failed = False
            except OSError:
                # Optional diagnostics must not interrupt a guest activation or
                # turn an acknowledged write into a transport failure.
                if not self.write_failed:
                    logging.getLogger(__name__).warning('Unable to persist maintenance troubleshooting trace')
                self.write_failed = True
            self.last_write = time.monotonic()

    def instrument(self, agent):
        call, qm, put = agent.call, agent.qm, agent.put

        def traced_qm(args, **options):
            # qm guest exec contains an entire Python script and an RPC JSON
            # argument. Only show the command prefix; the operation is logged below.
            visible = list(map(str, args))
            if '--' in visible:
                visible = visible[:visible.index('--') + 1] + ['[guest helper and RPC payload omitted]']
            self.emit('command', redact(['qm', *visible]) + ' (includes authorization check)', force=True)
            start = time.monotonic()
            try:
                result = qm(args, **options)  # stdin payload is deliberately never logged.
                safe = {k: result[k] for k in ('pid', 'exited', 'exitcode', 'out-truncated', 'err-truncated') if k in result}
                self.emit('response', f'qm completed in {time.monotonic()-start:.2f}s: {json.dumps(safe)}')
                return result
            except Exception as exc:
                # An exception from qm can embed its whole argv, including payload.
                self.emit('error', f'qm failed after {time.monotonic()-start:.2f}s ({type(exc).__name__}); see operation outcome', force=True)
                raise

        def traced_call(vmid, op, **data):
            fields = {k: data[k] for k in ('path', 'offset', 'limit', 'role', 'root', 'revision', 'expected_revision', 'service') if k in data}
            self.emit('operation', f'VM {vmid}: {op} {json.dumps(fields)}', force=True)
            start = time.monotonic()
            try:
                result = call(vmid, op, **data)
                safe = {k: result[k] for k in ('revision', 'branch', 'modified', 'missing_controls', 'path', 'size', 'sha256', 'written') if k in result}
                self.emit('response', f'VM {vmid}: {op} completed in {time.monotonic()-start:.2f}s: {json.dumps(safe)}')
                if op == 'write' and self.transfer is not None:
                    self.transfer['sent_bytes'] = data['offset'] + len(base64.b64decode(data['content']))
                    self.transfer['percent'] = round(100 * self.transfer['sent_bytes'] / max(1, self.transfer['total_bytes']), 1)
                    self.transfer['elapsed_seconds'] = round(time.monotonic() - self.transfer_start, 2)
                    self.transfer['bytes_per_second'] = round(self.transfer['sent_bytes'] / max(.001, self.transfer['elapsed_seconds']), 1)
                    self.transfer['updated_at'] = datetime.now(timezone.utc).isoformat()
                    self.emit('transfer', f"Acknowledged {self.transfer['sent_bytes']}/{self.transfer['total_bytes']} bytes ({self.transfer['percent']}%)")
                self.flush(force=True)
                return result
            except Exception as exc:
                self.emit('error', f'VM {vmid}: {op} failed after {time.monotonic()-start:.2f}s ({type(exc).__name__})', force=True)
                raise

        def traced_put(vmid, path, content):
            self.transfer_start = time.monotonic()
            self.transfer = dict(total_bytes=len(content), sent_bytes=0, percent=0, verified=False,
                                 elapsed_seconds=0, bytes_per_second=0, updated_at=datetime.now(timezone.utc).isoformat())
            self.emit('transfer', f'Uploading {len(content)} bytes to VM {vmid}: {path}; waiting for first acknowledgement', force=True)
            try:
                result = put(vmid, path, content)
                self.transfer.update(verified=True, percent=100, sent_bytes=len(content))
                self.emit('transfer', 'Upload complete; guest size and SHA-256 verified', force=True)
                return result
            except Exception:
                self.emit('error', 'Upload failed; byte progress is acknowledged data, not a verified complete file', force=True)
                raise

        agent.qm, agent.call, agent.put = traced_qm, traced_call, traced_put
        return agent
