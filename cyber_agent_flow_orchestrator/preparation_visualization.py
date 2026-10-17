"""Preparation topology from recorded checks only; never infers live connectivity."""

from .diagnostics import clean

PASS = {"pass", "passed", "completed"}
FAIL = {"fail", "failed", "error", "interrupted", "cancelled", "completed_with_errors"}


def aggregate(checks):
    states = [item["status"] for item in checks]
    if any(state == "failed" for state in states):
        return "failed"
    if "running" in states:
        return "running"
    if "warning" in states:
        return "warning"
    if states and all(state in ("passed", "skipped") for state in states):
        return "passed" if "passed" in states else "skipped"
    return "partial" if "passed" in states else "pending"


def build(steps, roles):
    phases = {step["id"]: step for step in steps}
    preflight = phases.get("preflight", {})
    inputs = {check["id"]: check for check in preflight.get("input_checks", [])}
    guests = {check["vmid"]: check for check in preflight.get("vm_checks", [])}
    roles = {role["role"]: role for role in roles}

    def check(key, label, record=None, phase="preflight"):
        record = record or {}
        raw = record.get("status", "pending")
        status = (
            "passed"
            if raw in PASS
            else (
                "failed"
                if raw in FAIL
                else (
                    "skipped"
                    if raw in ("skip", "skipped")
                    else (
                        "running"
                        if raw in ("running", "waiting")
                        else "warning" if raw == "warning" else "pending"
                    )
                )
            )
        )
        return dict(
            id=key,
            label=label,
            status=status,
            phase=phase,
            message=clean(
                record.get("message")
                or record.get("error")
                or record.get("operation")
                or (
                    "Recorded check passed."
                    if status == "passed"
                    else (
                        "Not checked yet."
                        if status == "pending"
                        else "Recorded status: " + raw
                    )
                )
            ),
            seconds=record.get("seconds", record.get("elapsed_seconds")),
            at=record.get("ended_at") or record.get("started_at"),
        )

    def input_check(key, label):
        return check(key, label, inputs.get(key))

    def phase_check(key, label):
        return check(key, label, phases.get(key), key)

    def guest_check(role):
        vmid = roles.get(role, {}).get("vmid")
        if vmid is None:
            return []
        record = guests.get(vmid)
        data = (
            None
            if record is None
            else dict(
                status="pass" if record.get("ready") is True else "fail",
                message=(
                    (
                        "Guest ready; recorded leftover jobs cleaned up: "
                        + str(len(record.get("stopped", [])))
                    )
                    if record.get("ready") is True
                    else "Guest readiness check did not pass."
                ),
            )
        )
        return [check("guest-" + role, "Guest access / cleanup", data)]

    participant = [
        *guest_check("participant"),
        input_check("engine", "CAF runtime"),
        input_check("participant-model", "Agent model access"),
    ]
    scenarioforge = [
        *guest_check("scenarioforge"),
        input_check("scenarioforge", "Application / configuration"),
    ]
    if "snapshot" in phases:
        scenarioforge.append(phase_check("snapshot", "Frozen scenario XML"))
    app = roles.get("scenarioforge", {}).get("vmid")
    scenarioforge += [
        phase_check(step["id"], "Prepare scenario inputs")
        for step in steps
        if step["id"].startswith("prepare-") and step.get("vmid") == app
    ]
    core = guest_check("core")
    if roles.get("core", {}).get("vmid") is not None or "core" in inputs:
        core.append(input_check("core", "CORE paths / HITL interface"))
    core.append(input_check("core-connection", "SSH / gRPC connection"))
    if "deploy" in phases:
        core.append(phase_check("deploy", "Scenario deployment"))
    core.append(phase_check("readiness", "Scenario readiness"))

    nodes = []
    for role, title, description, checks in [
        (
            "participant",
            "Cyber-Agent-Flow",
            "Agent, tools and model access",
            participant,
        ),
        ("scenarioforge", "ScenarioForge", "Scenario inputs and export", scenarioforge),
        ("core", "CORE", "Scenario network and services", core),
    ]:
        nodes.append(
            dict(
                id=role,
                title=title,
                description=description,
                vmid=roles.get(role, {}).get("vmid"),
                status=aggregate(checks),
                passed=sum(item["status"] == "passed" for item in checks),
                total=len(checks),
                checks=checks,
            )
        )

    handoff = [
        phase_check("fetch", "Evaluation package downloaded"),
        phase_check("freeze", "Participant resources frozen"),
        phase_check("import", "Evaluation plan imported"),
    ]
    links = [
        dict(
            id="package",
            source="scenarioforge",
            target="participant",
            label="Tasks · via orchestrator",
            description="The orchestrator downloads the ScenarioForge package and freezes/imports the participant task and resources. This is a data handoff, not a direct VM network probe.",
            checks=handoff,
        ),
        dict(
            id="core-connection",
            source="scenarioforge",
            target="core",
            label="SSH / gRPC",
            description="Recorded ScenarioForge-to-CORE connection test.",
            checks=[input_check("core-connection", "SSH / gRPC connection")],
        ),
        dict(
            id="hitl",
            source="participant",
            target="core",
            label="HITL · interface check",
            description="Checks CORE paths and its configured HITL interface. A passed interface check does not prove end-to-end participant-to-target reachability.",
            checks=[input_check("core", "CORE paths / HITL interface")],
            interface_only=True,
        ),
    ]
    for link in links:
        link["status"] = aggregate(link["checks"])
    return dict(
        nodes=nodes,
        edges=links,
        description="Recorded preparation checks, separate from challenge completion. Green means the listed checks passed, not a fresh connectivity probe.",
    )
