"""Preparation success is based on recorded checks, independent of trial outcome."""

from copy import deepcopy
import pytest
from cyber_agent_flow_orchestrator.preparation_visualization import build


def preparation_fixture():
    roles = [
        dict(role="participant", vmid=9403),
        dict(role="scenarioforge", vmid=9402),
        dict(role="core", vmid=9401),
    ]
    preflight = dict(
        id="preflight",
        label="Check inputs",
        status="completed",
        input_checks=[
            dict(
                id=key,
                label=key,
                status="pass",
                message="Recorded check verified",
                seconds=1,
            )
            for key in (
                "engine",
                "participant-model",
                "scenarioforge",
                "core-connection",
                "core",
            )
        ],
        vm_checks=[dict(vmid=role["vmid"], ready=True, stopped=[]) for role in roles],
    )
    steps = [preflight] + [
        dict(
            id=key,
            label=label,
            status="completed",
            vmid=9402 if key.startswith("prepare-") else None,
            elapsed_seconds=2,
        )
        for key, label in [
            ("snapshot", "Verify frozen XML"),
            ("prepare-inputs", "Prepare scenario inputs"),
            ("deploy", "Deploy scenario"),
            ("fetch", "Download package"),
            ("readiness", "Validate readiness"),
            ("freeze", "Freeze resources"),
            ("import", "Import tasks"),
        ]
    ]
    return steps, roles


def test_preparation_and_connection_checks_pass_independently_of_trial_errors():
    steps, roles = preparation_fixture()
    steps.append(dict(id="evaluate", status="failed"))
    result = build(steps, roles)
    assert [node["status"] for node in result["nodes"]] == ["passed"] * 3
    assert [edge["status"] for edge in result["edges"]] == ["passed"] * 3
    assert (
        result["edges"][0]["source"] == "scenarioforge"
        and result["edges"][0]["target"] == "participant"
    )
    assert "orchestrator" in result["edges"][0]["label"]
    assert result["edges"][2]["interface_only"]
    assert "does not prove end-to-end" in result["edges"][2]["description"]


def test_failed_connection_is_red_without_reclassifying_other_app_checks():
    steps, roles = preparation_fixture()
    steps[0]["input_checks"][3].update(
        status="fail", message="SSH authentication failed"
    )
    result = build(steps, roles)
    assert result["nodes"][2]["status"] == "failed"
    assert result["edges"][1]["status"] == "failed"
    assert result["nodes"][0]["status"] == result["nodes"][1]["status"] == "passed"
    assert any(
        check["message"] == "SSH authentication failed"
        for check in result["nodes"][2]["checks"]
    )


def test_missing_checks_never_become_green_from_an_overall_pass():
    steps, roles = preparation_fixture()
    steps[0]["input_checks"] = []
    steps[0]["vm_checks"] = []
    result = build(steps, roles)
    assert all(node["status"] != "passed" for node in result["nodes"])
    assert result["edges"][1]["status"] == result["edges"][2]["status"] == "pending"
    assert result["edges"][0]["status"] == "passed"  # Actual recorded package handoff.


def test_preflight_guest_readiness_failure_and_running_phase():
    steps, roles = preparation_fixture()
    steps[0]["vm_checks"][0]["ready"] = False
    steps[1]["status"] = "running"
    result = build(steps, roles)
    assert result["nodes"][0]["status"] == "failed"
    assert result["nodes"][1]["status"] == "running"
    assert result["nodes"][2]["status"] == "passed"


def test_topology_is_available_before_any_check_or_trial():
    _, roles = preparation_fixture()
    result = build([], roles)
    assert len(result["nodes"]) == 3 and len(result["edges"]) == 3
    assert all(
        item["status"] == "pending" for item in result["nodes"] + result["edges"]
    )
    assert [node["title"] for node in result["nodes"]] == [
        "Cyber-Agent-Flow",
        "ScenarioForge",
        "CORE",
    ]
