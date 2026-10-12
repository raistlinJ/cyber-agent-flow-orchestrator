import json

from cyber_agent_flow_eval import integration as ev

from cyber_agent_flow_orchestrator import helper_library
from cyber_agent_flow_orchestrator.workspaces import Workspace, private_directory


def source_run(workspace, run_id="scenario-" + "a" * 32):
    output = workspace.run_path(run_id)
    private_directory(output)
    ev.write_json(output / "workflow.json", {
        "workflow": {"id": "saved-flow"},
        "workflow_hash": "b" * 64,
        "scenario_experiment": {
            "id": "c" * 64,
            "scenario": "Saved training lab",
            "sha256": "d" * 64,
            "snapshot_token": "e" * 32,
        },
    })
    return run_id


def analysis():
    return {
        "helper": "Start with a narrow service probe. Preserve evidence after each verified step.",
        "analysis_sha256": "f" * 64,
        "input_sha256": "1" * 64,
        "created_at": "2026-10-12T12:00:00+00:00",
        "model": {"provider": "openai", "name": "analyst", "url": "https://model.example/v1",
                  "api_key": "must-not-persist"},
    }


def test_generated_helpers_are_private_idempotent_and_reviewable(tmp_path):
    root = tmp_path / "runs"
    alice = Workspace(root, "alice@pve")
    run_id = source_run(alice)

    first = helper_library.save(alice, run_id, analysis())
    second = helper_library.save(alice, run_id, analysis())

    assert first["id"] == second["id"]
    assert first["content"] == analysis()["helper"]
    assert first["source_scenario"]["scenario"] == "Saved training lab"
    assert first["model"] == {"provider": "openai", "name": "analyst",
                              "url": "https://model.example/v1"}
    assert "api_key" not in json.dumps(first)
    listed = helper_library.listing(alice)
    assert [item["id"] for item in listed] == [first["id"]]
    assert "content" not in listed[0]
    assert helper_library.listing(Workspace(root, "bob@pve")) == []

    reviewed = helper_library.set_reviewed(alice, first["id"], True)
    assert reviewed["reviewed"] is True and reviewed["reviewed_at"]
    assert helper_library.read(alice, first["id"])["content"] == analysis()["helper"]


def test_invalid_generated_helper_directories_are_not_listed(tmp_path):
    workspace = Workspace(tmp_path / "runs", "alice@pve")
    library = workspace.path / "generated-helpers"
    private_directory(library)
    (library / ("helper-" + "a" * 24)).symlink_to(tmp_path)
    (library / "unrelated").mkdir()

    assert helper_library.listing(workspace) == []
