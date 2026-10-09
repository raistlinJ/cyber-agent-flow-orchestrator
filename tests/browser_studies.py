"""Real HTTPS/PVE study creation and report download in Chrome."""

from pathlib import Path
import shutil
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace
from cyber_agent_flow_orchestrator.studies import export_report
from cyber_agent_flow_eval.studies import compare
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth
from test_user_access import Probe, vm


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "examples", root / "examples"
        )
        with pve_server(root) as pve:
            pve[0]["resources"]["operator@pve"] = [vm(9402), vm(9403)]
            dashboard = UserDashboard(
                root / "examples/01-reuse-export.yaml",
                root / "runs",
                2,
                lambda b, a: Probe(b, a, []),
            )
            try:
                folder = Workspace(dashboard.root, "operator@pve").run_path(
                    "inspect-lab"
                )
                folder.mkdir()
                record = dict(
                    workflow_hash="a" * 64,
                    status="ready",
                    scenario_experiment={
                        "scenario": "Configuration lab",
                        "sha256": "b" * 64,
                    },
                    task_design=[
                        dict(id="inspect", family="configuration", split="test")
                    ],
                    workflow={"id": "inspect-lab", "reset_each_trial": True},
                    runtime={
                        "backend": {"app_vmid": 9402, "participant_vmid": 9403},
                        "model": {"provider": "ollama_direct", "name": "demo"},
                        "execution": {"wall_seconds": 120},
                        "conditions": [
                            {"id": "baseline", "tools": ["curl"]},
                            {"id": "helper", "tools": ["curl", "helper"]},
                        ],
                    },
                    stages={},
                )
                ev.write_json(folder / "workflow.json", record)
                with (
                    secure_server(
                        dashboard, root / "web", auth=make_auth(pve)
                    ) as server,
                    sync_playwright() as playwright,
                ):
                    browser = playwright.chromium.launch(
                        channel="chrome", headless=True
                    )
                    page = browser.new_page(
                        ignore_https_errors=True, viewport=dict(width=1440, height=1080)
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(server["origin"])
                    page.get_by_label("Username").fill("operator@pve")
                    page.get_by_label("Password", exact=True).fill(PASSWORD)
                    page.get_by_role("button", name="Sign in", exact=True).click()
                    page.locator("[data-route=studies]").click()
                    expect(page.locator("#study-members input")).to_have_count(
                        1, timeout=30000
                    )
                    page.locator("#study-members input").check()
                    page.locator("#study-name").fill("Held-out configuration study")
                    page.locator("#create-study").click()
                    expect(page.locator("#studies-list h3")).to_have_text(
                        "Held-out configuration study", timeout=30000
                    )
                    expect(page.locator("#studies-list")).to_contain_text("ready")
                    items = page.evaluate("studyRequest('list',{})")["items"]
                    study = items[0]
                    response = page.request.get(
                        server["origin"]
                        + "/api/studies/"
                        + study["id"]
                        + "/artifact?name=study-summary.html"
                    )
                    assert (
                        response.status == 404
                        and "Authentication" not in response.text()
                    )
                    saved = dashboard.studies.folder(
                        type(
                            "A",
                            (),
                            {"username": "operator@pve", "current": lambda self: None},
                        )(),
                        study["id"],
                    )
                    rows = [
                        dict(
                            experiment_id="study",
                            scenario_id=scenario,
                            pair_id="inspect:0",
                            condition_id=condition,
                            verified_success=passed,
                            split="test",
                            family="configuration",
                        )
                        for scenario in ("a", "b")
                        for condition, passed in [
                            ("baseline", False),
                            ("helper", scenario == "a"),
                        ]
                    ]
                    comparisons = compare(rows)
                    report = dict(
                        planned_trials=4,
                        observed_trials=4,
                        evaluable_trials=4,
                        unverified_trials=0,
                        unstarted_trials=0,
                        comparisons=comparisons,
                        by_family=[
                            dict(
                                split="test",
                                family="configuration",
                                comparisons=comparisons,
                            )
                        ],
                        runs=[
                            dict(
                                experiment_id="inspect-lab",
                                spec_hash="a" * 64,
                                planned_trials=4,
                                observed_trials=4,
                            )
                        ],
                    )
                    for name, content in export_report(report).items():
                        (saved / name).write_text(content)
                    with page.expect_popup() as popup:
                        page.get_by_role("link", name="Open formatted report").click()
                    expect(popup.value.locator("h1")).to_have_text(
                        "Multi-scenario experiment study"
                    )
                    expect(popup.value.locator('svg[role="img"]')).to_be_visible()
                    popup.value.screenshot(
                        path="/tmp/caf-study-report.png", full_page=True
                    )
                    popup.value.close()
                    page.screenshot(path="/tmp/caf-studies.png")
                    page.set_viewport_size(dict(width=390, height=844))
                    assert page.evaluate(
                        "document.documentElement.scrollWidth<=innerWidth"
                    )
                    assert not errors, errors
                    browser.close()
            finally:
                dashboard.close()
    print(
        "PASS: study creation, selected membership, report link, truthful unavailable response, mobile layout"
    )


if __name__ == "__main__":
    main()
