# ScenarioForge test bundles

Import one of these ZIPs through Orchestrator **New → ScenarioForge → Upload XML or bundle**. Each contains a resolved ScenarioForge XML, Compose assets, reviewed task, evidence rubric, exact verifier, optional hints, and participant/facilitator guides.

The Evaluation tab can load each bundle without manual task authoring. Every ZIP carries the prompt, private success verifier, version 1 evidence rubric, required readiness checks, and ordered progressive hints in both `FlowState.evaluation_tasks` and the root `evaluation-tasks.json`. The XML is authoritative after import; the standalone JSON makes the task portable and provides an editable fallback.

| ZIP | Task |
| --- | --- |
| 01-http-service-token.zip | Fetch a plain-text service token. |
| 02-linked-web-flags.zip | Follow published links to recover two flags. |
| 03-json-manifest-token.zip | Read a manifest and fetch its token resource. |
| 04-file-download-path-traversal.zip | One CWE-22 vulnerability; recover a private proof value. |
| 05-path-traversal-generated-flag.zip | One CWE-22 vulnerability and one materialized flag-generator output. |

**Verification:** These bundles use **Both**: the final JSON must match the expected answer and the Judge LLM must verify the rubric against execution logs. Enable **Evaluation → Judge LLM**, or select **Edit loaded tasks → Exact checks** for a quick deterministic check. Each ZIP includes `evaluation-rubric.json`, `experiment-profile.json`, and a readable `experiment-summary.md`. Private answers and rubric references are excluded from the participant briefing.

Suggested limits: one repetition, six turns, 120 seconds. Allow the lab target address shown in the embedded task. The topology and participant attachment are resolved for the current Fusion lab. Another lab may require updating the CORE/participant configuration and regenerating the preview and task target address. CORE needs the python:3.12-alpine image, or access to pull it.

The fifth bundle includes generator source, input configuration, and frozen generated output. Import replays that output; it does not regenerate the flag or install a generator catalog.

Current validation: native ScenarioForge reproduction import, version 4 evaluation export, evaluator import/readiness checks, private-answer separation, and host scoring with simulated VM/model responses. See [validation.json](validation.json) for the current hashes and scope. These checks do **not** establish a new live deployment or LLM pass.

All five earlier versions passed on the local Fusion lab on October 2. [validation-2026-10-02-original.json](validation-2026-10-02-original.json) retains their original checksums and run IDs; `orchestrator-test-results.json` retains that test history.

All five are **development** scenarios. The three HTTP-discovery tasks share a family; the two traversal tasks share another. Keep related families together when assigning held-out splits. Baseline runs check functionality; comparison conditions are needed to measure artifact improvement. The judge has not been human calibrated.
