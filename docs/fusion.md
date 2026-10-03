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

Host VM locks are keyed to the VMX path under your Mac user's cache directory;
preflight does not treat an old Proxmox run or a different Fusion lab with the
same local IDs as owning these guest services. Keep a separate imported profile
and run directory for each lab. Application source-update actions remain disabled
for Fusion; local operator model configuration is supported. API keys should be
saved in the participant configuration, not the host inventory or runtime YAML.

Transport and WebUI tests use simulated guest operations. A complete live three-VM experiment still needs verification after provisioning.
Local startup and automatic dashboard access have been checked on the Mac.

With `--local`, access uses your current OS account automatically, without a separate password or operator group. The server is restricted to localhost. Proxmox retains its login and `caf-orchestration` group requirement.

Without `--local`, startup requires authentication. The flag overrides the bind address and public URL to localhost for that invocation and does not rewrite saved settings.
