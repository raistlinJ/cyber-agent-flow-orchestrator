# Reuse a ScenarioForge provision configuration

On the Proxmox host, pass the same `scenarioforge-lab.conf` file used by the
ScenarioForge provisioner:

```bash
uv run cyber-agent-flow-orchestrator --provision-config /root/scenarioforge-lab.conf
```

`serve` is implied. The existing web defaults, PVE login, `runs/` root and automatic
first-launch certificates still apply. Existing certificate/key pairs are never
replaced. You can combine this option with `--web-config`, `--runs-root`, and an
explicit base workflow:

```bash
uv run cyber-agent-flow-orchestrator serve examples/02-deploy-evaluate.yaml \
  --provision-config /root/scenarioforge-lab.conf
```

The importer reads ScenarioForge's **Proxmox `key=value` format**, including blank
lines, full-line comments and matching single/double quotes. It never executes
shell code, expands environment variables, provisions VMs or changes PVE ACLs.
Other hypervisors and installer state files are not supported by this importer.

## Imported settings

| Provision key | Orchestrator setting |
| --- | --- |
| `app_vmid` | ScenarioForge guest VM ID |
| `participant_vmid` | CAF participant guest VM ID |
| `core_vmid` | CoreVM monitoring ID |
| `llm_provider_type` | Runtime `model.provider`, when nonempty |
| `llm_provider_url` | Runtime `model.url`, when nonempty |
| `llm_model` | Runtime `model.name`, when nonempty |

Omitted VM IDs use the provisioner's defaults: CoreVM 9401, app 9402, participant
9403. IDs must be valid and distinct. Imported VM IDs replace the template's role
IDs, including matching preparation, artifact and before-trial commands. Commands
targeting other VM IDs are unchanged. The shipped `/var/lock/caf-lab-9401.lock`
follows the imported CoreVM ID; custom locks are retained. Continue using one
shared lock for every experiment targeting the same lab.

Nonempty imported model settings override their template counterparts. The model
API-key environment variable defaults to `MCP_API_KEY`, matching provisioned CAF;
an explicit template setting is retained. The import does not copy an API key or
create a guest environment file. Keep credentials in your existing guest setup.
Provider URLs containing credentials, query strings or fragments are rejected.

The standard guest paths and user already match the provisioner:
`/opt/scenarioforge`, `/opt/scenarioforge/.venv/bin/python`,
`/opt/cyber-agent-flow`, `/opt/cyber-agent-flow/venv/bin/python`, and `participant`.
Custom paths in the base workflow/runtime are retained.

The config file does **not** describe the scenario XML, selected scenario,
evaluation export, tasks, tool catalogs, trial budgets or evaluation allow/deny
scope. Those remain in the workflow/runtime. In particular, management, HITL and
LLM network addresses are not converted into evaluation scope or discovery facts.
Review the experiment template before starting evaluation; starting the dashboard
does not execute an experiment. Passwords, SSH keys, bridge settings, source URLs
and installation switches are ignored and are not copied into the profile.

Only file values are imported. If provisioning used `SF_*` environment variables
or command-line overrides, update the config to reflect the actual installed IDs
and model settings before importing it.

## Profiles and precedence

The launch option creates a separate profile under
`.local/provision/<settings-hash>/` and prints its workflow path. Base workflow,
runtime, web settings and provision config files are not overwritten. Catalog
and guidance paths are resolved to their existing host files. Keep those files
available; the profile is not a portable dataset export.

Repeating the same import reuses the profile and preserves edits to its generated
workflow/runtime. Changes to imported settings or base inputs produce a new
profile; changes to ignored fields such as passwords do not. Use the same launch
directory and flag on subsequent starts, or pass the printed profile workflow
directly to `serve`. Without the flag or an explicit workflow, startup continues
to use `workflow.yaml` in the current directory.

For a named profile you can edit and use with CLI experiments:

```bash
uv run cyber-agent-flow-orchestrator import-provision /root/scenarioforge-lab.conf \
  --workflow examples/02-deploy-evaluate.yaml --output profiles/my-lab

# Review/edit profiles/my-lab/workflow.yaml and runtime.yaml, then:
uv run cyber-agent-flow-orchestrator serve profiles/my-lab/workflow.yaml
uv run cyber-agent-flow-orchestrator plan profiles/my-lab/workflow.yaml
uv run cyber-agent-flow-orchestrator user-run profiles/my-lab/workflow.yaml \
  --username researcher@pve --run-id experiment-001
```

`--workflow` is optional for `import-provision`. An explicit output directory must
be new; the command refuses to replace an existing profile. `plan` validates host
inputs; it does not verify that the VMs or model service actually exist.

## Per-user VM roles

Imported profiles include `monitoring.initial_roles`. On a user's first dashboard
visit, these roles are initialized only for eligible VMs visible to that user's
PVE identity on the current node. Effective permissions are rechecked before
saving and before guest operations. Roles for inaccessible VMs remain empty; the
config file grants no access and does not expose other users' VMs.

Existing saved roles always win, including deliberately cleared selections. Users
can change them in the WebUI. Switching profiles under the same `runs/` root does
not replace those saved choices. Workspaces remain private per PVE identity.
Ordinary profiles without `initial_roles` continue to start with empty selections.

## Validation

Tests cover parsing, malformed IDs and URLs, secret omission, profile preservation,
command remapping, model settings, shared locks, unchanged network scope, implicit
CLI launch, and initial VM roles through the HTTPS PVE test server. Run:

```bash
uv run --group dev pytest -q
```

These checks use simulated guest operations, not a live Proxmox deployment.
