import json
from pathlib import Path

import yaml

from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import post_execution, run_artifacts, scenarios, samples, service
from cyber_agent_flow_orchestrator.config import load
from cyber_agent_flow_orchestrator.workspaces import Workspace, private_directory
from test_pve_auth import pve
from test_user_access import access, vm
from test_workflow import lab


def test_evidence_collection_uses_latest_participant_files_only(tmp_path):
    run = tmp_path / "run"
    for attempt_name, transcript in (("attempt-0001", "old transcript"),
                                     ("attempt-0002", "latest participant transcript")):
        session = run / "evaluation/trials/trial-000001" / attempt_name / "guest-output/runs/session"
        (session / "tool_calls").mkdir(parents=True)
        (session / "transcript.md").write_text(transcript)
        (session / "metadata.json").write_text(json.dumps({"available_tools": ["nmap"]}))
        (session / "tool_calls/001_nmap.json").write_text(json.dumps({
            "seq": 1, "tool": "nmap", "args": {"target": "10.0.0.2"},
            "result": "port 80 open", "exit_code": 0,
        }))
        (session.parents[2] / "evaluation.json").write_text(json.dumps({
            "judge": {"outcome": "partial"}
        }))
    private = run / "evaluation/trials/trial-000001/attempt-0002/reference-material.json"
    private.write_text('{"flag":"FLAG{private-answer}"}')

    payload, sources = post_execution.collect_participant_evidence(run)

    assert "latest participant transcript" in payload["transcript"]
    assert "old transcript" not in payload["transcript"]
    assert "FLAG{private-answer}" not in json.dumps(payload)
    assert all("reference-material" not in source["path"] for source in sources)
    assert payload["metadata"]["available_tools"] == ["nmap"]
    assert payload["tool_calls"][0]["tool"] == "nmap"


def test_create_assisted_freezes_lineage_and_paired_conditions(pve, lab, tmp_path):
    pve[0]["resources"]["operator@pve"] = [vm(9402), vm(9403)]
    user = access(pve)
    root = tmp_path / "owned-runs"
    workspace = Workspace(root, user.username)
    workspace.save_roles(dict(scenarioforge=9402, participant=9403, core=None), user)
    cfg, runtime, _, identity = load(lab[0])
    runtime["backend"].update(app_vmid=9402, participant_vmid=9403)
    source_run_id = "scenario-" + "a" * 32
    source = workspace.materialize(cfg, runtime, user, source_run_id)
    # Freeze catalogs inside the source input, matching normal experiment creation.
    saved = yaml.safe_load((source.parent / "runtime.yaml").read_text())
    for index, condition in enumerate(saved["conditions"]):
        catalog = source.parent / f"catalog-{index}.json"
        catalog.write_text('{"tools":[]}')
        condition["catalog"] = str(catalog)
        condition["guidance_files"] = []
        condition["tools"] = []
    (source.parent / "runtime.yaml").write_text(yaml.safe_dump(saved))
    cfg, runtime, _, identity = load(source)
    output = workspace.run_path(source_run_id)
    private_directory(output)
    private_directory(output / "inputs")
    captured = {"id": "b" * 64, "scenario": "Saved lab", "snapshot_token": "a" * 32,
                "snapshot_path": "/saved/scenario.xml", "sha256": "c" * 64, "repo": "/opt/scenarioforge"}
    ev.write_json(output / "workflow.json", dict(version=1, workflow=cfg, runtime=runtime,
        workflow_hash=identity, token=source_run_id, scenario_experiment=captured,
        status="completed_with_errors", phase="finished", stages={}, events=[],
        task_design=[{"id": "task", "family": "test", "split": "development"}]))
    manager = samples.SampleManager(runtime, root)
    try:
        controller = scenarios.ScenarioExperiments(cfg, runtime, root, manager)
        analysis = {"version": 1, "helper": "Use a narrow probe first, verify each result, and preserve command evidence.",
                    "analysis": "structured review", "analysis_sha256": "d" * 64,
                    "input_sha256": "e" * 64, "model": {"name": "fixture"},
                    "created_at": "2026-01-01T00:00:00+00:00"}
        result = controller.create_assisted(user, source_run_id, "f" * 32, analysis,
                                            helper_id="helper-" + "2" * 24)
        derived = workspace.run_path(result["run_id"])
        record = ev.read_json(derived / "workflow.json")
        derived_runtime = yaml.safe_load((workspace.path / "inputs" / result["run_id"] / "runtime.yaml").read_text())
        assert record["post_execution"]["source_run_id"] == source_run_id
        assert record["post_execution"]["helper_id"] == "helper-" + "2" * 24
        assert record["post_execution"]["source_workflow_hash"] == identity
        assert len(derived_runtime["conditions"]) == 2
        original, assisted = derived_runtime["conditions"]
        assert original["tools"] == assisted["tools"]
        assert assisted["id"].endswith("-generated-helper")
        assert assisted["guidance_files"][-1].endswith("generated-post-run-helper.md")
        assert record["post_execution"]["baseline_condition"] == original["id"]
        assert record["post_execution"]["assisted_condition"] == assisted["id"]
        assert (derived / "post-execution/generated-helper.md").read_text() == analysis["helper"]
        assert ev.read_json(derived / "post-execution/analysis.json")["analysis_sha256"] == "d" * 64
        assert run_artifacts.configuration(derived)["post_execution"]["source_run_id"] == source_run_id
        assert any(item["path"] == "post-execution/generated-helper.md"
                   for item in run_artifacts.files(derived))
        assert service.status(derived)["post_execution"]["source_run_id"] == source_run_id
    finally:
        manager.close()
