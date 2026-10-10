# ScenarioForge scenarios in the WebUI

Both bundled sample presets deploy their fixed ScenarioForge XML and evaluate the resulting lab. To evaluate your own ScenarioForge scenario, use **Experiments → New experiment → ScenarioForge XML or bundle**.

1. In **Lab setup**, select and save the ScenarioForge and participant VM roles.
2. Load scenarios from the ScenarioForge VM. Search by file path or scenario name.
3. Select a named scenario with a saved, resolved Flow chain. Unresolved XML is listed but cannot be selected for evaluation. Saved sequences are recognized from either `FlowState.chain` or `FlowState.chain_ids`, including XML saved by older Preview clients.
4. Review the allowed and excluded target IP addresses/CIDRs, model settings, and trial limits.
5. Create the experiment. This saves a separate copy of the selected XML and freezes the run settings; it does not deploy yet.
6. Choose **Deploy and run**. The existing ScenarioForge CLI workflow deploys the saved XML, exports tasks and verifiers, checks readiness, and runs CAF evaluation.

This initial option uses one repetition with baseline tools `nmap`, `curl`, and `python3`. It clears template trial-reset commands because those may belong to a different scenario. The XML's CORE connection is used for deployment; choosing a CoreVM role does not rewrite that connection. Stop requests take effect at stage/trial boundaries and leave the deployed scenario in place.

The saved XML is hash-checked before launch. Changes to the original selected file do not change the saved experiment. Run again creates a separate result using the same saved scenario and settings.

## Where XML is found

By default the selector searches these directories on the selected ScenarioForge VM:

- `<scenarioforge repo>/uploads`
- `<scenarioforge repo>/outputs`
- `/tmp/scenarioforge-eval-out/webui-xml`

It also checks the server workflow's configured `scenarioforge.xml`, if present. Configure additional or alternative search roots in the server workflow:

```yaml
monitoring:
  scenarioforge_xml_roots:
    - /opt/scenarioforge/uploads
    - /tmp/scenarioforge-eval-out/webui-xml
    - /srv/demo-scenarios
```

Listing is bounded and skips symbolic links. Search narrows returned scenarios; it does not accept arbitrary file paths from the browser. Access uses the signed-in user's authorized VM scope.

## Generating a scenario first

ScenarioForge-Eval already supports deterministic `.spec.yaml` generation and AI prompt generation. For example:

```yaml
name: ai-demo-01
iterations: 1
seed: 12345
ai:
  prompt: "two routers, three docker hosts, ssh service, and two flag node generators"
  enabled: true
  timeout_s: 480
  retries: 1
```

Run this through ScenarioForge-Eval on the ScenarioForge machine using the configured CORE connection and model:

```bash
uv run scenarioforge-eval demo.spec.yaml --sf-path /opt/scenarioforge \
  --execute --reproduction-mode bundle
```

This command generates and executes the scenario; it is not a generation-only command. The evaluator preserves `scenarioforge-webui.xml`, a convenient XML copy under its output `webui-xml` directory, and a reproduction ZIP. Ensure that output directory is in the selector's search roots. Resolve/save the Flow chain before selecting it for CAF evaluation.

Prompt-driven generation is not reproducible from seed alone; preserve its resulting XML and reproduction package. Generation is currently a ScenarioForge-Eval operation, not a button in the orchestration WebUI.

## Results and sharing

Results show the exported task prompts, tool settings, model, and collected metrics. Downloads include the captured run bundle, complete scenario XML, and ScenarioForge reproduction ZIP. The reproduction manifest reports any unavailable generated artifacts; see [saved run inputs](run-inputs.md).

## Upload from New experiment

Choose a local XML or ScenarioForge reproduction ZIP and click **Send to ScenarioForge**. Upload uses the selected VM and signed-in user’s access, then invokes ScenarioForge’s own importer. ZIP artifacts are restored and the imported XML appears in the scenario selector. Import does not deploy. The limit is 32 MiB uploaded / 128 MiB expanded. Experiment-results ZIPs are not scenario import packages.

The imported files are stored beneath `<scenarioforge repo>/uploads/caf-upload-<id>/`. Ready scenarios can be selected immediately. For unresolved scenarios, open the imported XML in ScenarioForge, configure the CORE connection, resolve/save the Flow chain, then reload the list. The original uploaded source is preserved in `inputs/uploaded-source.xml` or `inputs/uploaded-source.zip` in the experiment download, alongside the XML used for the run.

### Fixed scenario samples

Choose **Model smoke test** or **Tools vs. added helper** in New experiment. Both presets import their fixed XML/ZIP, prepare a deterministic topology and fresh task secrets using ScenarioForge, deploy to CORE, check readiness, export the evaluation suite, then run CAF. The smoke preset reads a real service token; the helper preset compares baseline tools with the HTTP helper across three paired repetitions.

ScenarioForge, participant and CoreVM roles must all be selected. The ScenarioForge CORE connection must be configured and the participant must have a route to the deployed network. Scenario and evaluation fields are locked for samples; CAF model settings remain configurable.

The XML/ZIP downloads are fixed source packages. Generic import lists their unresolved definition; the named sample preset performs its preparation automatically. The run's final XML, exact prompt, fresh verifier and reproduction artifacts are preserved in Results. Deployment remains in place after execution.

See [demo profile](demo-profile.md) for tasks, tools and metrics. Rebuild packages with `python scripts/build_demo_scenarios.py`.

### CAF and evaluation configuration

XML defines the ScenarioForge lab. CAF's checkout, Python executable, model and default execution settings come from the server runtime YAML, with saved model preferences for the selected participant VM. New experiment shows these effective settings explicitly.

The **Cyber-agent-flow** tab contains model controls that are editable immediately and prefilled from saved experiment defaults. **Pull from VM** optionally replaces the displayed values with the application configuration. **Apply settings** saves changes and requires application-maintenance access. Creating an experiment captures the saved settings without another VM write.

The **Evaluation** tab shows each sample’s locked task ID, prompt template, success criteria and required readiness checks. Custom scenarios can use their saved tasks or define an experiment-specific list. **Load scenario tasks** previews the selected XML’s definitions; **Edit loaded tasks** copies them into the editor. Add/remove tasks or import a JSON array (1–32 tasks, up to 64 KiB). Each task has an ID, family, prompt, success criteria and required checks. Advanced settings preserve split and discovery/fact declarations.

Success criteria support exact JSON equality (`json_equals`), required output strings (`contains_all`), or scenario flag node IDs (`flag_nodes`). Flag-node tasks may omit the prompt to use ScenarioForge’s generated prompt. Expected answers are evaluator-only; they are not added to the participant prompt. Enter real scenario targets in custom prompts; the editor does not interpolate addresses. ScenarioForge checks graph references, discovery facts and flag disclosure against the deployed scenario during export.

Task overrides are embedded in a private snapshot of the selected XML. The source scenario is unchanged. Definitions are also saved as `inputs/evaluation-tasks.json`, retained on reruns and included in the run bundle; the reproduction bundle contains the evaluated XML with those tasks. Choosing the scenario source sends no override, preserving ScenarioForge’s embedded definitions or default flag-collection behavior.

Custom scenarios can set repetitions, maximum turns, trial seconds, tool timeout and context window below the tasks. Their baseline tools remain fixed. Sample tasks, budgets and conditions are locked to preserve the experiment design.

### New experiment tabs

The enlarged modal separates **Experiment** (type), **ScenarioForge** (selected sample XML/bundle downloads or custom scenario import/selection), **Cyber-agent-flow** (effective runtime and model editor), and **Evaluation** (tasks, prompts, success criteria, readiness checks, limits and conditions). Settings persist when switching tabs. Create stays visible, and validation opens the tab containing a field that needs correction. Arrow keys, Home and End navigate the tab bar.

### Optional progressive hints

**New → Evaluation → Provide progressive hints** defaults to off and is editable for both fixed samples. It is saved in the runtime as `execution.provide_progressive_hints`; reruns retain it. Baseline and helper conditions share the same policy.

With hints enabled, the evaluator can release at most three hints per trial after two agent turns without observed progress, or after an incorrect final answer. A correct final answer stops normally. Assistance consumes the existing turn/time budget. Observed progress means a newly observed declared fact in bounded successful tool output; without fact declarations, it means a new nonempty successful tool output. This is a stall heuristic, not proof of what the model knows.

Both demos prepare three ordered hints in the saved scenario XML. Custom task definitions can include `"progressive_hints": ["First pointer", "More specific guidance"]` in their advanced JSON. Use reviewed participant/facilitator-guide excerpts here; entire guide documents are **not** automatically sent to the agent. ScenarioForge exports these hints only in private `evaluator/task-metadata.json`. Discovery tasks can also use their existing `discoverable_facts`, `evidence`, and prerequisite declarations: the evaluator offers an artifact pointer, evidence guidance, then an explicit fact value. It skips already observed or supplied facts and filters literal verifier answers from assistance. Enabling hints without a usable hint/fact source fails the trial with an explicit error.

The participant receives only released hints, never the private plan or verifier. Results show unassisted/assisted successes, hints released and facts revealed; Progress includes timestamped hint events. Each trial's `assistance.json` records policy version, text, source, trigger, turn, elapsed time, and observed/revealed fact IDs. JSON results and the full run ZIP retain this audit. “Unassisted success” means success without a released hint; it is not a counterfactual estimate of what an assisted trial would have achieved alone.

Deploy the matching orchestrator, evaluator, ScenarioForge, and participant CAF changes before enabling this setting. An older CAF engine remains usable with hints off; enabled execution checks for the between-turn callback and reports an actionable update error if missing.


### Reference documents while writing a prompt

Select a saved scenario in **New → ScenarioForge**. On **Evaluation → Tasks &
trial settings**, use the **Open scenario reference** dropdown beside the task
editor to open the attack graph, participant guide or facilitator guide. Allow
popups for the orchestrator site, or use the fallback link if a popup is blocked.

References reuse saved ScenarioForge exports beside the selected XML and existing
documents in uploaded scenario bundles. Missing documents use ScenarioForge's
`attack-graph` and `guides` CLI exports from a copy of the selected XML. The graph
is visual and offers JSON and, when available, DOT downloads; guides are formatted
and offer HTML or Markdown downloads. Exports do not deploy the scenario or
change your experiment draft. The participant guide helps define the task, while
the facilitator guide includes solutions. Opening a reference does not include
it in the agent's prompt or hints.

Reference documents persist under `outputs/caf-reference-previews/` in the
ScenarioForge checkout, keyed by the selected XML's content, path and scenario.
Opening or reloading a popup reuses the disk cache, including after a service
restart. Concurrent opens share an export lock. If both guides are missing, one
export produces both; an existing guide is never regenerated while exporting the
other. Changing the XML requires reloading the scenario list and selecting its
new revision. Generating a missing guide requires Node.js in the ScenarioForge
APP VM and a ScenarioForge version with these CLI phases. Popup loading/errors
are independent of dashboard refresh.


### Progressive hints

**Provide progressive hints** enables assistance when a task supplies authored
`progressive_hints`, eligible saved Flow hints, or `discoverable_facts`. It does
not invent hints or convert facilitator solutions into hints. ScenarioForge can
inherit public saved Flow hints for flag-collection tasks without an explicit
hint plan. Answer-bearing hints and unresolved templates are excluded.

For custom tasks, use **Progressive hints (one per line)** on Evaluation. Imported
hint plans remain editable. Clearing an existing plan explicitly leaves it empty;
facts remain separate in Advanced settings. Hints containing a known explicit
verifier answer are rejected before creation.

With assistance enabled but no usable guidance, a trial runs unassisted rather
than failing. Results record `progressive_hints_available`, the unavailable
reason, released hints/facts, and assisted versus unassisted success. Existing
hints and facts still use the same release policy and verifier-answer protection.

**Turns without progress before a hint** is editable beside the toggle. It
defaults to 2 and must be smaller than **Maximum agent turns**. The saved value
applies to every condition and is preserved for reruns and reports.



The Evaluation tab includes **Max tries before solution**, editable for samples
and custom scenarios (default 6, range 1–1,000). With progressive hints enabled,
a try is an agent turn without new observed progress; observing new evidence or
new successful tool output resets the count. At the limit, the evaluator provides
the **current challenge's facilitator walkthrough and exact answer/flag**.
ScenarioForge exports each challenge's guide section separately into private
`challenge_solutions` metadata; unreleased solutions are never uploaded to CAF.
Observed flags advance to the next unsolved challenge and reset its try count.
For older task packages without guide sections, the fallback uses the reviewed
task procedure/hints and exact verifier answer.

There are up to three ordinary hints per trial and at most one full solution per
challenge. The same policy applies to every condition and is saved for reruns
and in the run summary. Turn and time budgets still apply; a smaller trial budget
may end before the limit is reached. Results distinguish **unassisted**,
**hint-assisted**, and **solution-assisted** passes, and record `solution_provided`,
`solutions_released`, the configured limit and the per-challenge release audit
in `assistance.json`. Incorrect final answers before the limit receive neutral
retry feedback when no intermediate hint is due; this feedback is audited
separately and counts as assistance.


### Judge LLM

Configure the judge on **New → Evaluation → Judge LLM**. It is optional and off
by default unless the server's experiment defaults explicitly enable it. Exact
answer and flag checks can determine success without a judge. Enable **Use an
LLM judge agent** for an additional review of the execution evidence. By default
it uses the participant's provider, endpoint and model; turn off **Use the
participant's provider, endpoint and model** to choose a separate
OpenAI-compatible, Ollama or LiteLLM judge.
Judge turns (default 6), total review time (120 seconds) and output tokens per
call (2,048) are independent of the participant's budgets and saved for reruns.
Existing experiments retain their saved configuration.

The judge runs on the **orchestrator host**, using bounded read-only tools to
inspect collected result, message, tool-event, model-call and assistance files.
It also reads CAF's native transcript, per-tool JSON logs and saved text outputs
under `guest-output/runs/<run_id>/`. When execution logs are present, a verdict
must cite an actual log read; native tool records take priority over summaries.
The review checks tool arguments, observed output, exit codes and errors. It can
page through full saved outputs when a tool record contains only a preview.
Missing execution logs are clearly flagged as limited evidence in Results and
the formatted report. `judge_execution_trace_reviewed`, `judge_evidence_files`
and `judge_evidence_warning` record what was available and read.
It never runs commands or probes live VM state. When the judge is enabled, a
trial passes only when both the judge and the deterministic success criteria
pass. A malformed, unavailable
or timed-out judge produces `judge_error`, with success unverified; it never
silently falls back to passing with the deterministic checker alone.

Results show the verdict and reason, judge time, request count and reported token
usage. `judge.json` in each attempt records the judge prompt, evidence reads,
responses, settings and usage. The formatted experiment report includes judge
reviews. Assistance categories still distinguish answer disclosure from
independent task completion.

The endpoint must be reachable from the host. API credentials live in the
orchestrator process environment, not the participant VM. An administrator can
configure `judge.model.api_key_env` in the host runtime YAML; the WebUI reuses
those credentials only for that configured endpoint and cannot select arbitrary
host secret variables. The guest API key is never copied to the host.

Attack graph and guide buttons appear only on the **Evaluation** tab. References
are exported from the selected saved XML, with a private copy preserving relative
artifact paths. Update ScenarioForge on the APP VM to support custom evaluation
target chains with flag sequencing disabled. Restart the orchestrator after
updating its code so the popup page and API routes are active.

### Rubric tasks and controlled studies

See [Rubric experiments and multi-scenario studies](rubric-experiments.md) for
judge-only tasks without flags, structured criteria, evidence citations, per-trial
lab resets, condition editing, study collections and publication exports.
