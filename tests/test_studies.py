"""Studies preserve ownership, controlled design and constituent evidence."""

from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace
import io
import json
import zipfile
import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator.studies import Studies
from cyber_agent_flow_orchestrator.workspaces import Workspace
from cyber_agent_flow_orchestrator.samples import SampleRequestError
from cyber_agent_flow_orchestrator.access import AccessDenied


class Access:
    username = "alice"

    def current(self):
        return None

    def require_vms(self, ids):
        assert set(ids) == {101, 102}


def save(root, access, name, **changes):
    record = dict(
        workflow_hash=name + "-frozen",
        status="ready",
        scenario_experiment={"sha256": name + "-source"},
        task_design=[dict(id="inspect", family="configuration", split="test")],
        workflow={"reset_each_trial": True},
        runtime={
            "backend": {"app_vmid": 101, "participant_vmid": 102},
            "model": {"name": "participant"},
            "execution": {"wall_seconds": 120},
            "conditions": [
                {"id": "baseline", "tools": ["curl"]},
                {"id": "helper", "tools": ["curl", "helper"]},
            ],
        },
    )
    record.update(changes)
    path = Workspace(root, access.username).run_path(name)
    path.mkdir()
    ev.write_json(path / "workflow.json", record)
    return record


@pytest.fixture
def study(tmp_path):
    access = Access()
    dashboard = SimpleNamespace(root=tmp_path, samples=SimpleNamespace(jobs={}))
    manager = Studies(dashboard)
    for name in ("first", "second"):
        save(tmp_path, access, name)
    yield manager, access, dashboard
    manager.close()


def test_controlled_design_and_owner_isolation(study):
    manager, access, dashboard = study
    value = manager.create(access, ["first", "second"], "Held-out study")
    assert value["status"] == "ready" and len(value["members"]) == 2
    other = Access()
    other.username = "bob"
    assert manager.listing(other)["items"] == []
    with pytest.raises(SampleRequestError, match="not found"):
        manager.record(manager.folder(other, value["id"]))
    path = (
        Workspace(dashboard.root, access.username).run_path("second") / "workflow.json"
    )
    record = ev.read_json(path)
    record["runtime"]["model"]["name"] = "different"
    ev.write_json(path, record)
    with pytest.raises(SampleRequestError, match="share model"):
        manager.create(access, ["first", "second"], "Invalid")
    for ids in ([{}], ["first", "first"], []):
        with pytest.raises(SampleRequestError):
            manager.create(access, ids, "Invalid")
    with pytest.raises(AccessDenied):
        manager.create(access, ["../bob"], "Invalid")


def test_family_holdout_and_reset_requirement(study):
    manager, access, dashboard = study
    path = (
        Workspace(dashboard.root, access.username).run_path("second") / "workflow.json"
    )
    record = ev.read_json(path)
    record["task_design"][0]["split"] = "development"
    ev.write_json(path, record)
    with pytest.raises(SampleRequestError, match="family cannot cross"):
        manager.create(access, ["first", "second"], "Leaky holdout")
    record["workflow"]["reset_each_trial"] = False
    ev.write_json(path, record)
    with pytest.raises(SampleRequestError, match="per-trial"):
        manager.create(access, ["second"], "No reset")


def test_reference_requires_observed_completion_for_every_task(study, monkeypatch):
    from cyber_agent_flow_eval import reporting

    manager, access, dashboard = study
    root = Workspace(dashboard.root, access.username).run_path("first") / "evaluation"
    root.mkdir()
    ev.write_json(root / "manifest.json", {"spec": {"tasks": [{"id": "inspect"}]}})
    rows = [
        {"task_id": "inspect", "verified_success": False, "assistance_level": "none"}
    ]
    monkeypatch.setattr(
        reporting,
        "results",
        lambda path: dict(attempts=rows, spec_hash="reference-spec"),
    )
    with pytest.raises(SampleRequestError, match="successful completion"):
        manager.create(
            access,
            ["first"],
            "Reference required",
            reference_run_ids=["first"],
            require_references=True,
        )
    rows[0]["verified_success"] = True
    result = manager.create(
        access,
        ["first"],
        "Reference verified",
        reference_run_ids=["first"],
        require_references=True,
    )
    assert result["reference_coverage"] == {"covered": 1, "total": 1}
    with pytest.raises(SampleRequestError, match="Every scenario"):
        manager.create(
            access,
            ["first", "second"],
            "Missing reference",
            reference_run_ids=["first"],
            require_references=True,
        )
    rows[0]["assistance_level"] = "solution"
    with pytest.raises(SampleRequestError, match="answer-only assistance"):
        manager.create(access, ["first"], "Answer copied", reference_run_ids=["first"])


def test_sequential_execution_and_bundle_preserve_member_outputs(study, monkeypatch):
    manager, access, dashboard = study
    value = manager.create(access, ["first", "second"], "Study")
    launched = []

    def run_saved(access, name, token):
        launched.append(name)
        result = "executed-" + name
        record = save(dashboard.root, access, result, status="completed")
        future = Future()
        future.set_result(record)
        dashboard.samples.jobs[access.username] = future
        return {"run_id": result}

    dashboard.scenarios = SimpleNamespace(run_saved=run_saved)
    monkeypatch.setattr(manager, "summarize", lambda *args: {"comparisons": []})
    folder = manager.folder(access, value["id"])
    manager._run(access, folder)
    result = manager.record(folder)
    assert launched == ["first", "second"] and result["status"] == "completed"

    @contextmanager
    def artifact(access, run_id, kind):
        assert kind == "run-bundle"
        yield io.BytesIO(
            ("Evidence for " + run_id).encode()
        ), "application/zip", run_id + ".zip"

    dashboard.artifact = artifact
    with manager.artifact(access, value["id"], "study-bundle.zip") as (
        stream,
        mime,
        name,
    ):
        with zipfile.ZipFile(stream) as archive:
            assert json.loads(archive.read("study.json"))["status"] == "completed"
            assert (
                archive.read("experiments/executed-first.zip")
                == b"Evidence for executed-first"
            )
            assert (
                archive.read("experiments/executed-second.zip")
                == b"Evidence for executed-second"
            )


def test_changed_member_is_blocked_before_vm_launch(study):
    manager, access, dashboard = study
    value = manager.create(access, ["first"], "Frozen study")
    path = (
        Workspace(dashboard.root, access.username).run_path("first") / "workflow.json"
    )
    record = ev.read_json(path)
    record["workflow_hash"] = "changed"
    ev.write_json(path, record)
    folder = manager.folder(access, value["id"])
    manager._run(access, folder)
    assert manager.record(folder)["status"] == "failed"
    assert "configuration changed" in manager.record(folder)["message"]


def test_active_study_blocks_both_individual_launch_apis():
    from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
    from cyber_agent_flow_orchestrator.samples import SampleBusy

    dashboard = UserDashboard.__new__(UserDashboard)
    dashboard.studies = SimpleNamespace(active=lambda owner: True)
    access = Access()
    with pytest.raises(SampleBusy, match="study is running"):
        dashboard.run_sample(access, "smoke", "token")
    with pytest.raises(SampleBusy, match="study is running"):
        dashboard.experiment(access, "run", {})
