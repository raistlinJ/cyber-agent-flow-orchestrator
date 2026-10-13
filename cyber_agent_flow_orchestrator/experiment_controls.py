"""Reviewable condition definitions; host paths never come from browser input."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from .samples import SampleRequestError


def participant_catalogue(workspace, vmid):
    """Return a server-owned catalog pulled from the currently selected participant."""
    if vmid is None:
        return {}
    metadata_path = workspace.path / 'participant-tools.json'
    catalog_path = workspace.path / 'participant-tools-catalog.json'
    if (not metadata_path.is_file() or metadata_path.is_symlink()
            or not catalog_path.is_file() or catalog_path.is_symlink()):
        return {}
    try:
        metadata = json.loads(metadata_path.read_text())
        content = catalog_path.read_bytes()
        catalog = json.loads(content)
        tools = metadata['tools']
        names = [item['name'] for item in tools]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if (metadata.get('vmid') != vmid or metadata.get('source') != 'participant-vm'
            or metadata.get('catalog_sha256') != hashlib.sha256(content).hexdigest()
            or not isinstance(catalog, dict) or not isinstance(catalog.get('tools'), list)
            or not isinstance(tools, list) or not tools or len(set(names)) != len(names)
            or any(not isinstance(name, str) or not name for name in names)
            or names != [item.get('name') for item in catalog['tools'] if isinstance(item, dict)]):
        return {}
    return {'participant-vm': dict(catalog=str(catalog_path), tools=names,
                                   definitions=deepcopy(tools), guidance_files=[])}


def participant_guidance_assets(workspace, vmid, *, include_content=False):
    """Return the VM-bound CAF guidance snapshot, with content only to server code."""
    if vmid is None:
        return {}
    metadata_path = workspace.path / 'participant-tools.json'
    if not metadata_path.is_file() or metadata_path.is_symlink():
        return {}
    try:
        metadata = json.loads(metadata_path.read_text())
        path = Path(metadata['assets'])
        if path != workspace.path / 'participant-guidance-assets.json':
            return {}
        if not path.is_file() or path.is_symlink():
            return {}
        content = path.read_bytes()
        assets = json.loads(content)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if (metadata.get('vmid') != vmid or metadata.get('source') != 'participant-vm'
            or metadata.get('assets_sha256') != hashlib.sha256(content).hexdigest()
            or not isinstance(assets, list) or len(assets) > 500):
        return {}
    result = {}
    for item in assets:
        if not isinstance(item, dict):
            return {}
        asset_id, body = item.get('id'), item.get('content')
        if (not isinstance(asset_id, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,160}', asset_id)
                or asset_id in result or not isinstance(body, str)
                or item.get('sha256') != hashlib.sha256(body.encode()).hexdigest()):
            return {}
        result[asset_id] = deepcopy(item) if include_content else {
            key: deepcopy(value) for key, value in item.items() if key != 'content'}
    return result


def catalogue(runtime, extra_catalogs=None):
    builtins = Path(__file__).with_name("sample_data")
    choices = {
        "baseline": dict(
            catalog=str(builtins / "baseline.json"),
            tools=["nmap", "curl", "python3"],
            guidance_files=[],
        ),
        "http-helper": dict(
            catalog=str(builtins / "with-http-helper.json"),
            tools=["nmap", "curl", "python3", "http_flag_walk"],
            guidance_files=[],
            helper=dict(
                name="HTTP flag walker",
                kind="tool bundle",
                origin="Bundled sample helper",
                description="Follows same-origin links on a bounded HTTP(S) site and reports observed FLAG{…} values.",
                tags=["web", "discovery", "flags"],
            ),
        ),
    }
    for item in runtime["conditions"]:
        name = item["id"]
        while name in choices:
            name = "configured-" + name
        choices[name] = {k: deepcopy(v) for k, v in item.items() if k != "id"}
        choices[name]["collect_source"] = item["id"]
    for name, item in (extra_catalogs or {}).items():
        if name not in choices:
            choices[name] = deepcopy(item)
    return choices


def validate(value, runtime, extra_catalogs=None, guidance_assets=None):
    if not isinstance(value, dict) or set(value) - {
        "order_seed",
        "reset_each_trial",
        "conditions",
        "pricing",
        "use_configured_reset_hooks",
        "auto_approve_dangerous",
    }:
        raise SampleRequestError("Unknown experiment control fields")
    if (
        type(value.get("order_seed", 0)) is not int
        or not -(2**31) <= value.get("order_seed", 0) < 2**31
    ):
        raise SampleRequestError("Order seed must be a 32-bit integer")
    if type(value.get("reset_each_trial", True)) is not bool:
        raise SampleRequestError("reset_each_trial must be boolean")
    if type(value.get("use_configured_reset_hooks", False)) is not bool:
        raise SampleRequestError("use_configured_reset_hooks must be boolean")
    if type(value.get("auto_approve_dangerous", True)) is not bool:
        raise SampleRequestError("auto_approve_dangerous must be boolean")
    rows = value.get("conditions", [])
    if not isinstance(rows, list) or len(rows) > 16:
        raise SampleRequestError("Supply up to 16 experiment conditions")
    choices = catalogue(runtime, extra_catalogs)
    seen = set()
    if "pricing" in value:
        from cyber_agent_flow_eval.usage import validate_pricing

        try:
            validate_pricing(value["pricing"])
        except ValueError as exc:
            raise SampleRequestError(str(exc)) from None
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
            "id",
            "source",
            "tools",
            "guidance",
            "guidance_assets",
        }:
            raise SampleRequestError("Unknown condition fields")
        if (
            not isinstance(row.get("id"), str)
            or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,79}", row["id"])
            or row["id"] in seen
        ):
            raise SampleRequestError("Condition IDs must be valid and unique")
        seen.add(row["id"])
        if row.get("source") not in choices:
            raise SampleRequestError("Choose a configured condition catalog")
        if (
            not isinstance(row.get("tools"), list)
            or any(not isinstance(t, str) or not t for t in row["tools"])
            or len(set(row["tools"])) != len(row["tools"])
        ):
            raise SampleRequestError("Condition tools must be distinct names")
        if set(row["tools"]) - set(choices[row["source"]].get("tools", [])):
            raise SampleRequestError(
                "Select tools available in the configured condition catalog"
            )
        if (
            not isinstance(row.get("guidance", ""), str)
            or len(row.get("guidance", "")) > 32000
        ):
            raise SampleRequestError("Condition guidance exceeds 32000 characters")
        selected_assets = row.get('guidance_assets', [])
        if (not isinstance(selected_assets, list)
                or any(not isinstance(asset_id, str) or not asset_id for asset_id in selected_assets)
                or len(set(selected_assets)) != len(selected_assets)
                or set(selected_assets) - set(guidance_assets or {})):
            raise SampleRequestError('Select CAF guidance available in the participant snapshot')
    return deepcopy(value)


def apply(value, runtime, cfg, folder, defaults, source_cfg=None, extra_catalogs=None,
          guidance_assets=None):
    value = validate(value, defaults, extra_catalogs, guidance_assets)
    runtime["order_seed"] = value.get("order_seed", 0)
    cfg["reset_each_trial"] = value.get("reset_each_trial", True)
    runtime.setdefault("execution", {})["auto_approve_dangerous"] = value.get(
        "auto_approve_dangerous", defaults.get("execution", {}).get("auto_approve_dangerous", True)
    )
    if value.get("use_configured_reset_hooks"):
        if not defaults["backend"].get("before_trial"):
            raise SampleRequestError(
                "No extra reset/readiness hooks are configured on this server"
            )
        runtime["backend"]["before_trial"] = deepcopy(
            defaults["backend"]["before_trial"]
        )
    if value.get("pricing"):
        runtime["pricing"] = value["pricing"]
    for condition in runtime.get("conditions", []):
        # The Evaluation hint policy is the only orchestrator-level control.
        condition.pop("provide_progressive_hints", None)
    if not value.get("conditions"):
        return
    folder.mkdir(parents=True, exist_ok=True)
    runtime["conditions"] = []
    cfg["collect"] = {}
    for row in value["conditions"]:
        original = deepcopy(catalogue(defaults, extra_catalogs)[row["source"]])
        original.pop('definitions', None)
        original.pop('helper', None)
        collect_source = original.pop("collect_source", None)
        original.update(id=row["id"], tools=row["tools"])
        # Progressive hints are controlled once for the whole evaluation.
        original.pop("provide_progressive_hints", None)
        if source_cfg and "guidance_files" in source_cfg.get("collect", {}).get(
            collect_source, {}
        ):
            # Remote generation supplies the catalog's guidance; its local
            # template paths are placeholders, not additional authored files.
            original["guidance_files"] = []
        if row.get("guidance"):
            path = folder / (row["id"] + "-guidance.md")
            path.write_text(row["guidance"])
            path.chmod(0o600)
            original.setdefault("guidance_files", []).append(str(path))
        for index, asset_id in enumerate(row.get('guidance_assets', []), 1):
            asset = (guidance_assets or {})[asset_id]
            suffix = re.sub(r'[^A-Za-z0-9_-]+', '-', asset_id).strip('-')[:60]
            path = folder / f'{row["id"]}-caf-{index}-{suffix}.md'
            path.write_text(asset['content'])
            path.chmod(0o600)
            original.setdefault('guidance_files', []).append(str(path))
        original.setdefault("guidance_files", [])
        runtime["conditions"].append(original)
        if source_cfg and collect_source in source_cfg.get("collect", {}):
            cfg["collect"][row["id"]] = deepcopy(source_cfg["collect"][collect_source])
            cfg["artifacts"] = deepcopy(source_cfg.get("artifacts", []))
