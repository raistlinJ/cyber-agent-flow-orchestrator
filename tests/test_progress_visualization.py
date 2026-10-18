"""Frozen dependency graphs use cited progress and released assistance only."""

import json
from copy import deepcopy
from cyber_agent_flow_orchestrator.progress_visualization import build


def save(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fixture(root):
    titles = [
        "Discover entry service",
        "Read configuration on 10.0.0.3",
        "Inspect vulnerable endpoint",
        "Recover final proof",
    ]
    steps = [
        dict(
            id=f"s{i}",
            node_id=f"n{i}",
            title=title,
            criterion_ids=[f"c{i}a", f"c{i}b"],
            requires=[] if i == 1 else [f"s{i-1}"],
            hints=["UNRELEASED_HINT"],
            solution="PRIVATE_SOLUTION",
        )
        for i, title in enumerate(titles, 1)
    ]
    criteria = [
        dict(
            id=cid,
            requirement="Observed " + cid,
            essential=True,
            private_reference="PRIVATE_EXPECTED",
        )
        for step in steps
        for cid in step["criterion_ids"]
    ]
    task = dict(
        id="task",
        prompt="Investigate the scenario.",
        challenge_plan=dict(version=1, steps=steps),
        rubric=dict(version=1, criteria=criteria),
    )
    save(root, "evaluation/manifest.json", dict(spec=dict(tasks=[task])))
    citation = dict(file="events.jsonl", offset=0, limit=100)
    monitor = dict(
        version=2,
        updated_at="2026-10-16T20:00:00Z",
        completed_steps=["s1"],
        criteria={
            cid: dict(
                status="satisfied", reason="Observed tool result", evidence=[citation]
            )
            for cid in ["c1a", "c1b", "c2a"]
        },
        observations={
            "s2": dict(
                activity="discovered",
                summary="Read service configuration.",
                discoveries=["Service version found"],
                turn=3,
                evidence=[citation],
                audit="progress-checks/turn-0003/judge.json",
            ),
            "s3": dict(
                activity="targeted",
                summary="Requested endpoint; access denied.",
                discoveries=[],
                turn=4,
                evidence=[citation],
            ),
        },
        targeted_steps=["s3"],
        checks=[dict(turn=4, status="completed", seconds=2)],
    )
    row = dict(
        trial_id="trial-000001",
        task_id="task",
        attempt=1,
        condition_id="configuration-1",
        attempt_path="trials/trial-000001/attempt-0001",
        status="running",
    )
    base = "evaluation/" + row["attempt_path"] + "/"
    save(root, base + "progress-monitor.json", monitor)
    save(
        root,
        base + "assistance.json",
        dict(
            progress_monitor_enabled=True,
            active_step="s2",
            events=[
                dict(
                    id="hint1",
                    step_id="s2",
                    turn=3,
                    at="2026-10-16T20:00:00Z",
                    text="Inspect the configuration endpoint.",
                    solution=False,
                    tier="medium",
                    interpretation="The configuration service is the strongest lead.",
                    next_action="Inspect the configuration endpoint.",
                    success_signal="A response containing the required setting.",
                    source="catalog_assistance",
                    monitor_observation={
                        "summary": "The service configuration was reached.",
                        "evidence": [citation],
                    },
                ),
                dict(
                    id="solution1",
                    step_id="s2",
                    turn=5,
                    at="2026-10-16T20:00:05Z",
                    text="Released walkthrough only.",
                    solution=True,
                ),
            ],
        ),
    )
    return row, task, monitor


def test_graph_states_dependencies_evidence_and_released_assistance(tmp_path):
    row, _, _ = fixture(tmp_path)
    graph = build(tmp_path, {}, [row])["trials"][0]
    assert graph["counts"] == dict(solved=1, partial=1, targeted=1, other=1)
    assert graph["edges"] == [
        dict(source="s1", target="s2"),
        dict(source="s2", target="s3"),
        dict(source="s3", target="s4"),
    ]
    nodes = graph["nodes"]
    assert nodes[1]["eligible"] and not nodes[2]["eligible"]
    assert nodes[1]["target"] == "10.0.0.3" and nodes[1]["discoveries"] == [
        "Service version found"
    ]
    assert nodes[1]["hints"] == 1 and nodes[1]["solutions"] == 1
    release = graph["releases"][0]
    assert release["tier"] == "medium"
    assert release["next_action"] == "Inspect the configuration endpoint."
    assert release["monitor_observation"]["summary"] == "The service configuration was reached."
    assert nodes[2]["focus"] and not nodes[1]["focus"] and nodes[1]["assistance_focus"]
    assert (
        graph["monitor"]["checks"] == 1
        and graph["monitor"]["basis"] == "monitor checkpoints"
    )
    payload = json.dumps(graph)
    for private in ["UNRELEASED_HINT", "PRIVATE_SOLUTION", "PRIVATE_EXPECTED"]:
        assert private not in payload


def test_final_review_overrides_intermediate_completion(tmp_path):
    row, task, _ = fixture(tmp_path)
    row["criterion_results"] = [
        dict(
            id=c["id"],
            status="unverified",
            reason="Not supported by final review",
            evidence=[],
        )
        for c in task["rubric"]["criteria"]
    ]
    graph = build(tmp_path, {}, [row])["trials"][0]
    assert graph["nodes"][0]["state"] == "other" and not graph["nodes"][1]["eligible"]
    assert graph["monitor"]["basis"] == "final review"
    # Overall success never auto-completes a step lacking criterion evidence.
    task["challenge_plan"]["steps"][3]["criterion_ids"] = []
    save(tmp_path, "evaluation/manifest.json", dict(spec=dict(tasks=[task])))
    row["verified_success"] = True
    assert build(tmp_path, {}, [row])["trials"][0]["nodes"][3]["state"] == "other"


def test_planned_graph_and_missing_or_linked_artifacts(tmp_path):
    row, task, _ = fixture(tmp_path)
    planned = build(tmp_path, {}, [])["trials"][0]
    assert planned["trial_id"] is None and planned["counts"]["other"] == 4
    assert not planned["monitor"]["enabled"] and planned["releases"] == []
    path = tmp_path / "evaluation" / row["attempt_path"] / "progress-monitor.json"
    path.unlink()
    path.symlink_to(tmp_path / "evaluation/manifest.json")
    graph = build(tmp_path, {}, [row])["trials"][0]
    assert graph["counts"]["solved"] == 0
    assert build(tmp_path, {}, [])["trials"]


def test_exact_only_and_rubric_without_dependency_plan(tmp_path):
    task = dict(
        id="task",
        prompt="Read service.",
        rubric={"criteria": [dict(id="c1", requirement="Read result")]},
    )
    journal = dict(scenario_experiment=dict(evaluation_tasks=[task]))
    graph = build(tmp_path, journal, [dict(task_id="task", trial_id="trial-1")])[
        "trials"
    ][0]
    assert graph["edges"] == [] and graph["graph_source"] == "Task success criteria"
    del task["rubric"]
    graph = build(
        tmp_path,
        journal,
        [dict(task_id="task", trial_id="trial-1", verified_success=True)],
    )["trials"][0]
    assert graph["nodes"][0]["state"] == "solved"
