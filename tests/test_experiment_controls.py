import json
from pathlib import Path
import pytest
from cyber_agent_flow_orchestrator.experiment_controls import validate, apply
from cyber_agent_flow_orchestrator.evaluation_tasks import validate_tasks
from cyber_agent_flow_orchestrator.samples import SampleRequestError
from cyber_agent_flow_orchestrator import trial_reset

RUBRIC = {
    "version": 1,
    "criteria": [
        dict(
            id="read",
            requirement="Read the configuration.",
            evidence="Successful tool output showing its contents.",
        )
    ],
}


def test_judge_task_needs_no_flag_or_exact_answer():
    task = dict(
        id="read-config",
        family="configuration",
        prompt="Find and inspect the configuration.",
        required_checks=["services"],
        verification_mode="judge",
        rubric=RUBRIC,
    )
    assert validate_tasks([task])[0] == task
    with pytest.raises(SampleRequestError, match="without an exact"):
        validate_tasks([dict(task, verifier={"type": "json_equals", "expected": {}})])


def test_conditions_select_configured_catalogs_and_freeze_guidance(tmp_path):
    runtime = {"conditions": []}
    cfg = {}
    value = dict(
        order_seed=19,
        reset_each_trial=True,
        conditions=[
            dict(
                id="baseline",
                source="baseline",
                tools=["curl"],
                provide_progressive_hints=False,
            ),
            dict(
                id="guided",
                source="http-helper",
                tools=["curl", "http_flag_walk"],
                guidance="Inspect the service.",
                provide_progressive_hints=True,
            ),
        ],
    )
    apply(value, runtime, cfg, tmp_path, {"conditions": []})
    assert cfg["reset_each_trial"] and runtime["order_seed"] == 19
    assert runtime["conditions"][1]["provide_progressive_hints"]
    assert (
        Path(runtime["conditions"][1]["guidance_files"][0]).read_text()
        == "Inspect the service."
    )
    value["conditions"][0]["source"] = "../../private"
    with pytest.raises(SampleRequestError, match="configured"):
        validate(value, {"conditions": []})


@pytest.mark.parametrize("changed", [False, True])
def test_lab_reset_checks_frozen_objectives_and_tracks_owned_session(
    tmp_path, monkeypatch, changed
):
    from cyber_agent_flow_orchestrator import scenarios
    from types import SimpleNamespace
    import hashlib

    original = b'<Scenarios><Scenario name="Lab"/></Scenarios>'
    package = tmp_path / "suite"
    (package / "evaluator").mkdir(parents=True)
    (package / "evaluator/scenario.xml").write_bytes(original)
    tasks = [
        {
            "id": "read",
            "prompt": "Inspect service",
            "verifier": {"type": "contains_all", "expected": ["observed"]},
        }
    ]
    baseline = {
        "package_hash": "original",
        "scenario": {
            "xml_sha256": hashlib.sha256(original).hexdigest(),
            "core_host": "core",
            "core_session_id": 1,
        },
    }

    def load(path):
        if Path(path) == package:
            return tasks, baseline
        newer = dict(
            baseline,
            scenario=dict(baseline["scenario"], core_session_id=2),
            readiness={"ok": True},
        )
        if changed:
            newer["scenario"]["xml_sha256"] = "changed"
        return tasks, newer

    monkeypatch.setattr(trial_reset.ev, "load_suite", load)
    monkeypatch.setattr(trial_reset.ev, "require_ready", lambda snapshot, age: None)
    monkeypatch.setattr(
        trial_reset.ev,
        "ProxmoxBackend",
        lambda *a, **k: SimpleNamespace(fetch_suite=lambda archive, path: path.mkdir()),
    )
    calls = []

    class Remote:
        def call(self, vmid, op, **data):
            calls.append(op)
            return {"path": "/opt/sf/outputs/source/.caf-reset-test.xml"}

    monkeypatch.setattr(scenarios, "guest", lambda backend: Remote())

    class Workflow:
        journal = {"token": "run"}
        agent = SimpleNamespace(
            put=lambda vmid, path, data: calls.append(("put", path, data))
        )

        def save(self):
            pass

        def notify(self, message):
            pass

        def command(self, stage, command, retry):
            calls.append(command["argv"])
            return "EVALUATION_PACKAGE_JSON: " + json.dumps(
                dict(
                    state="complete", readiness_passed=True, archive="/opt/sf/reset.zip"
                )
            )

    wf = Workflow()
    cfg = {
        "scenarioforge": dict(
            mode="execute",
            repo="/opt/sf",
            xml="/opt/sf/outputs/source/scenario.xml",
            user="sf",
            python="/opt/sf/python",
            scenario="Lab",
            output_root="/opt/sf/eval",
            timeout_seconds=300,
            split="test",
        ),
        "max_readiness_age_seconds": 3600,
    }
    runtime = {"backend": {"app_vmid": 9402}, "engine": {}}
    reset = trial_reset.callback(wf, cfg, runtime, package)
    directory = tmp_path / "attempt-1"
    directory.mkdir()
    if changed:
        from cyber_agent_flow_eval.runner import PreparationError

        with pytest.raises(PreparationError, match="changed"):
            reset(directory, {"trial_id": "trial-1"})
    else:
        assert reset(directory, {"trial_id": "trial-1"})["passed"]
    assert calls[0][-2:] == ["--session", "1"]
    assert wf.journal["active_reset_session_id"] == 2
    assert "reset-ready" in calls


def test_remote_guidance_and_authored_guidance_are_frozen_together(tmp_path):
    from types import SimpleNamespace
    from cyber_agent_flow_orchestrator.workflow import Workflow

    defaults = {
        "conditions": [
            {
                "id": "baseline",
                "catalog": "placeholder.json",
                "tools": ["remote_tool"],
                "guidance_files": ["placeholder.md"],
            }
        ]
    }
    source = {
        "collect": {
            "baseline": {
                "catalog": "/generated/catalog.json",
                "guidance_files": ["/generated/guide.md"],
            }
        },
        "artifacts": [],
    }
    runtime = {"conditions": [], "backend": {"participant_vmid": 101}}
    cfg = {}
    value = {
        "conditions": [
            dict(
                id="guided",
                source="configured-baseline",
                tools=["remote_tool"],
                guidance="Author guidance",
            )
        ],
        "reset_each_trial": True,
    }
    apply(value, runtime, cfg, tmp_path / "author", defaults, source)
    authored = runtime["conditions"][0]["guidance_files"][0]
    assert "placeholder.md" not in runtime["conditions"][0]["guidance_files"]
    agent = SimpleNamespace(
        get=lambda vm, path: (
            b'{"tools":[]}' if path.endswith(".json") else b"Generated guidance"
        )
    )
    output = tmp_path / "run"
    output.mkdir()
    journal = {"stages": {}}
    wf = Workflow(output, journal, agent=agent, progress=None)
    wf.freeze_runtime(
        runtime, {authored: "Author guidance"}, dict(cfg, id="frozen"), "a" * 64
    )
    import yaml

    frozen = yaml.safe_load((output / "runtime.yaml").read_text())
    assert [Path(p).read_text() for p in frozen["conditions"][0]["guidance_files"]] == [
        "Generated guidance",
        "Author guidance",
    ]
