# ScenarioForge scenarios in the WebUI

Both bundled sample presets deploy their fixed ScenarioForge XML and evaluate the resulting lab. To evaluate your own ScenarioForge scenario, use **Experiments → New experiment → ScenarioForge XML or bundle**.

1. In **Lab setup**, select and save the ScenarioForge and participant VM roles.
2. Load scenarios from the ScenarioForge VM. Search by file path or scenario name.
3. Select a named scenario with a saved, resolved Flow chain. Unresolved XML is listed but cannot be selected for evaluation.
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

The **Cyber-agent-flow** tab contains the model controls. Pull from VM reads the application configuration; saving changes requires application-maintenance access. Creating an experiment saves those changes and freezes its settings.

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
