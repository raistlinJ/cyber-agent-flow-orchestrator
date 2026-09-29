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

Custom scenarios can set repetitions, maximum turns, trial seconds, tool timeout and context window in New. Their baseline tools remain fixed; task prompts/verifiers come from ScenarioForge's export. Sample budgets and conditions are fixed to preserve the experiment design.

### New experiment tabs

The enlarged modal separates **Experiment** (type and demo packages), **ScenarioForge** (fixed sample XML or custom scenario import/selection), **Cyber-agent-flow** (effective runtime and model editor), and **Evaluation** (limits and conditions). Settings persist when switching tabs. Create stays visible, and validation opens the tab containing a field that needs correction. Arrow keys, Home and End navigate the tab bar.
