"""Post-execution CAF analysis and generation of derived assisted experiments."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_eval.proxmox import authorized_operations

from .auth import private_file
from .samples import SampleRequestError
from .workspaces import Workspace


MAX_EVIDENCE_BYTES = 3 * 1024 * 1024
MAX_TRIALS = 12


def _latest_attempts(evaluation: Path) -> list[Path]:
    attempts = []
    for trial in sorted((evaluation / "trials").glob("trial-*")):
        candidates = sorted(
            (path for path in trial.glob("attempt-*") if path.is_dir()),
            key=lambda path: path.name,
        )
        if candidates:
            attempts.append(candidates[-1])
    # Errors, failures and partial results carry the most useful repair signal.
    def priority(path):
        try:
            result = ev.read_json(path / "evaluation.json")
            outcome = str((result.get("judge") or {}).get("outcome") or "").lower()
        except (FileNotFoundError, ValueError, TypeError):
            outcome = "error"
        return ({"error": 0, "fail": 1, "failed": 1, "partial": 2}.get(outcome, 3), path.name)
    return sorted(attempts, key=priority)[:MAX_TRIALS]


def collect_participant_evidence(run: Path) -> tuple[dict, list[dict]]:
    """Collect only evidence visible to the participant agent."""
    transcripts, annotations, calls, sources = [], [], [], []
    available_tools = set()
    total = 0
    for attempt in _latest_attempts(run / "evaluation"):
        roots = sorted((attempt / "guest-output" / "runs").glob("*/transcript.md"))
        if not roots:
            continue
        transcript_path = roots[-1]
        session = transcript_path.parent
        transcript = transcript_path.read_text(errors="replace")
        block = f"\n\n# Trial evidence: {attempt.parent.name} / {attempt.name}\n\n{transcript}"
        if total + len(block.encode()) > MAX_EVIDENCE_BYTES:
            break
        transcripts.append(block)
        total += len(block.encode())
        sources.append({"path": str(transcript_path.relative_to(run)),
                        "sha256": hashlib.sha256(transcript_path.read_bytes()).hexdigest()})
        annotation_path = session / "annotations.jsonl"
        if annotation_path.is_file() and not annotation_path.is_symlink():
            text = annotation_path.read_text(errors="replace")
            if total + len(text.encode()) <= MAX_EVIDENCE_BYTES:
                annotations.append(text)
                total += len(text.encode())
                sources.append({"path": str(annotation_path.relative_to(run)),
                                "sha256": hashlib.sha256(annotation_path.read_bytes()).hexdigest()})
        metadata_path = session / "metadata.json"
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                metadata = ev.read_json(metadata_path)
                available_tools.update(str(name) for name in metadata.get("available_tools", []) if name)
            except (ValueError, TypeError):
                pass
        for path in sorted((session / "tool_calls").glob("*.json")):
            if path.is_symlink() or len(calls) >= 2_000:
                continue
            try:
                record = ev.read_json(path)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            record = {key: record.get(key) for key in (
                "seq", "timestamp", "tool", "args", "duration_ms", "exit_code",
                "result", "stderr", "graceful_stop", "cancelled"
            )}
            record["trial"] = attempt.parent.name
            encoded = json.dumps(record).encode()
            if total + len(encoded) > MAX_EVIDENCE_BYTES:
                break
            calls.append(record)
            total += len(encoded)
            sources.append({"path": str(path.relative_to(run)),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not transcripts:
        raise SampleRequestError("No collected CAF transcript is available for post-execution analysis")
    payload = {
        "run_id": "post-" + uuid.uuid4().hex,
        "transcript": "".join(transcripts),
        "annotations": "\n".join(annotations),
        "tool_calls": calls,
        "metadata": {"available_tools": sorted(available_tools)},
    }
    return payload, sources


def analyze(access, root, run_id, progress=lambda step, message: None):
    """Run CAF's headless analyzer inside the participant VM."""
    if not isinstance(run_id, str) or not re.fullmatch(r"scenario-[0-9a-f]{32}", run_id):
        raise SampleRequestError("Choose a saved scenario experiment")
    workspace = Workspace(root, access.username)
    run = workspace.run_path(run_id)
    record = ev.read_json(run / "workflow.json")
    if not record.get("scenario_experiment") or record.get("status") not in {
        "completed", "completed_with_errors", "failed", "interrupted", "cancelled"
    }:
        raise SampleRequestError("Post-execution analysis requires a finished scenario experiment")
    progress(1, "Selecting participant-visible transcripts and tool evidence")
    payload, sources = collect_participant_evidence(run)
    runtime = record["runtime"]
    model = deepcopy(runtime["model"])
    required = ("provider", "name", "url", "ssl_verify")
    if any(key not in model for key in required):
        raise SampleRequestError("The frozen participant model is incomplete; URL, model, TLS setting and credential binding are required")
    payload["model"] = {key: model[key] for key in required}
    payload["model"]["api_key_env"] = model.get("api_key_env", "")
    content = json.dumps(payload, separators=(",", ":")).encode()
    if len(content) > 4 * 1024 * 1024:
        raise SampleRequestError("Participant evidence exceeds the post-analysis input limit")

    backend = runtime["backend"]
    vmid = backend["participant_vmid"]
    access.require_vm(vmid)
    token = uuid.uuid4().hex
    remote_root = backend["workspace"].rstrip("/") + "/post-analysis-" + token
    input_path = remote_root + "/attempt/input.json"
    output_path = remote_root + "/attempt/output.json"
    unit = "caf-eval-hook-" + token
    agent = ev.GuestAgent(backend)
    progress(2, f"Preparing CAF analysis workspace on participant VM {vmid}")
    with authorized_operations(access.qm):
        agent.call(vmid, "mkdir", path=remote_root, user=backend["user"], timeout=60)
        try:
            agent.put(vmid, input_path, content)
            progress(3, "CAF is analyzing the completed transcript and tool use")
            timeout = min(3600, max(600, int(runtime.get("judge", {}).get("timeout_seconds", 1200))))
            command = [runtime["engine"]["python"],
                       str(Path(runtime["engine"]["path"]) / "post_run_analysis.py"),
                       "--input", input_path, "--output", output_path]
            try:
                result = agent.call(vmid, "hook", timeout=timeout + 45, unit=unit,
                    seconds=timeout, argv=command, user=backend["user"], cwd=runtime["engine"]["path"],
                    environment_file=backend.get("environment_file"))
                try:
                    log = agent.get(vmid, result["log_path"]).decode(errors="replace")
                finally:
                    agent.call(vmid, "unlink", path=result["log_path"])
                if result["exitcode"]:
                    raise SampleRequestError("CAF post-run analysis failed: " + (log[-2000:] or "no command output"))
                progress(4, "Collecting and validating the generated helper")
                analysis = json.loads(agent.get(vmid, output_path))
            finally:
                try:
                    agent.call(vmid, "stop", unit=unit, timeout=30)
                except Exception:
                    pass
        finally:
            for path in (input_path, output_path):
                try:
                    agent.call(vmid, "unlink", path=path)
                except Exception:
                    pass
    if not isinstance(analysis, dict) or analysis.get("version") != 1:
        raise SampleRequestError("CAF returned an invalid post-run analysis result")
    helper = analysis.get("helper")
    if not isinstance(helper, str) or not 40 <= len(helper) <= 16_000:
        raise SampleRequestError("CAF did not generate a usable helper")
    analysis["evidence"] = sources
    analysis["source_run_id"] = run_id
    analysis["created_at"] = datetime.now(timezone.utc).isoformat()
    return analysis
