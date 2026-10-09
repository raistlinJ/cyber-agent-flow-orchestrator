"""Owner-scoped collections of saved experiments, executed sequentially."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
import json
import re
import threading
import time
import uuid
import tempfile
import zipfile
import shutil
from contextlib import contextmanager
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_eval.studies import summarize
from .workspaces import Workspace
from .samples import SampleRequestError, SampleBusy


def now():
    return datetime.now(timezone.utc).isoformat()


def export_report(report):
    from .run_report import Document, STYLE

    doc = Document()
    doc.heading("Multi-scenario experiment study", 1)
    doc.paragraph(
        "Rubric-based automated judgments; human judge calibration was not performed. Confidence intervals resample scenario clusters; repetitions are not independent scenarios."
    )
    doc.table(
        ["Planned", "Observed", "Evaluable", "Unverified", "Unstarted"],
        [
            [
                report.get(k)
                for k in (
                    "planned_trials",
                    "observed_trials",
                    "evaluable_trials",
                    "unverified_trials",
                    "unstarted_trials",
                )
            ]
        ],
    )
    height = 100 + 60 * len(report["comparisons"])
    axis = height - 35
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 {height}" role="img" aria-label="Paired success differences and 95 percent intervals">'
    ]
    for tick in (-1, -0.5, 0, 0.5, 1):
        x = 590 + 280 * tick
        svg.append(
            f'<line x1="{x}" y1="10" x2="{x}" y2="{axis}" stroke="{ "#999" if tick==0 else "#e4e9ee" }"/><text x="{x}" y="{axis+22}" text-anchor="middle" font-size="12">{tick:+g}</text>'
        )
    for i, row in enumerate(report["comparisons"]):
        delta = row["mean_success_difference"]
        y = 35 + i * 60
        label = row["condition"]
        for line in range(0, len(label), 26):
            svg.append(
                f'<text x="10" y="{y-12+(line//26)*16}" font-size="14">{escape(label[line:line+26])}</text>'
            )
        if delta is not None:
            x = 590 + 280 * delta
            svg.append(
                f'<rect x="{min(590,x)}" y="{y-15}" width="{abs(x-590)}" height="20" fill="#167d58"/>'
            )
            ci = row["confidence_interval_95"]
            if ci:
                svg.append(
                    f'<line x1="{590+280*ci[0]}" x2="{590+280*ci[1]}" y1="{y-5}" y2="{y-5}" stroke="#111" stroke-width="3"/>'
                )
        else:
            svg.append(f'<text x="600" y="{y}" font-size="13">Not evaluable</text>')
    svg.append("</svg>")
    graph = "".join(svg)
    doc.heading("Paired comparisons")
    doc.table(
        [
            "Condition",
            "Baseline",
            "Success difference",
            "95% interval",
            "Pairs",
            "Excluded pairs",
            "Scenario clusters",
        ],
        [
            [
                r["condition"],
                r["baseline"],
                r["mean_success_difference"],
                r["confidence_interval_95"],
                r["eligible_pairs"],
                r["excluded_pairs"],
                r["independent_scenarios"],
            ]
            for r in report["comparisons"]
        ],
    )
    doc.paragraph(
        "Bars show success-rate improvement over baseline; black lines show scenario-cluster bootstrap intervals. +0.5 means a 50 percentage point improvement. Intervals with few scenarios are exploratory; missing or invariant data have no estimable interval."
    )
    doc.html.append(graph)
    doc.markdown.append("![Paired success differences](study-charts.svg)")
    for row in report["by_family"]:
        doc.heading(row["split"] + " · " + row["family"], 3)
        doc.table(
            ["Condition", "Difference", "95% interval", "Scenario clusters"],
            [
                [
                    r["condition"],
                    r["mean_success_difference"],
                    r["confidence_interval_95"],
                    r["independent_scenarios"],
                ]
                for r in row["comparisons"]
            ],
        )
    doc.heading("Scenario reference completions")
    coverage = report.get("reference_coverage")
    if coverage:
        doc.table(
            ["Scenario definitions covered", "Total definitions"],
            [[coverage["covered"], coverage["total"]]],
        )
    else:
        doc.paragraph("No scenario reference coverage was recorded.")
    doc.paragraph(
        "Reference completions are automated judgments matched by saved scenario definition, not human-validated solvability proofs."
    )
    doc.table(
        ["Reference run", "Scenario definition SHA256", "Basis"],
        [
            [r["run_id"], r["source_xml_sha256"], r["basis"]]
            for r in report.get("reference_completions", [])
        ],
    )
    doc.heading("Included experiments")
    doc.table(
        ["Experiment", "Spec hash", "Planned", "Observed"],
        [
            [
                r["experiment_id"],
                r["spec_hash"],
                r["planned_trials"],
                r["observed_trials"],
            ]
            for r in report["runs"]
        ],
    )
    return {
        "study-summary.md": "\n\n".join(doc.markdown),
        "study-summary.html": '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'
        + STYLE
        + "</style></head><body><main>"
        + "".join(doc.html)
        + "</main></body></html>",
        "study-charts.svg": graph,
    }


class Studies:
    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="study")
        self.lock = threading.RLock()
        self.jobs = {}
        self.stopping = threading.Event()

    def close(self):
        self.stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def active(self, owner):
        with self.lock:
            return owner in self.jobs

    def folder(self, access, study_id):
        access.current()
        if not re.fullmatch("[0-9a-f]{32}", study_id):
            raise SampleRequestError("Invalid study ID")
        return (
            Workspace(self.dashboard.root, access.username).path / "studies" / study_id
        )

    def record(self, folder):
        try:
            return ev.read_json(folder / "study.json")
        except FileNotFoundError:
            raise SampleRequestError("Study not found") from None

    def listing(self, access):
        root = Workspace(self.dashboard.root, access.username).path / "studies"
        access.current()
        return dict(
            items=[
                self.record(p)
                for p in sorted(root.glob("*"), reverse=True)
                if p.is_dir() and not p.is_symlink() and (p / "study.json").exists()
            ]
        )

    def create(
        self,
        access,
        run_ids,
        name,
        baseline="baseline",
        reference_run_ids=None,
        require_references=False,
    ):
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 120
            or not isinstance(run_ids, list)
            or not 1 <= len(run_ids) <= 100
            or any(not isinstance(r, str) for r in run_ids)
            or len(set(run_ids)) != len(run_ids)
        ):
            raise SampleRequestError(
                "Supply a study name and 1–100 distinct experiment IDs"
            )
        if not isinstance(baseline, str) or not re.fullmatch(
            "[A-Za-z0-9][A-Za-z0-9_-]{0,79}", baseline
        ):
            raise SampleRequestError("Invalid baseline condition ID")
        workspace = Workspace(self.dashboard.root, access.username)
        access.current()
        members = []
        settings = None
        family_splits = {}
        for run_id in run_ids:
            if not isinstance(run_id, str):
                raise SampleRequestError("Invalid experiment ID")
            record = ev.read_json(workspace.run_path(run_id) / "workflow.json")
            if not record.get("scenario_experiment"):
                raise SampleRequestError("Studies require ScenarioForge experiments")
            access.require_vms(
                __import__(
                    "cyber_agent_flow_orchestrator.workspaces",
                    fromlist=["workflow_vmids"],
                ).workflow_vmids(record["workflow"], record["runtime"])
            )
            if baseline not in {c["id"] for c in record["runtime"]["conditions"]}:
                raise SampleRequestError(
                    "Baseline condition must exist in every experiment"
                )
            if not record["workflow"].get("reset_each_trial"):
                raise SampleRequestError(
                    "Study members must enable per-trial lab reset; create controlled experiments first"
                )
            runtime = record["runtime"]
            signature = dict(
                model=runtime["model"],
                judge=runtime.get("judge"),
                budgets={
                    k: runtime["execution"].get(k)
                    for k in (
                        "wall_seconds",
                        "max_turns",
                        "tool_timeout",
                        "context_window",
                        "provide_progressive_hints",
                        "max_tries_before_solution",
                        "auto_approve_dangerous",
                    )
                },
                conditions=[
                    {k: c.get(k) for k in ("id", "tools", "provide_progressive_hints")}
                    for c in runtime["conditions"]
                ],
            )
            if settings is not None and settings != signature:
                raise SampleRequestError(
                    "Study members must share model, judge, condition selections and execution budgets"
                )
            settings = signature
            for task in record.get("task_design", []):
                family = task["family"]
                split = task.get("split", "development")
                if family in family_splits and family_splits[family] != split:
                    raise SampleRequestError(
                        "A scenario family cannot cross development/validation/test splits"
                    )
                family_splits[family] = split
            members.append(dict(run_id=run_id, workflow_hash=record["workflow_hash"]))
        if type(require_references) is not bool or (
            reference_run_ids is not None
            and (
                not isinstance(reference_run_ids, list)
                or any(not isinstance(r, str) for r in reference_run_ids)
            )
        ):
            raise SampleRequestError("Invalid reference validation settings")
        references = []
        covered = set()
        for run_id in reference_run_ids or []:
            ref = ev.read_json(workspace.run_path(run_id) / "workflow.json")
            report = __import__(
                "cyber_agent_flow_eval.reporting", fromlist=["results"]
            ).results(workspace.run_path(run_id) / "evaluation")
            rows = report["attempts"]
            successful = {
                r["task_id"]
                for r in rows
                if r.get("verified_success") is True
                and (
                    r.get("assistance_level", "none") == "none"
                    or r.get("judge_execution_trace_reviewed")
                )
            }
            manifest = ev.read_json(
                workspace.run_path(run_id) / "evaluation/manifest.json"
            )
            if not successful or not all(
                t["id"] in successful for t in manifest["spec"]["tasks"]
            ):
                raise SampleRequestError(
                    "Reference runs must demonstrate successful completion of every task, without answer-only assistance"
                )
            key = ref.get("scenario_experiment", {}).get("sha256")
            if not key:
                raise SampleRequestError(
                    "Reference must be a saved ScenarioForge scenario experiment"
                )
            covered.add(key)
            references.append(
                dict(
                    run_id=run_id,
                    source_xml_sha256=key,
                    spec_hash=report["spec_hash"],
                    basis="automated reference completion; not human calibrated",
                )
            )
        source_keys = {
            ev.read_json(workspace.run_path(m["run_id"]) / "workflow.json")[
                "scenario_experiment"
            ]["sha256"]
            for m in members
        }
        if require_references and not source_keys.issubset(covered):
            raise SampleRequestError(
                "Every scenario definition needs a successful reference run before this study can be created"
            )
        study_id = uuid.uuid4().hex
        folder = self.folder(access, study_id)
        folder.mkdir(parents=True, mode=0o700)
        value = dict(
            id=study_id,
            name=name.strip(),
            baseline=baseline,
            members=members,
            references=references,
            require_references=require_references,
            reference_coverage=dict(
                covered=len(source_keys & covered), total=len(source_keys)
            ),
            status="ready",
            created_at=now(),
            message="Ready to run saved experiments sequentially",
        )
        ev.write_json(folder / "study.json", value)
        return value

    def summarize(self, access, study_id):
        folder = self.folder(access, study_id)
        value = self.record(folder)
        workspace = Workspace(self.dashboard.root, access.username)
        outputs = [
            workspace.run_path(row.get("executed_run_id", row["run_id"])) / "evaluation"
            for row in value["members"]
        ]
        if not all((p / "manifest.json").is_file() for p in outputs):
            raise SampleRequestError(
                "Every study member needs evaluation outputs before summarizing"
            )
        report = summarize(outputs, value["baseline"])
        report.update(
            reference_completions=value.get("references", []),
            reference_coverage=value.get("reference_coverage"),
            reference_validation="automated completion under saved criteria; not proof of judge accuracy",
        )
        ev.write_json(folder / "study-summary.json", report)
        for name, content in export_report(report).items():
            (folder / name).write_text(content)
        return report

    @contextmanager
    def artifact(self, access, study_id, name):
        folder = self.folder(access, study_id)
        record = self.record(folder)
        allowed = {
            "study-summary.md": "text/markdown",
            "study-summary.html": "text/html",
            "study-charts.svg": "image/svg+xml",
            "study-summary.json": "application/json",
            "study-bundle.zip": "application/zip",
        }
        if name not in allowed:
            raise SampleRequestError("Unknown study artifact")
        if name != "study-bundle.zip":
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise SampleRequestError("Study report is not available")
            with path.open("rb") as stream:
                yield stream, allowed[name], name
            return
        if record["status"] not in {"completed", "failed", "cancelled"}:
            raise SampleRequestError(
                "Wait for the study to finish before downloading its bundle"
            )
        with tempfile.TemporaryFile() as stream:
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for path in sorted(folder.glob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, path.name)
                for member in record["members"]:
                    run_id = member.get("executed_run_id", member["run_id"])
                    with self.dashboard.artifact(access, run_id, "run-bundle") as (
                        source,
                        _,
                        filename,
                    ):
                        with archive.open(
                            "experiments/" + run_id + ".zip", "w", force_zip64=True
                        ) as target:
                            shutil.copyfileobj(source, target)
            stream.seek(0)
            yield stream, allowed[name], name

    def start(self, access, study_id):
        folder = self.folder(access, study_id)
        with self.lock:
            if self.active(access.username) or self.stopping.is_set():
                raise SampleBusy("A study is already running")
            value = self.record(folder)
            if value["status"] in {"running", "stopping"}:
                raise SampleBusy("This study is running")
            (folder / "stop.json").unlink(missing_ok=True)
            value.update(status="running", started_at=now(), message="Starting study")
            ev.write_json(folder / "study.json", value)
            self.jobs[access.username] = self.pool.submit(self._run, access, folder)
        return value

    def stop(self, access, study_id):
        folder = self.folder(access, study_id)
        value = self.record(folder)
        if value["status"] != "running":
            raise SampleRequestError("Study is not running")
        ev.write_json(folder / "stop.json", {"at": now()})
        return dict(status="stopping")

    def _run(self, access, folder):
        value = self.record(folder)
        workspace = Workspace(self.dashboard.root, access.username)
        try:
            for index, member in enumerate(value["members"]):
                if self.stopping.is_set() or (folder / "stop.json").exists():
                    raise InterruptedError("Study stopped")
                access.current()
                record = ev.read_json(
                    workspace.run_path(member["run_id"]) / "workflow.json"
                )
                if record["workflow_hash"] != member["workflow_hash"]:
                    raise ValueError("Saved experiment configuration changed")
                value.update(
                    message=f'Experiment {index+1}/{len(value["members"])}: {member["run_id"]}'
                )
                ev.write_json(folder / "study.json", value)
                launched = self.dashboard.scenarios.run_saved(
                    access, member["run_id"], uuid.uuid4().hex
                )
                future = self.dashboard.samples.jobs.get(access.username)
                member["executed_run_id"] = launched["run_id"]
                ev.write_json(folder / "study.json", value)
                while True:
                    current = ev.read_json(
                        workspace.run_path(launched["run_id"]) / "workflow.json"
                    )
                    if current["status"] not in {
                        "queued",
                        "preparing",
                        "evaluating",
                        "stopping",
                    } and (future is None or future.done()):
                        break
                    if self.stopping.is_set() or (folder / "stop.json").exists():
                        if current["status"] != "stopping":
                            self.dashboard.scenarios.stop(access, launched["run_id"])
                    time.sleep(0.5)
                if (folder / "stop.json").exists() or self.stopping.is_set():
                    raise InterruptedError("Study stopped")
                if current["status"] not in {"completed", "completed_with_errors"}:
                    raise ValueError(
                        "Study experiment "
                        + launched["run_id"]
                        + " "
                        + current["status"]
                    )
            self.summarize(access, value["id"])
            value.update(
                status="completed",
                ended_at=now(),
                message="Study completed; comparison report available",
            )
        except Exception as exc:
            value.update(
                status="cancelled" if isinstance(exc, InterruptedError) else "failed",
                ended_at=now(),
                message=str(exc),
            )
        finally:
            ev.write_json(folder / "study.json", value)
            with self.lock:
                self.jobs.pop(access.username, None)
