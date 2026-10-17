"""Bounded, owner-scoped tool activity from host mirrors. No network probes."""

from datetime import datetime, timedelta, timezone
import json
import math
import re
from cyber_agent_flow_eval.network_targets import extract, overlaps
from .diagnostics import clean
from .run_artifacts import open_saved

TAIL_BYTES = 128 * 1024
HEARTBEAT_SECONDS = 45
RECENT_SECONDS = 12
CONTEXT_SECONDS = 120


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def read_transport(root, base):
    try:
        with open_saved(root, base + "transport.json") as stream:
            raw = stream.read(256 * 1024 + 1)
        value = json.loads(raw) if len(raw) <= 256 * 1024 else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_events(root, base):
    for name in ("guest-output/events.jsonl", "live-events.jsonl", "events.jsonl"):
        path = base + name
        try:
            with open_saved(root, path) as stream:
                stream.seek(0, 2)
                size = stream.tell()
                start = max(0, size - TAIL_BYTES)
                stream.seek(start)
                raw = stream.read(TAIL_BYTES)
            if start:
                split = raw.find(b"\n")
                if split < 0:
                    return []
                start += split + 1
                raw = raw[split + 1 :]
            records = []
            offset = start
            for line in raw.splitlines(keepends=True):
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        records.append(
                            (event, dict(file=path, offset=offset, limit=len(line)))
                        )
                except (ValueError, UnicodeDecodeError):
                    pass
                offset += len(line)
            return records[-512:]
        except (OSError, ValueError):
            continue
    return []


def observe(root, base, row, monitor, nodes, *, active=False, now=None):
    now = now or datetime.now(timezone.utc)
    transport = read_transport(root, base)
    anchor = timestamp(transport.get("execution_started_at") or row.get("started_at"))
    heartbeat = timestamp(transport.get("updated_at"))

    def event_at(event):
        value = timestamp(event.get("_eval_at"))
        if value and value <= now + timedelta(seconds=15):
            return value
        seconds = event.get("_eval_elapsed_seconds")
        if (
            anchor
            and type(seconds) in (float, int)
            and math.isfinite(seconds)
            and 0 <= seconds <= 365 * 86400
        ):
            return anchor + timedelta(seconds=seconds)
        return None

    pending = recent = thinking = None
    for event, source in read_events(root, base):
        kind = event.get("type")
        at = event_at(event)
        if kind == "tool_call":
            pending = dict(
                tool=clean(event.get("tool", "tool"), limit=80),
                targets=extract(event.get("args", {})),
                scan=bool(
                    re.search(
                        r"\b(nmap|masscan|rustscan|naabu)\b",
                        str(event.get("tool", "")) + " " + str(event.get("args", {})),
                        re.I,
                    )
                ),
                at=at,
                source=source,
            )
            recent = thinking = None
        elif kind == "tool_status" and pending and event.get("tool") == pending["tool"]:
            pending["at"] = at or pending["at"]
        elif kind == "tool_result":
            recent = dict(
                tool=clean(event.get("tool", "tool"), limit=80),
                targets=extract(event.get("args", {})),
                scan=bool(
                    re.search(
                        r"\b(nmap|masscan|rustscan|naabu)\b",
                        str(event.get("tool", "")) + " " + str(event.get("args", {})),
                        re.I,
                    )
                ),
                at=at,
                source=source,
            )
            pending = thinking = None
        elif (
            kind == "status"
            and str(event.get("message", "")).startswith("Calling ")
            and "(turn " in str(event.get("message", ""))
        ):
            pending = None
            thinking = dict(at=at, source=source, agent_state="waiting_model")
        elif kind in ("reasoning", "response") and not pending:
            thinking = dict(at=at, source=source, agent_state="reasoning" if kind == "reasoning" else "responding")
        elif kind in (
            "chat_done",
            "chat_cancelled",
            "error",
            "tool_timeout_decision",
            "post_tool_reply_decision",
        ):
            pending = recent = thinking = None
    selected = pending or thinking or recent or {}
    at = selected.get("at")
    fresh_heartbeat = bool(
        heartbeat and -15 <= (now - heartbeat).total_seconds() <= HEARTBEAT_SECONDS
    )
    phase = transport.get("phase")
    service = transport.get("service_status") or {}
    worker_live = bool(
        active
        and row.get("status") == "running"
        and phase == "executing"
        and fresh_heartbeat
        and service.get("SubState") not in ("exited", "failed", "dead")
        and service.get("LoadState") != "not-found"
    )
    lagging = transport.get("transcript_offset", 0) < transport.get(
        "transcript_size", 0
    )
    reviewing = bool(
        monitor.get("checks") and monitor["checks"][-1].get("status") == "running"
    )
    expires = None
    mode = "idle"
    if pending:
        mode = "tool"
        if worker_live:
            expires = heartbeat + timedelta(seconds=HEARTBEAT_SECONDS)
    elif selected is recent and at:
        mode = "recent"
        expires = at + timedelta(seconds=RECENT_SECONDS)
    elif selected is thinking and at:
        mode = "thinking"
        expires = (
            heartbeat + timedelta(seconds=HEARTBEAT_SECONDS)
            if worker_live
            else at + timedelta(seconds=HEARTBEAT_SECONDS)
        )
    live = bool(
        worker_live and not lagging and not reviewing and expires and expires > now
    )
    if not active or row.get("status") != "running":
        mode = "finished"
        live = False
    elif lagging:
        mode = "catching-up"
        live = False
    elif reviewing:
        mode = "reviewing"
        live = False
    elif selected and not live:
        mode = "stale"
    targets = []
    step_ids = set()
    for address in selected.get("targets", []):
        matched = [
            node["id"]
            for node in nodes
            if any(overlaps(address, target) for target in node.get("targets", []))
        ]
        step_ids.update(matched)
        targets.append(dict(address=address, step_ids=matched, range="/" in address))
    monitor_at = timestamp(monitor.get("updated_at"))
    context_fresh = bool(
        monitor_at and 0 <= (now - monitor_at).total_seconds() <= CONTEXT_SECONDS
    )
    context = []
    if not targets and (pending or thinking) and context_fresh:
        known = {node["id"] for node in nodes}
        context = [key for key in monitor.get("targeted_steps", []) if key in known]
        step_ids.update(context)
    tool = selected.get("tool")
    scan = bool(
        selected.get("scan")
        or tool
        and any(name in tool.lower() for name in ("nmap", "masscan", "scan"))
    )
    return dict(
        live=live,
        phase=mode,
        tool=tool,
        agent_state=(selected.get("agent_state", "using_tool" if pending else "tool_complete" if recent else "idle") if mode in ("thinking", "tool", "recent") else mode),
        kind="scan" if scan else "interaction",
        targets=targets,
        step_ids=sorted(step_ids) if live else [],
        context_step_ids=context if live else [],
        observed_at=now.isoformat(),
        event_at=at.isoformat() if at else None,
        expires_at=expires.isoformat() if live else None,
        source=selected.get("source"),
        description="IP targets come from recorded tool arguments; monitor focus supplies challenge context. This is action telemetry, not a packet trace.",
    )
