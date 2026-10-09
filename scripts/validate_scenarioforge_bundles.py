"""Validate saved samples without a VM or model; write an explicitly scoped report."""

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "scenarioforge"))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def payloads(archive):
    manifest = json.loads(archive.read("scenarioforge-reproduction.json"))
    return {
        source["archive_path"].rstrip("/")
        + "/"
        + f["path"]: digest(
            archive.read(source["archive_path"].rstrip("/") + "/" + f["path"])
        )
        for source in manifest["artifact_sources"]
        for f in source.get("files", [])
        if f["path"]
        not in {
            "README.md",
            "demo-profile.json",
            "participant-guide.md",
            "facilitator-guide.md",
            "evaluation-rubric-template.json",
        }
    }


def main():
    from cyber_agent_flow_orchestrator.scenario_upload import validate
    from cyber_agent_flow_eval.scenarioforge import load_suite, require_ready
    from cyber_agent_flow_eval.runner import verify
    from scenarioforge.evaluation.export import export_package
    from scenarioforge.evaluation.rubric import validate_rubric

    importer = runpy.run_path(
        str(ROOT.parent / "scenarioforge/webapp/reproduction_bundle.py")
    )["import_scenario_file"]
    rows = []
    paths = sorted((ROOT / "ScenarioForge-Bundles").glob("*.zip"))
    paths += [
        ROOT / "cyber_agent_flow_orchestrator/static" / f"demo-{name}.zip"
        for name in ("smoke", "tools-vs-helper")
    ]
    with tempfile.TemporaryDirectory() as scratch:
        for path in paths:
            data = path.read_bytes()
            assert validate(data) == "reproduction-bundle"
            imported = importer(str(path), str(Path(scratch) / path.stem))
            assert (
                imported.bundled_artifact_sources == imported.total_artifact_sources > 0
            )
            xml = Path(imported.xml_path)
            scene = ET.parse(xml).find("Scenario")
            state = json.loads(scene.find(".//FlowState").text)
            with zipfile.ZipFile(path) as archive:
                old = subprocess.check_output(
                    ["git", "show", "49a5dc2:" + path.relative_to(ROOT).as_posix()],
                    cwd=ROOT,
                )
                with zipfile.ZipFile(io.BytesIO(old)) as original:
                    assert payloads(archive) == payloads(
                        original
                    ), "Website / generated payload changed"
                    if "evaluation_tasks" in state:
                        previous_tree = ET.fromstring(original.read("scenario.xml"))
                        current_tree = ET.fromstring(archive.read("scenario.xml"))
                        previous_flow = previous_tree.find(".//FlowState")
                        current_flow = current_tree.find(".//FlowState")
                        previous_state = json.loads(previous_flow.text)
                        current_state = json.loads(current_flow.text)
                        previous_state.pop("evaluation_tasks")
                        current_state.pop("evaluation_tasks")
                        assert (
                            previous_state == current_state
                        ), "Frozen Flow deployment changed"
                        previous_flow.text = current_flow.text = ""
                        assert ET.tostring(previous_tree) == ET.tostring(
                            current_tree
                        ), "Frozen topology changed"
                if "evaluation_task_template" in state:
                    task = state["evaluation_task_template"]
                    assert (
                        task["version"] == 2
                        and task["verification_mode"] == "both-if-judge-enabled"
                    )
                    validate_rubric(task["rubric"])
                    assert task == json.loads(
                        archive.read("evaluation-task-template.json")
                    )
                    checks = [
                        "orchestrator upload",
                        "native reproduction import / payload integrity",
                        "version 2 task template / version 1 rubric",
                        "unchanged website payload",
                    ]
                else:
                    tasks = state["evaluation_tasks"]
                    assert tasks == json.loads(archive.read("evaluation-tasks.json"))
                    scene_name = scene.get("name")
                    readiness = dict(
                        status="complete",
                        ok=True,
                        overall="pass",
                        scenario=scene_name,
                        session_id=1,
                        session_confirmed=True,
                        core_host="fixture-core",
                        xml_sha256=digest(xml.read_bytes()),
                        checked_at=datetime.now(timezone.utc).isoformat(),
                        checks=[
                            dict(key=k, status="pass")
                            for k in ("containers", "services", "ports")
                        ],
                    )
                    output = Path(scratch) / (path.stem + "-evaluation")
                    manifest = export_package(
                        xml_path=xml,
                        graph=dict(
                            schema_version=2,
                            scenario=scene_name,
                            nodes=[dict(id="web", ipv4="192.0.2.10")],
                            edges=[],
                        ),
                        output=output,
                        suite_id="sample-contract",
                        definitions=tasks,
                        readiness=readiness,
                        session_id=1,
                    )
                    assert manifest["version"] == 4
                    loaded, snapshot = load_suite(output)
                    require_ready(snapshot, 3600)
                    public = (output / "participant/tasks.json").read_text()
                    for task in loaded:
                        expected = task["verifier"]["expected"]
                        values = (
                            expected["flags"]
                            if "flags" in expected
                            else list(expected.values())
                        )
                        assert all(value not in public for value in values)
                        assert (
                            verify(json.dumps(expected), task["verifier"])["passed"]
                            is True
                        )
                        assert verify("{}", task["verifier"])["passed"] is False
                    checks = [
                        "orchestrator upload",
                        "native reproduction import / payload integrity",
                        "ScenarioForge v4 export / evaluator import",
                        "simulated readiness contract",
                        "private-answer separation",
                        "exact-check positive / negative cases",
                        "unchanged topology payload / frozen generator output",
                    ]
            rows.append(
                dict(
                    bundle=str(path.relative_to(ROOT)),
                    sha256=digest(data),
                    status="pass",
                    checks=checks,
                )
            )
    report = dict(
        checked_at=datetime.now(timezone.utc).isoformat(),
        scope="Native import/export and static contracts; no VM deployment or live model calls",
        live_vm_rerun=False,
        live_llm_rerun=False,
        scoring_test="cyber-agent-flow-eval/tests/test_sample_bundles.py: simulated worker/judge success, partial, wrong final JSON, and exact-only paths",
        historical_live_validation="validation-2026-10-02-original.json",
        bundles=rows,
    )
    (ROOT / "ScenarioForge-Bundles/validation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(f"{len(rows)} sample bundles passed; report written")


if __name__ == "__main__":
    main()
