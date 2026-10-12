"""Owner-scoped library for reviewed CAF-generated Markdown helpers."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from cyber_agent_flow_eval import integration as ev

from .access import AccessDenied
from .auth import private_file
from .samples import SampleRequestError
from .workspaces import private_directory


MAX_HELPERS = 256
MAX_HELPER_BYTES = 64 * 1024
HELPER_ID = re.compile(r"helper-[0-9a-f]{24}")


def _directory(workspace):
    return private_directory(workspace.path / "generated-helpers")


def _path(workspace, helper_id):
    if not isinstance(helper_id, str) or not HELPER_ID.fullmatch(helper_id):
        raise SampleRequestError("Invalid generated helper ID")
    root = _directory(workspace)
    path = root / helper_id
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise AccessDenied("Invalid generated helper path")
    return path


def _read(workspace, helper_id, *, include_content=False):
    directory = _path(workspace, helper_id)
    manifest_path, content_path = directory / "manifest.json", directory / "helper.md"
    if (not directory.is_dir() or manifest_path.is_symlink() or content_path.is_symlink()
            or not manifest_path.is_file() or not content_path.is_file()
            or manifest_path.stat().st_size > 64 * 1024
            or content_path.stat().st_size > MAX_HELPER_BYTES):
        raise SampleRequestError("Generated helper is unavailable")
    manifest = ev.read_json(manifest_path)
    content = content_path.read_text()
    if (manifest.get("version") != 1 or manifest.get("id") != helper_id
            or hashlib.sha256(content.encode()).hexdigest() != manifest.get("helper_sha256")):
        raise SampleRequestError("Generated helper failed integrity validation")
    result = deepcopy(manifest)
    if include_content:
        result["content"] = content
    return result


def listing(workspace):
    root = _directory(workspace)
    result = []
    for path in root.iterdir():
        if len(result) >= MAX_HELPERS:
            break
        if path.is_symlink() or not path.is_dir() or not HELPER_ID.fullmatch(path.name):
            continue
        try:
            result.append(_read(workspace, path.name))
        except (OSError, ValueError, KeyError, SampleRequestError):
            continue
    return sorted(result, key=lambda item: (item.get("created_at", ""), item["id"]), reverse=True)


def save(workspace, source_run_id, analysis):
    helper = analysis.get("helper") if isinstance(analysis, dict) else None
    if not isinstance(helper, str) or not 40 <= len(helper) <= 16_000:
        raise SampleRequestError("A validated generated helper is required")
    source_path = workspace.run_path(source_run_id)
    source = ev.read_json(source_path / "workflow.json")
    scenario = source.get("scenario_experiment")
    if not isinstance(scenario, dict):
        raise SampleRequestError("Generated helpers require a scenario experiment")
    helper_sha = hashlib.sha256(helper.encode()).hexdigest()
    identity = hashlib.sha256((source_run_id + "\0" + str(analysis.get("analysis_sha256"))
                               + "\0" + helper_sha).encode()).hexdigest()
    helper_id = "helper-" + identity[:24]
    directory = _path(workspace, helper_id)
    if directory.exists():
        existing = _read(workspace, helper_id, include_content=True)
        if existing["content"] != helper:
            raise SampleRequestError("Generated helper ID collision")
        return existing
    name = scenario.get("scenario") or source.get("workflow", {}).get("id") or source_run_id
    created_at = analysis.get("created_at") or datetime.now(timezone.utc).isoformat()
    model = analysis.get("model") if isinstance(analysis.get("model"), dict) else {}
    manifest = {
        "version": 1,
        "id": helper_id,
        "name": f"Generated helper · {name}",
        "created_at": created_at,
        "source_run_id": source_run_id,
        "source_workflow_hash": source.get("workflow_hash"),
        "source_scenario": {
            key: scenario.get(key) for key in ("id", "scenario", "sha256", "snapshot_token")
            if scenario.get(key) is not None
        },
        "model": {key: model.get(key) for key in ("provider", "name", "url") if model.get(key) is not None},
        "analysis_sha256": analysis.get("analysis_sha256"),
        "input_sha256": analysis.get("input_sha256"),
        "helper_sha256": helper_sha,
        "reviewed": False,
        "reviewed_at": None,
    }
    with ev.lease(workspace.path / ".workspace.lock"):
        if directory.exists():
            return _read(workspace, helper_id, include_content=True)
        private_directory(directory)
        try:
            private_file(directory / "helper.md", helper.encode())
            private_file(directory / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        except BaseException:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return dict(manifest, content=helper)


def read(workspace, helper_id):
    return _read(workspace, helper_id, include_content=True)


def set_reviewed(workspace, helper_id, reviewed):
    if type(reviewed) is not bool:
        raise SampleRequestError("reviewed must be a boolean")
    with ev.lease(workspace.path / ".workspace.lock"):
        manifest = _read(workspace, helper_id)
        manifest["reviewed"] = reviewed
        manifest["reviewed_at"] = datetime.now(timezone.utc).isoformat() if reviewed else None
        path = _path(workspace, helper_id) / "manifest.json"
        private_file(path, (json.dumps(manifest, indent=2) + "\n").encode(), replace=True)
    return manifest
