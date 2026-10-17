"""Graph snapshots from frozen challenge plans and collected monitor evidence only."""

import json
from .run_artifacts import open_saved
from .diagnostics import clean
from cyber_agent_flow_eval.network_targets import extract
from .live_activity import observe


def saved(root, name, fallback):
    try:
        with open_saved(root, name) as stream:
            content = stream.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            return fallback
        return json.loads(content)
    except (OSError, ValueError):
        return fallback


def text(value, limit=1200):
    return clean(value or "", limit=limit)


def citations(values):
    return (
        [
            {k: item[k] for k in ("file", "offset", "limit") if k in item}
            for item in values[:8]
            if isinstance(item, dict)
        ]
        if isinstance(values, list)
        else []
    )


def build(root, journal, rows, *, active=False):
    manifest = saved(root, "evaluation/manifest.json", {})
    tasks = (
        manifest.get("spec", {}).get("tasks")
        or saved(root, "suite/participant/tasks.json", [])
        or journal.get("scenario_experiment", {}).get("evaluation_tasks", [])
    )
    tasks = {
        task["id"]: task for task in tasks if isinstance(task, dict) and task.get("id")
    }
    result = []
    selected_rows = rows[-10:]
    for row in selected_rows or [
        dict(trial_id=None, task_id=key, attempt=0) for key in list(tasks)[:32]
    ]:
        task = tasks.get(row.get("task_id"), {})
        plan = task.get("challenge_plan") or {}
        criteria = {c["id"]: c for c in task.get("rubric", {}).get("criteria", [])}
        steps = plan.get("steps") or [
            dict(
                id=key,
                node_id="",
                title=item["requirement"],
                criterion_ids=[key],
                requires=[],
            )
            for key, item in criteria.items()
        ]
        if not steps:
            steps = [
                dict(
                    id=task.get("id") or row.get("task_id") or "task",
                    node_id="",
                    title=task.get("prompt") or "Task",
                    criterion_ids=[],
                    requires=[],
                )
            ]
        base = (
            "evaluation/" + row["attempt_path"] + "/"
            if row.get("attempt_path")
            else None
        )
        monitor = (
            saved(
                root, base + "progress-monitor.json", row.get("progress_monitor") or {}
            )
            if base
            else {}
        )
        assistance = saved(root, base + "assistance.json", {}) if base else {}
        events = [
            dict(
                id=e.get("id"),
                step_id=e.get("step_id"),
                node_id=e.get("node_id"),
                turn=e.get("turn"),
                at=e.get("at"),
                solution=bool(e.get("solution")),
                text=text(e.get("text"), 4000),
                truncated=len(e.get("text", "")) > 4000,
                reason=text(e.get("reason")),
            )
            for e in assistance.get("events", [])[-100:]
        ]
        findings = monitor.get("criteria", {})
        final = row.get("criterion_results") or []
        if final:
            findings = {f["id"]: f for f in final}
        completed = set(monitor.get("completed_steps", [])) | set(
            assistance.get("completed_steps", [])
        )
        nodes = []
        for step in steps[:32]:
            key = step["id"]
            refs = step.get("criterion_ids", [])
            checks = [
                dict(
                    id=cid,
                    requirement=text(criteria.get(cid, {}).get("requirement") or cid),
                    essential=criteria.get(cid, {}).get("essential", True),
                    status=findings.get(cid, {}).get("status", "unverified"),
                    reason=text(findings.get(cid, {}).get("reason")),
                    evidence=citations(findings.get(cid, {}).get("evidence", [])),
                )
                for cid in refs
            ]
            required = [c for c in checks if c["essential"]] or checks
            observation = monitor.get("observations", {}).get(key, {})
            discoveries = [
                text(value, 500) for value in observation.get("discoveries", [])[:16]
            ]
            solved = (
                all(c["status"] == "satisfied" for c in required)
                if required and final
                else key in completed
                or bool(required)
                and all(c["status"] == "satisfied" for c in required)
            )
            if not refs and not plan and row.get("verified_success") is True:
                solved = True
            partial = any(c["status"] == "satisfied" for c in checks) or bool(
                discoveries
            )
            targeted = (
                key in monitor.get("targeted_steps", [])
                or observation.get("activity") == "targeted"
                or any(c["status"] == "unmet" and c["evidence"] for c in checks)
            )
            state = (
                "solved"
                if solved
                else "partial" if partial else "targeted" if targeted else "other"
            )
            releases = [
                e
                for e in events
                if e["step_id"] == key
                or not e["step_id"]
                and e["node_id"]
                and e["node_id"] == step.get("node_id")
            ]
            targets = list(dict.fromkeys(
                extract(step["title"] + " " + " ".join(c["requirement"] for c in checks))
                + observation.get("targets", [])
            ))
            nodes.append(
                dict(
                    id=key,
                    node_id=step.get("node_id"),
                    title=text(step["title"], 200),
                    target=targets[0] if targets else "",
                    targets=targets,
                    requires=step.get("requires", []),
                    state=state,
                    eligible=all(dep in completed for dep in step.get("requires", [])),
                    deferred=key in assistance.get("deferred_steps", []),
                    satisfied=sum(c["status"] == "satisfied" for c in checks),
                    total=len(checks),
                    criteria=checks,
                    discoveries=discoveries,
                    observation=text(observation.get("summary")),
                    observation_turn=observation.get("turn"),
                    evidence=citations(observation.get("evidence", [])),
                    audit=observation.get("audit"),
                    hints=sum(not e["solution"] for e in releases),
                    solutions=sum(e["solution"] for e in releases),
                    releases=releases,
                    focus=key in monitor.get("targeted_steps", []),
                    assistance_focus=key == assistance.get("active_step"),
                )
            )
        solved_ids = {node["id"] for node in nodes if node["state"] == "solved"}
        for node in nodes:
            node["eligible"] = all(dep in solved_ids for dep in node["requires"])
        known = {n["id"] for n in nodes}
        checks = monitor.get("checks", [])
        result.append(
            dict(
                trial_id=row.get("trial_id"),
                task_id=row.get("task_id"),
                condition_id=row.get("condition_id"),
                attempt=row.get("attempt"),
                status=row.get("status", "pending"),
                verified_success=row.get("verified_success"),
                graph_source=(
                    "ScenarioForge challenge dependencies"
                    if plan
                    else "Task success criteria"
                ),
                nodes=nodes,
                activity=observe(root,base,row,monitor,nodes,active=active) if base else None,
                edges=[
                    dict(source=dep, target=n["id"])
                    for n in nodes
                    for dep in n["requires"]
                    if dep in known
                ],
                monitor=dict(
                    enabled=bool(
                        assistance.get("progress_monitor_enabled")
                        or row.get("progress_monitor_enabled")
                        or monitor
                    ),
                    updated_at=monitor.get("updated_at"),
                    checks=len(checks),
                    latest=(
                        {
                            k: checks[-1].get(k)
                            for k in ("turn", "status", "error", "seconds", "audit")
                        }
                        if checks
                        else None
                    ),
                    error=text(assistance.get("progress_monitor_error")),
                    basis="final review" if final else "monitor checkpoints",
                    observed_facts=len(assistance.get("observed_fact_ids", [])),
                    revealed_facts=len(assistance.get("revealed_fact_ids", [])),
                ),
                releases=events,
                counts={
                    state: sum(n["state"] == state for n in nodes)
                    for state in ("solved", "partial", "targeted", "other")
                },
            )
        )
    return dict(
        version=1,
        trials=result,
        live=any(t.get("status") == "running" for t in result),
        description="Observed challenge progress; final scoring is independent. Planned nodes are not proof of discovery.",
    )
