"""Reviewable condition definitions; host paths never come from browser input."""

from copy import deepcopy
from pathlib import Path
import re
from .samples import SampleRequestError


def catalogue(runtime):
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
        ),
    }
    for item in runtime["conditions"]:
        name = item["id"]
        while name in choices:
            name = "configured-" + name
        choices[name] = {k: deepcopy(v) for k, v in item.items() if k != "id"}
        choices[name]["collect_source"] = item["id"]
    return choices


def validate(value, runtime):
    if not isinstance(value, dict) or set(value) - {
        "order_seed",
        "reset_each_trial",
        "conditions",
        "pricing",
        "use_configured_reset_hooks",
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
    rows = value.get("conditions", [])
    if not isinstance(rows, list) or len(rows) > 16:
        raise SampleRequestError("Supply up to 16 experiment conditions")
    choices = catalogue(runtime)
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
            "provide_progressive_hints",
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
        if type(row.get("provide_progressive_hints", False)) is not bool:
            raise SampleRequestError("Condition hints must be boolean")
    return deepcopy(value)


def apply(value, runtime, cfg, folder, defaults, source_cfg=None):
    value = validate(value, defaults)
    runtime["order_seed"] = value.get("order_seed", 0)
    cfg["reset_each_trial"] = value.get("reset_each_trial", True)
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
    if not value.get("conditions"):
        return
    folder.mkdir(parents=True, exist_ok=True)
    runtime["conditions"] = []
    cfg["collect"] = {}
    for row in value["conditions"]:
        original = deepcopy(catalogue(defaults)[row["source"]])
        collect_source = original.pop("collect_source", None)
        original.update(
            id=row["id"],
            tools=row["tools"],
            provide_progressive_hints=row.get("provide_progressive_hints", False),
        )
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
        original.setdefault("guidance_files", [])
        runtime["conditions"].append(original)
        if source_cfg and collect_source in source_cfg.get("collect", {}):
            cfg["collect"][row["id"]] = deepcopy(source_cfg["collect"][collect_source])
            cfg["artifacts"] = deepcopy(source_cfg.get("artifacts", []))
