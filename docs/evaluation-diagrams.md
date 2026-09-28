# Architecture and evaluation diagrams

[Architecture PNG](../scenarioforge_cyber-agent-flow.png) ·
[Evaluation flow PNG](../scenarioforge_cyber-agent-flow-eval.png)

The architecture figure shows where each component runs. The evaluation figure
expands the trial loop and the boundary between participant inputs and private
host-side scoring. Both describe the current Proxmox backend.

## Responsibilities

- **Orchestrator, on the host:** workflow YAML, VM roles and access, saved-scenario
  execution or export reuse, configured preparation commands, workflow journal,
  progress and results interfaces.
- **Evaluator, on the host:** imported tasks, conditions, seeded trial ordering,
  repetitions, budgets, participant input staging, private verifiers and datasets.
- **CAF, in participant-vm:** shared engine, selected tools/guidance, model requests
  and scoped lab actions. A thin evaluation worker invokes the engine per trial.
- **ScenarioForge, in app-vm:** scenario deployment and export of tasks, starting
  facts, private answers and readiness evidence. CORE runs the scenario network.

Host-to-guest arrows represent QEMU Guest Agent operations, not an IP network
between guests. App-vm and participant-vm need no direct network link. Participant
lab traffic uses HITL; model connectivity must be available separately. Private
answers and full attack graphs are not staged to the participant.

## Reading the evaluation comparison

The illustrative schedule is **2 tasks × 2 conditions × 3 repetitions = 12 trials**;
it is not the default size of the bundled samples. A trial starts with one supplied
task prompt, then the engine's model/tool loop runs within the configured turn and
wall-time limits. Prompts can come from task YAML or an imported ScenarioForge suite.

Compare a controlled baseline tool set against that same set plus selected artifacts.
Hold tasks, starting facts, model and budgets constant, and configure reset/readiness
hooks to restore comparable lab state. A new worker does not reset CORE. Scenario
reset, facilitator hints and artifact repair are not automatic evaluation features.
Generation/testing commands are optional, explicitly configured preparation stages;
executable artifacts and their dependencies must be installed and archived separately.
Catalog/guidance snapshots do not freeze those executables or dependencies.

Private scoring can record verified success, flag progress and time to first flag.
Human review decides whether an artifact helps and whether to revise it between
studies. The WebUI currently provides included samples and result inspection;
full configured workflows use the CLI.

## Commands represented in the architecture figure

Run from the orchestrator checkout on the Proxmox host. First edit an example's
VM IDs, guest paths, model endpoint, network scope and scenario/export selection.
Here `workflow.yaml` means your edited copy and `runs/study` is a new output path:

```bash
O=.venv/bin/cyber-agent-flow-orchestrator
$O plan workflow.yaml
$O run workflow.yaml --output runs/study
$O results runs/study
$O export runs/study --destination exports/study
```

These are administrator CLI commands. For PVE-account authorization, saved VM roles
and private user workspaces, use `user-run` with the server's runs root instead:

```bash
$O user-run workflow.yaml --username researcher@pve --runs-root runs --run-id study-001
```

Use [the runnable workflow examples](../README.md#run-simple-workflows) for details.
Non-Proxmox desktop guest backends remain placeholders.

## Image maintenance

The PNGs are generated with the built-in image generation tool and visually
reviewed against the implementation. Matching copies live at the root of the CAF,
evaluator and orchestrator repositories so their documentation is self-contained.
The prompt record lives in the evaluator's `docs/architecture-image-prompt.md`.
