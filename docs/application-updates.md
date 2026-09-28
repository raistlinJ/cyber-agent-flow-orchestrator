# Application updates through the host

The **Application versions** section manages Cyber-agent-flow in the selected
participant VM and ScenarioForge in the selected app VM. CoreVM is not updated.
The host downloads source from the configured Git repository, packages it as a
Git bundle and transfers it through QEMU Guest Agent. Neither VM needs internet
access, an SSH server, a management IP connection to the host, or a connection to
the other VM. Git must already be installed in both guests.

This is **source maintenance for existing installations**, not initial provisioning
or an OS/package upgrade. The existing Python environment is checked and reused.
Dependency-manifest changes are rejected before activation; use provisioning or a
separate dependency maintenance procedure for those upgrades. The updater never
runs `pip install`, `uv sync`, or network Git fetches inside the VM.

## Update the host installation

This feature requires evaluator **0.4.1+**, whose guest launch guard coordinates
updates with experiment startup. From your existing host checkouts:

```bash
cd /root/cyber-agent-flow-eval
git pull --ff-only
cd /root/cyber-agent-flow-orchestrator
git pull --ff-only
python3 install.py
```

Restart the orchestrator using your existing command/configuration. Existing
certificates, settings, VM selections and results are preserved. Other host
orchestrators or standalone evaluators controlling these VMs must also use the
updated evaluator for the guest-side launch guard to apply.

## Give an operator maintenance access

Checking versions requires ordinary orchestrator membership and current access
to the selected VM. **Update** and **Roll back** additionally require membership
in `caf-maintainers`. As a Proxmox administrator, create that group once, then add
the intended maintainer without replacing their existing memberships:

```bash
pveum group add caf-maintainers --comment 'CAF and ScenarioForge application maintenance'
pveum user modify researcher@pve --groups caf-maintainers --append 1
```

Keep the user's existing `caf-orchestrator` membership and VM/pool permissions.
The equivalent group membership can be configured in the PVE WebUI. Maintenance
permission is checked again before each guest command and file-transfer chunk;
removing either permission blocks further dispatch. Updates affect everyone using
the VM, not just the operator's private experiment results.

## Use the WebUI

1. Save the participant and ScenarioForge VM roles.
2. Click **Check version** for an application. The page shows its last checked
   commit, tracked-local-edit state, and (for CAF) missing evaluation controls.
3. Enter a branch, tag or full commit SHA from its configured repository. The
   default is `main`. Click **Update**.
4. Watch the maintenance job below the cards. **Details** includes the source URL,
   requested ref, resolved commit, bundle checksum, guest outcome and errors.
5. Use **Roll back** to restore the previous successful revision. Files generated
   since the update remain in place.

Requests run in background workers. A revision check is explicit, rather than a
Git operation on every dashboard poll. Select **Check version** again after any
out-of-band guest change. The UI shows only your own host maintenance jobs, and
role changes affect subsequent requests. Up to two maintenance jobs can run per
server, with at most one per account. The browser accepts neither guest commands,
repository URLs, filesystem paths nor VM IDs for these operations.

## Paths, sources and disabling the feature

CAF uses the runtime YAML's `engine.path` and `engine.python`. ScenarioForge uses
`monitoring.scenarioforge_path` (or `scenarioforge.repo`, default
`/opt/scenarioforge`), and `scenarioforge.python` (default
`<scenarioforge-path>/.venv/bin/python`). The checkout must be a normal absolute
Git root, not a symlink. Its filesystem owner runs Git and Python validation.

The updater manages `monitoring.scenarioforge_service`, default
`scenarioforge-web.service`, and an optional `monitoring.caf_service`. An active
configured service is stopped just before activation and restarted afterward.
A previously inactive service remains inactive. Other application processes
must be stopped before updating; the updater will not kill arbitrary processes.

Optional overrides in `web.yaml` (restart after editing):

```yaml
updates:
  group: caf-maintainers
  participant:
    url: https://github.com/raistlinJ/cyber-agent-flow.git
    ref: main
  scenarioforge:
    url: https://github.com/raistlinJ/scenarioforge.git
    ref: main
```

Only the host administrator can change source URLs. Use a trusted HTTPS repository
without embedded credentials. Omitted settings use the defaults above. Set
`updates: false` to hide the section and reject maintenance requests.

## Activation, preservation and recovery

The host resolves the requested ref to a commit and builds a bundle, excluding
objects already present when the guest revision is a known ancestor. The guest
verifies its checksum and Git prerequisites, stages a detached worktree, checks
dependency manifests, imports the application using its configured Python, and
runs `pip check` when pip is installed. CAF updates must provide the evaluator
controls; rollback may restore an older version without them.

Before activation, the updater reserves the workflow target and selected VM using
the evaluator's locks. Guest-side locking also prevents trial/hook starts during
maintenance. Active evaluator/workflow/sample units and other application
processes block activation. Updates are not silently queued behind an experiment:
if a required reservation is held, the maintenance job fails and can be retried
once the experiment finishes.

Activation checks out the verified commit **detached** at the existing application
path. This keeps virtualenv paths, service definitions, configuration, catalogs,
generated tools, scenario outputs and results at their existing locations. Tracked
local edits and untracked files that conflict with the new revision cause refusal;
there is no `reset --hard`, `git clean`, forced checkout or automatic stashing.
Commit/preserve such edits or resolve the conflict deliberately before retrying.
The guest branch and origin configuration are not rewritten; subsequent updates
should use the orchestrator, rather than expecting `git pull` on a detached HEAD.

A failed service restart triggers an attempt to restore the original commit and
restart the prior service. If activation/restoration is interrupted, a guest
pending marker blocks new experiment launches. **Check version**, then **Roll
back**, uses the durable guest record to recover. If there are new local edits,
permission is revoked, Git is left locked, or service restoration still fails,
resolve that problem as a VM administrator before retrying rollback. Do not delete
the pending marker to bypass an unresolved application state.

The guest records maintenance state and retained source bundles under
`/var/lib/caf-application-updates/`; the launch guard uses
`/run/caf-application-maintenance.lock` and the durable marker
`/var/lib/caf-application-maintenance.pending`, so a reboot cannot silently clear
an incomplete update.
Host job records and downloaded Git objects are private to the operator under
`RUNS_ROOT/_users/<owner-hash>/updates/<request-id>/`. They are retained for audit;
there is no automatic garbage collection yet.

Rollback changes application source and service state. It does not roll back a VM,
Python dependencies, databases, configuration changes or experiment output.
Evaluator manifests record the CAF source hash, Git revision and tracked-edit state
for subsequent Proxmox runs. Service `active` status and import checks are the
available health checks; they do not prove every application feature works.

## CLI

The CLI uses the same saved VM roles, authorization, workers and records:

```bash
uv run cyber-agent-flow-orchestrator user-app inspect --role participant --username researcher@pve
uv run cyber-agent-flow-orchestrator user-app update --role participant --ref main --username researcher@pve
uv run cyber-agent-flow-orchestrator user-app update --role scenarioforge --ref main --username researcher@pve
uv run cyber-agent-flow-orchestrator user-app rollback --role scenarioforge --username researcher@pve
```

Pass `--config`, `--web-config` and `--runs-root` when your server uses nondefault
locations. The command prompts for PVE credentials, waits for completion, prints
the job JSON and exits 0 on success or 1 for a failed maintenance job.

## Validation

Tests exercise real local Git bundles/worktrees/checkouts for both applications,
rollback preserving new runtime files, dirty-source/dependency/checksum refusal,
service-start failure recovery, interrupted activation, active-job exclusion,
permission revocation, HTTPS/CSRF and owner scope. Linux services, PVE and guest
transport are simulated; this is not a live Proxmox deployment test.

```bash
uv run --group dev pytest -q
uv run --group dev python tests/browser_updates.py /tmp/caf-updates-preview
```
