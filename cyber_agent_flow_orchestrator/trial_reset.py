"""Redeploy the frozen lab before every trial; reject changed objectives."""

from pathlib import Path
import uuid
from cyber_agent_flow_eval import integration as ev


def callback(wf, cfg, runtime, package):
    from .workflow import execute_command, export_marker
    from cyber_agent_flow_eval.runner import PreparationError

    baseline_tasks, baseline = ev.load_suite(package)
    baseline_xml = (Path(package) / "evaluator/scenario.xml").read_bytes()
    if cfg["scenarioforge"]["mode"] != "execute":
        raise ValueError(
            "Per-trial redeployment requires an executable ScenarioForge XML"
        )

    def reset(directory, trial):
        wf.notify(
            "Resetting CORE scenario and checking readiness for " + trial["trial_id"]
        )
        sf = dict(cfg["scenarioforge"])
        token = uuid.uuid4().hex
        session_id = wf.journal.get(
            "active_reset_session_id", baseline["scenario"]["core_session_id"]
        )
        delete = dict(
            id="reset-session",
            vmid=runtime["backend"]["app_vmid"],
            user=sf["user"],
            cwd=sf["repo"],
            timeout_seconds=sf["timeout_seconds"],
            argv=[
                sf["python"],
                "-u",
                "-m",
                "scenarioforge.evaluation.reset",
                "--xml",
                sf["xml"],
                "--scenario",
                sf["scenario"],
                "--session",
                str(session_id),
            ],
            **(
                {"environment_file": sf["environment_file"]}
                if "environment_file" in sf
                else {}
            ),
        )
        wf.command(
            "reset-delete-" + trial["trial_id"] + "-" + directory.name, delete, True
        )
        # A dedicated frozen copy prevents regeneration/replanning the source
        # experiment and preserves resolved artifact references.
        from .scenarios import guest

        remote = guest(runtime["backend"])
        data = dict(repo=sf["repo"], token=token, user=sf["user"], source=sf["xml"])
        frozen = remote.call(runtime["backend"]["app_vmid"], "reset-start", **data)[
            "path"
        ]
        wf.agent.put(runtime["backend"]["app_vmid"], frozen, baseline_xml)
        remote.call(
            runtime["backend"]["app_vmid"],
            "reset-ready",
            **data,
            sha256=baseline["scenario"]["xml_sha256"],
        )
        sf["xml"] = frozen
        sf["suite_id"] = "reset-" + token
        command = execute_command(sf, runtime["backend"], token)
        marker = export_marker(
            wf.command(
                "reset-" + trial["trial_id"] + "-" + directory.name, command, True
            )
        )
        target = directory / "reset-suite"
        ev.ProxmoxBackend(
            runtime["backend"], runtime["engine"], agent=wf.agent
        ).fetch_suite(marker["archive"], target)
        tasks, snapshot = ev.load_suite(target)
        wf.journal["active_reset_session_id"] = snapshot["scenario"]["core_session_id"]
        wf.save()
        ev.require_ready(snapshot, cfg["max_readiness_age_seconds"])
        # Session identity may change; objectives, addresses and private answers
        # must not. Otherwise this is a different experiment, never a fair pair.
        keys = ("id", "prompt", "verifier", "rubric", "verification_mode")
        comparable = lambda rows: [{key: row.get(key) for key in keys} for row in rows]
        if (
            snapshot["scenario"]["xml_sha256"] != baseline["scenario"]["xml_sha256"]
            or snapshot["scenario"]["core_host"] != baseline["scenario"]["core_host"]
            or (snapshot.get("producer") or {}).get("source_sha256")
            != (baseline.get("producer") or {}).get("source_sha256")
            or comparable(tasks) != comparable(baseline_tasks)
        ):
            raise PreparationError(
                "Redeployment changed frozen XML, prompts or success criteria; trial refused"
            )
        return dict(
            passed=True,
            method="ScenarioForge frozen XML redeployment",
            core_session_id=snapshot["scenario"]["core_session_id"],
            readiness=snapshot["readiness"],
            package_hash=snapshot["package_hash"],
            logs="reset-suite/evaluator/readiness.json",
        )

    return reset
