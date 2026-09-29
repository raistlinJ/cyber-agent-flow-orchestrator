# Saved run inputs and ScenarioForge exports

Open **Results → Task and run configuration** to inspect the captured inputs for a
run. This section is above the result metrics and shows:

- Task IDs and exact task prompts.
- Model, provider, endpoint, repetitions, order seed, and execution budgets.
- Selected tools, frozen tool catalogs, and additional guidance per condition.
- Exact initial system prompts when collected worker checkpoints are available.
- Engine/backend settings, schedule, source hashes, dependency versions, and the
  saved workflow configuration.
- ScenarioForge information, readiness evidence, XML, and reproduction fidelity.

These values come from the run's saved evaluation manifest, not the application's
current defaults. Before evaluation begins, the section uses the saved study or
experiment settings and identifies that source. Missing historical evidence is
reported as unavailable.

The same data is included in the Results JSON as `run_configuration`.
Per-attempt checkpoint details, when saved, are included under
`evaluation.attempts[].flag_progress`.

## Downloads

| Download | Contents |
| --- | --- |
| Run inputs and evidence ZIP | Saved workflow/study/runtime, catalogs, guidance, evaluation manifest, attempts, collected model/tool evidence and logs, scenario files, and the re-import ZIP |
| Scenario XML | Complete XML captured in the evaluation suite |
| ScenarioForge re-import ZIP | Importer-compatible reproduction package with saved XML and captured generated artifacts |
| ScenarioForge evaluation ZIP | Saved evaluation package with tasks, private verifiers, attack graph, readiness and manifest |
| Individual saved files | Each available captured file, including trial inputs and collected evidence |

The XML preview is loaded only when expanded and displays at most 128 KiB of
characters. Its download contains the entire file. ZIP/XML responses stream
through the HTTPS server. Downloads remain scoped to the authenticated run owner;
there is no public sharing endpoint. Run/evaluation ZIP creation waits until the
run is idle so it cannot mix files from an active attempt.

Saved scenario XML and raw evidence retain their original contents. They may
contain connection details, discovered flags, and private verifier answers.

## Re-importing a scenario

For a new ScenarioForge workflow, orchestration automatically creates a
`reproduction/scenarioforge-reproduction.zip` after retrieving and validating
the evaluation suite. It includes the exact scenario XML and attempts to collect
generated artifact directories referenced by `FlowState` and `PlanPreview`.

Capture checks the ScenarioForge VM and, when configured, the CoreVM through QEMU
Guest Agent. It reads recognized ScenarioForge output directories and the
`/tmp/vulns/flag_generators_runs` and
`/tmp/vulns/flag_node_generators_runs` directories. Symlinked files are excluded.
The backend transfer limit bounds capture. Missing, inaccessible, unsupported, or
oversized sources are listed in the package manifest; they are not silently
declared portable.

Download **ScenarioForge re-import ZIP**, import it through ScenarioForge's
scenario import control, then materialize and deploy. The manifest reports
`portable-artifacts`, `partial-artifacts`, or `xml-replay` fidelity. This
restores a scenario definition and any bundled generated files, not VM disks or
container images. External images, executables, credentials, and missing sources
still require the destination environment.

For an older run without a captured reproduction archive, the download creates
an XML replay bundle from its saved XML. It identifies referenced artifact
sources as unbundled; opening Results never probes a VM to reconstruct history.

If an existing reproduction archive is already available in the ScenarioForge
VM, a workflow can specify it explicitly in either scenario mode:

```yaml
scenarioforge:
  mode: reuse_export
  archive: /exports/evaluation.zip
  reproduction_archive: /exports/scenarioforge-reproduction.zip
```

Orchestration validates the reproduction manifest, file hashes, and correspondence
with the evaluated XML before freezing that archive. A mismatch stops the run.
Resume checks the frozen archive for changes.

## Bundled demo samples

New bundled samples deploy their fixed ScenarioForge XML on CORE and evaluate the exported tasks. Results includes the original imported ZIP, exact deployed XML, preparation command, tool catalogs and captured reproduction package. Historical participant-only runs retain their original inputs and explicitly report that ScenarioForge was not used.

The run download contains captured inputs and evidence. Paths inside historical
configuration files refer to the original host/guest and may need rebasing;
external tools and environments are not automatically recreated by downloading
the ZIP.
