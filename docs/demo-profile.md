# Cyber Agent Flow Demo Summary

Both sample presets exercise **ScenarioForge XML/bundle import → topology preparation → CORE deployment → readiness → evaluation export → Cyber-agent-flow → scores and reproduction downloads**.

Select ScenarioForge, participant and CoreVM roles in Lab setup. ScenarioForge must have a working CORE connection, and the participant must be able to reach the generated lab network. The sample's XML, task template, tool conditions and trial limits are fixed and shown greyed out in New experiment. CAF model settings remain configurable.

## Model smoke test

**Scenario:** `demo-smoke.xml` / `demo-smoke.zip`. One deployed HTTP website, with a fresh service token generated for each run.

**Task / prompt template:**

> Fetch http://<deployed-host>/ and read the service token from its response body. Return only JSON with service_token set to the exact observed token.

**Tools:** `nmap`, `curl`, `python3`.

**Evaluation:** One trial, at most 6 turns and 120 seconds; 30-second tool timeout. Exact JSON is verified against the generated service token.

## Tools vs. added helper

**Scenario:** `demo-tools-vs-helper.xml` / `demo-tools-vs-helper.zip`. One deployed read-only website with two fresh flags on linked pages.

**Task / prompt template:**

> Explore http://<deployed-host>/ and follow its links to recover two flags. Return only JSON with a flags array in discovery order (first.html, then deeper/second.html).

| Condition | Tools |
| --- | --- |
| Baseline | `nmap`, `curl`, `python3` |
| Added helper | Baseline + `http_flag_walk` |

**Evaluation:** Three repetitions per condition (six trials), at most 12 turns and 120 seconds per trial; 30-second tool timeout. Both conditions receive the same deployed website and prompt. Exact JSON verifies both flags in discovery order.

## Captured metrics and inputs

Both presets collect readiness results, verified success/score, execution time, trial status, errors, and condition summaries. Available worker/model/tool telemetry is retained. These presets use exact-JSON verification; their final score is all-or-nothing rather than partial flag credit.

Results expose the actual prompt containing the resolved host address, model and execution settings, tool catalogs, deployed XML, evaluation package and reproduction ZIP. The full run bundle also preserves the original imported ZIP and preparation command. CAF tool scope is limited to the private host addresses exported by ScenarioForge.

The downloaded XML is a fixed source definition. The named sample preset automatically imports its payload, resolves topology, adds fresh tokens/flags and reviewed task definitions, and deploys it. Generic XML import alone does not apply the sample preset.

Deployment is retained after the run or a stop request. Historical participant-only samples remain readable; new sample creation uses ScenarioForge.
