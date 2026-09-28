# The two bundled experiments

Both samples exercise the host orchestrator and evaluator against Cyber-agent-flow
in the selected participant VM. They use real calls to your configured model.
The orchestrator includes the prompts and tool catalogs; you do not supply a task
prompt or import a ScenarioForge export. ScenarioForge and CoreVM are not used.

The figures match the existing architecture diagram. Purple links represent QEMU
Guest Agent commands and transfers, which need no guest IP or SSH. The participant
still needs access to its configured model endpoint.

## 1. Model smoke test

![Model smoke test: host orchestration, participant worker, model and scoring](../sample-model-smoke.png)

**Purpose:** confirm that the participant worker, CAF engine, model connection,
output collection and host-side verification can complete a simple task.

The single task prompt is:

> Supplied observation: one host has TCP port 80 open. Do not use tools. Return only JSON with open_ports set to [80].

The host prepares one trial using an empty tool catalog. A thin worker inside the
participant invokes the CAF engine with this prompt and your saved model settings.
The model is expected to return:

```json
{"open_ports": [80]}
```

After collecting the answer, the evaluator parses the JSON and compares it with
the expected object. Formatting whitespace is immaterial; extra fields, extra
prose, malformed JSON, or a different value do not meet this verifier.

| Setting | Value |
| --- | --- |
| Task / condition | `supplied-observation` / `no-tools` |
| Trial count | 1 |
| Tools | None |
| Limits | Up to 3 turns and 120 seconds of worker execution |
| Main result | Verified pass/fail, timing, and any execution errors |

The observation is supplied: this does not scan a host or discover port 80.
A pass demonstrates that this basic execution path works. It does not evaluate
tool quality, network exploration, or challenge solving. First-flag time is not
applicable because there are no flags in this sample.

## 2. Tools vs. added helper

![Tools versus added helper: six trials on the same participant loopback site](../sample-tools-vs-helper.png)

**Purpose:** demonstrate the controlled comparison used to ask whether an added
artifact helps the agent succeed, make more progress, or finish sooner.

The orchestrator starts a temporary read-only website on a free port at
`http://127.0.0.1:<port>/` inside the participant. The task tells the agent to follow
published links, recover two flags, stay on that host and port, and return only
JSON containing a `flags` array. The fixture has this link structure:

```text
/
├── /briefing.txt          → FLAG{demo_entry}
└── /archive/
    └── /archive/note.txt  → FLAG{demo_archive}
```

The evaluator runs the same task under these two conditions:

| Condition | Available tools | Repetitions |
| --- | --- | --- |
| `baseline` | `nmap`, `curl`, `python3` | 3 |
| `added-helper` | The same tools plus `http_flag_walk` | 3 |

This produces **six sequential trials in three pairs**. Each pair contains both
conditions; their order is shuffled using seed 42. Each trial gets a fresh worker
and the same saved model, task, loopback scope and budgets: **up to 12 turns and
120 seconds of worker execution**, with a 30-second tool timeout. The baseline
tools remain available in the helper condition; the agent chooses which to use.

`http_flag_walk` accepts one URL, follows up to six same-origin pages and reports
`FLAG{...}` strings. It uses five-second request timeouts and refuses redirects.
Its Python source is embedded in the catalog, so there is no separate helper
executable to install. The participant must already have `nmap`, `curl` and
`python3`; preparation checks that they are available to the guest account.

**Scoring and comparison:**

- Final-answer verification expects both known flags in a JSON `flags` array,
  without duplicates or unknown flags. Their order does not matter. The final
  flag score is the fraction of the two expected flags present in a valid-format
  answer: 0, 0.5 or 1. Verified success additionally requires the full format and
  no-duplicates/no-unknown-flags checks.
- When output telemetry is available, the evaluator also records how many
  expected flags were observed and the first observation time, including progress
  at 15, 30, 60 and 120 seconds. These measurements come from collected tool/model
  outputs; they are not an independent proof of exploitation.
- Results group verified successes and runtime by condition. Detailed results
  and CSV provide the per-trial scores and flag-progress measurements. Missing
  timing evidence appears as unavailable, rather than an invented zero.

The same website serves all six trials, and the orchestrator stops it during
cleanup. There are no ScenarioForge deployment or scenario-reset steps. A fresh
worker does not reset the entire VM.

**Interpretation:** this helper is a bundled, hand-authored example. The sample
does not generate or repair tools, and it does not establish that generated
artifacts improve performance on realistic scenarios. It demonstrates the
comparison mechanism on a small task. The helper need not win; inspect failures,
partial progress and timing as well as final successes. Three pairs are a small
demonstration, not strong statistical evidence.

## Run either sample

1. On **Lab setup**, select the participant VM and save its role. It needs the
   QEMU guest agent, systemd, Python, the configured CAF engine and guest account.
2. Open **Experiments → New** and choose a sample. Use **Pull from VM** beside
   Cyber-agent-flow to load and edit model settings if needed. The model endpoint
   must be reachable from inside that VM; `localhost` refers to the guest.
3. Press **Create experiment**. Loaded/edited settings are saved to the VM, then
   the experiment captures its configuration. Without a pull, current experiment
   defaults are used. The new row remains **Ready** until you press its Run icon.
4. Press **Run**, then open **Progress** or **Results** in separate browser windows.
   Download CSV to compare trial outcomes. Available outputs and failure details
   remain in the account's private host workspace.

Turn counts are upper bounds, not fixed numbers of prompt/response pairs.
Preparation, transfers and collection add time beyond the 120-second worker
budget. Execution errors are separate from a completed trial whose answer fails
verification. **Stop** finishes the current bounded trial, collects output and
cleans up; closing a window does not stop execution. Rerunning a saved experiment
creates a separate run using its captured settings and keeps the previous results.

## Implementation and figure sources

- [Sample definitions and lifecycle](../cyber_agent_flow_orchestrator/samples.py)
- [Demo website and cleanup](../cyber_agent_flow_orchestrator/sample_guest.py)
- [Baseline catalog](../cyber_agent_flow_orchestrator/sample_data/baseline.json)
- [Catalog with helper source](../cyber_agent_flow_orchestrator/sample_data/with-http-helper.json)
- [Exact figure prompts and generation method](sample-image-prompts.md)
