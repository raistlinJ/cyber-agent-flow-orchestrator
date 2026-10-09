# Rubric experiments and multi-scenario studies

ScenarioForge, cyber-agent-flow and the evaluator remain independent products.
ScenarioForge exports rubrics using its own stdlib-only validator; it never imports
CAF or the orchestrator. CAF's standalone CLI and WebUI retain their normal tools
and sessions. Only evaluator-managed tasks add the reporting scaffold. CAF tool
records now include stable evidence IDs and output hashes, including across session
continuation.

## Define a challenge

In New → Evaluation → Tasks & trial settings, load the scenario's task draft and
choose Edit loaded tasks. Tasks with no generated flags receive an editable Flow
rubric draft. Review the intended objective using the reference-document dropdown;
topology alone does not establish the objective. Refine generic draft requirements
before using them in a study.

Choose Exact checks, Judge review, or Both. Judge and Both require an enabled judge
under Evaluation → Judge LLM. Judge-only tasks need no flag or expected final value.
The configuration editor provides requirement, acceptable evidence, essential,
weight and private reference fields for every criterion. Set a specific scenario
family and development/validation/test split. Entire families must remain in one
split; renaming near-identical templates does not create a meaningful holdout.

Rubric version 1 example:

```json
{
  "version": 1,
  "criteria": [
    {
      "id": "retrieve",
      "requirement": "Locate and read the configuration used by the target service.",
      "evidence": "Successful tool output connecting the service to the configuration and showing its contents.",
      "essential": true,
      "weight": 1,
      "private_reference": "Optional facilitator reference; never put it in participant instructions."
    }
  ]
}
```

The authoritative validator adds defaults (essential=true, weight=1), rejects
unknown fields, duplicate IDs, invalid weights, missing evidence descriptions and
rubrics with no essential criterion. The JSON Schema is published in ScenarioForge
and the evaluator; their executable contracts are tested to be identical.

ScenarioForge saves authored definitions in `FlowState.evaluation_tasks` or accepts
them through the standalone CLI's `--evaluation-tasks` JSON file. Rubric evaluation
packages use format version 4. Public task prompts contain only requirements and
an evidence-report scaffold. Full rubrics/private references remain in private
`evaluator/task-metadata.json` and `evaluator/verifiers.json`. XML/reproduction
bundles preserve the definitions. Authors and bundle recipients can access these
private evaluator documents; participant workers cannot.

## Evidence and scoring

The judge receives the rubric and an inventory of collected execution evidence.
It uses read-only evidence tools and must cite byte ranges it actually read.
Tool output can establish completion without a saved file or prescribed directory.
Agent claims and the final answer alone cannot establish successful actions. Full
CAF output artifacts are available when a tool-record preview is truncated.
`evidence-manifest.json` records the collected evidence files, sizes and hashes.

The judge labels each criterion satisfied, unmet or unverified, with evidence and a
reason. Host code computes the result:

* Success: every essential criterion is satisfied. Optional criteria contribute
  to the score but cannot prevent success.
* Partial: at least one criterion is satisfied, at least one essential criterion
  is unmet, and essential evidence is sufficiently verified to decide.
* Fail: no criteria are satisfied and essential criteria are observed unmet.
* Unverified: essential evidence is missing/inconclusive or judging failed.

Score is the weight of satisfied criteria divided by total criterion weight.
It is supported completion credit, not model confidence. A high score cannot
compensate for an unmet essential requirement. In Both mode, exact correctness is
also required. Exact-only tasks retain their output-based checks.

Task outcome, execution status and assistance level are separate fields. A budget
exhausted trial can have observed partial progress; a VM/reset failure remains an
execution error with an unverified task outcome. Guidance supplied up front,
released hints and disclosed solutions are recorded separately. Assistance does
not rewrite the rubric. Results, JSONL/CSV and Markdown/HTML reports include the
criterion findings and evidence references. A workflow marked Completed means
execution finished, not that every task succeeded.

Explicit condition labels are withheld from rubric judges. Tool names, guidance
and execution content can still reveal the intervention; this is limited blinding,
not a guarantee that the judge cannot infer the condition. Judge accuracy has not
been human calibrated. Describe findings as rubric-based automated judgments.

## Controlled conditions and resets

Experiment controls select configured tool catalogs, permitted tools, optional
participant guidance and hints per condition. Host paths cannot be supplied by the
browser. Baseline and HTTP helper are built in; server-configured catalogs can use
existing artifact-generation/collection stages. A server catalog whose ID matches
a built-in is listed with a configured- prefix so the two sources stay distinct. Collected guide text and authored
condition guidance are frozen together. Optional extra reset/readiness hooks can
be selected when the server template configures them. All conditions share the participant
model, reporting scaffold and turn/time/context budgets. A frozen seed randomizes
condition order within each task/repetition pair.

Reset the lab before each trial is enabled by default in New. The host removes only
the experiment's tracked CORE session, confirms its removal, then redeploys a copy
of the frozen XML beside its source (preserving relative artifact references).
It exports and validates fresh readiness and compares XML identity, task prompts,
private criteria, CORE host and ScenarioForge source identity against the initial
suite. A changed objective or answer blocks execution. Each trial retains its reset
log, readiness export and reset timing; the active session is journaled for retries.
Per-trial checks also reject changed CAF source/dependencies.

This is a redeployment reset, not a whole-VM snapshot restore. Persisted volumes or
external services outside ScenarioForge's managed deployment need an explicit
reset/readiness hook appropriate to that scenario. Inspect reset evidence and
establish that your scenario's persistent state is actually reset before making
controlled-comparison claims. A readiness pass is not proof that every challenge
is solvable; retain a known successful reference execution for each scenario.

## Studies and uncertainty

The Studies page collects saved scenario experiments. Creation checks baseline
presence, equal model/judge/budget/condition settings and per-trial reset policy.
Execution runs members sequentially with the same access checks as individual
experiments. Stop finishes the active stage/trial and collects results. Individual
launches are blocked while the account's study is active. Optional reference-run
selection can require an observed successful completion for every saved scenario
definition before creating a study. Unassisted passes qualify; assisted passes need
an execution trace reviewed by the judge, so answer-only copies do not qualify.
This checks automated reference completion, not human judge calibration.

Study reports enforce family holdouts, preserve member configuration hashes, and
show planned, observed, evaluable, unverified and unstarted counts. Combining
outputs from different engine/evaluator sources or dependencies is refused. Primary paired
success differences use matched task/repetition pairs and equal scenario weights.
Supported completion-score and unassisted-success differences are also recorded.
Percentile bootstrap intervals resample scenario clusters (2,000 draws with a
saved seed), not individual repetitions. Orchestrated reruns cluster by the saved scenario
definition hash, rather than changing deployment IDs. Very small samples are exploratory; fewer
than two clusters or no observed variation produce no bootstrap interval.
Missing counterparts/unverified outcomes are counted as excluded pairs, never
silently converted into failure. Inspect evaluation coverage alongside success
rates, including successes divided by all planned trials.

Download the standalone HTML graph report, Markdown + SVG, JSON, or complete study
ZIP. The ZIP includes study configuration/reports and the full constituent run
bundles (experiment inputs, scenario reproduction bundles and execution evidence).
These exports contain private evaluator references and may contain lab secrets;
prepare a suitable release dataset rather than assuming every file is public.

Optional USD rates are frozen with the experiment. Participant and judge costs use
provider-reported tokens and operator-supplied rates; unavailable prices/usage stay
unknown. Recorded generation cost is an operator-supplied experiment-level value,
not an inferred bill. Generation stage duration, reset time, worker time and judge
time are separate. Nested tool/provider usage may be unavailable.

## Standalone evaluator studies

A study YAML lists experiment specs. Each must configure guest `before_trial`
reset/readiness hooks or an explicit host `reset` command (argv, cwd,
timeout_seconds). Host reset scripts are hashed and rechecked before execution.
Scripts must return zero only after the intended environment is reset and ready.

```yaml
version: 1
id: helpers-heldout
baseline: baseline
bootstrap_seed: 42
experiments:
  - scenarios/configuration.yaml
  - scenarios/service-discovery.yaml
```

Run `cyber-agent-flow-eval study --config study.yaml --output study-runs`.
Use `--resume` for unchanged studies. To summarize existing evaluations, use
`cyber-agent-flow-eval study --evaluations run-a run-b --output study-report`.
No ScenarioForge application import is needed in the evaluator, and none of these
features require the orchestrator for standalone use.

For a task using Both, rubric completion credit is retained even if the final answer fails the exact check. Verified success still requires both checks to pass; a criterion score of 1 does not override an incorrect JSON answer. The rubric scaffold preserves the task's required final response format and uses the recorded execution logs for evidence when the task requires JSON only.
