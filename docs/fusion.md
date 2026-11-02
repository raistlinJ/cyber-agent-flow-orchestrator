# Run the experiment demos on VMware Fusion

Provision the lab with ScenarioForge's `scripts/provision/vmware-fusion-mac`
installer and `cyber_agent_flow=true`. Configure the provider endpoint and model,
and wait for all three guests to finish. Start CORE, APP and participant in Fusion.
The guest agent is VMware Tools (`open-vm-tools`), installed by that provisioner.

From the orchestrator checkout on your Mac, with the sibling evaluator updated:

```bash
uv sync
.venv/bin/cyber-agent-flow-orchestrator import-fusion --output fusion-local
.venv/bin/cyber-agent-flow-orchestrator serve --local fusion-local/workflow.yaml --web-config fusion-local/web.yaml --runs-root fusion-local/runs
```

Open https://localhost:8443, accept your local development certificate. The dashboard opens directly.
Use New to select either demo or import an existing ScenarioForge scenario. The
same XML preparation, deployment, readiness checks, evaluation, progressive hints,
cleanup, progress output and results bundles run through the Fusion transport.

`import-fusion` reads the provisioner's default saved state and credentials in
`~/Library/Application Support/ScenarioForge/fusion-lab`. Use `--state-dir` for
a custom state directory. The output directory must be new. The command checks
that provisioning completed with CAF enabled and imports the model/provider.

The inventory maps stable local IDs 9401/9402/9403 to the CORE/APP/participant
VMX files. They are not Proxmox IDs. VM selection and guest dispatch are limited
to this inventory. Local access uses your OS account and automatic HTTPS sessions;
Proxmox tickets, host root and SSH to the participant are unnecessary. Guest
commands use the provisioned login's sudo access, keeping the existing systemd
worker, bounded execution and journal-owned cleanup behavior.

Inventory credentials are in a private file (chmod 600), separate from experiment
configs and result exports. `vmrun` requires guest credentials in process arguments;
transport errors omit command arguments and redact passwords. RPC input is copied
into a unique owner-only guest directory, deleted after reading, and replies are
published atomically. Polling never resends an acknowledged guest command.

Large uploads (1 MiB and above) and downloads automatically use a temporary TLS
connection over an existing VMware host adapter when available. VMware Tools
authorizes and launches the transfer helper; bulk data bypasses its slower copy
RPC. The helper trusts only the temporary certificate passed through that control
channel. Transfers have a single-use token, byte limits and SHA-256 verification;
uploads become visible only after verification. The listener closes after each
transfer. No guest daemon, SSH, shared folder, route change or reprovisioning is
required. Maintenance progress reports bytes, percentage and transfer speed.

The evaluator discovers the adapter from the VMX and Fusion networking settings.
If that adapter cannot be used or the guest cannot connect, it falls back to
VMware Tools. For an unusual adapter layout, an inventory VM entry can specify
`"transfer_host": "192.168.20.1"` (the Mac's VMware adapter address), or
`"transfer_host": false` to use only Tools. This address is separate from the
localhost-only WebUI bind address. Application-specific RPC helpers retain their
own transfer protocol.

Host VM locks are keyed to the VMX path under your Mac user's cache directory;
preflight does not treat an old Proxmox run or a different Fusion lab with the
same local IDs as owning these guest services. Keep a separate imported profile
and run directory for each lab. Application source checks, updates and rollback
use the same bounded guest-agent transport and are authorized by the local desktop
session and imported VM inventory. Local operator model configuration is also
supported. API keys should be
saved in the participant configuration, not the host inventory or runtime YAML.

After **Check version**, a dirty checkout changes the action to **Force update**.
The confirmation names the tracked paths and states that those edits will be
permanently discarded. The server binds that confirmation to the inspected VM,
revision and changed-file list, rechecks them immediately before packaging, and
the guest resets tracked edits only after validating the selected revision.
Rollback restores committed revisions; it cannot recover discarded local edits.
Untracked files are never deleted by Force update.

Transport and WebUI tests use simulated guest operations. A complete live three-VM experiment still needs verification after provisioning.
Local startup and automatic dashboard access have been checked on the Mac.

With `--local`, access uses your current OS account automatically, without a separate password or operator group. The server is restricted to localhost. This option is accepted only for desktop-virtualization workflows. Proxmox rejects `--local` and retains its login and `caf-orchestration` group requirement.

Without `--local`, startup requires authentication. The flag overrides the bind address and public URL to localhost for that invocation and does not rewrite saved settings.


### Judge credentials on the Mac host

The judge and intermediate progress reviewer run on the orchestrator host.
A key saved in the CAF participant VM is available only to the participant;
using the same model URL does not transfer that key to the host. If the saved
judge model names `CAF_JUDGE_API_KEY`, provide that variable before starting the
orchestrator. A private `fusion-local/host-secrets.env` can contain:

```dotenv
CAF_JUDGE_API_KEY="your-judge-api-key"
```

Keep it owner-only (`chmod 600 fusion-local/host-secrets.env`) and start with:

```bash
uv run --env-file fusion-local/host-secrets.env -m cyber_agent_flow_orchestrator serve fusion-local/workflow.yaml --web-config fusion-local/web.yaml --local
```

The imported `fusion-local` directory is excluded from Git. The key is not
written into experiment settings or results. Restart the host after changing
its environment; then rerun the experiment to create a fresh recorded attempt.
An authenticated model server returning HTTP 401 is reachable but rejected the
judge credential; it is distinct from a participant wall-clock timeout.
