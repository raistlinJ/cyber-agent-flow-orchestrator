# CLI and WebUI boundary

The CLI and a WebUI with PVE VM-role selection and monitoring are available. Both use application services
rather than invoke each other's command lines. Browser execution controls are not
implemented. See [the dashboard guide](webui.md).

```mermaid
flowchart TD
    CLI[Installed CLI / python -m] --> Service[Orchestrator service API]
    Browser[Browser] -->|HTTPS| Proxy[Python TLS proxy]
    Proxy --> Auth[Login and session checks]
    Auth --> UI[Read-only WebUI]
    UI --> Service
    UI --> Monitor[Guest and VM monitor]
    Monitor --> Proxmox[Proxmox status and guest-agent probes]
    Service --> Workflow[Workflow journal, locks, preparation, recovery]
    Service --> Reports[Evaluator reporting API]
    Workflow --> Eval[Evaluator coordinator and worker transport]
    Reports --> Records[Manifest and attempt records]
    Eval --> Records
```

`cyber_agent_flow_orchestrator.service` owns workflow operations, inspection and
export. `cyber_agent_flow_eval.reporting` owns trial record selection, metrics,
collected logs and result serialization. The CLI owns argparse, JSON rendering,
progress printing and exit codes. Neither service layer imports argparse or exits
the process. Inspection is read-only; exports and execution explicitly acquire
locks. Long-running execution is blocking and accepts a progress callback.

Future browser execution controls should run workflows in supervised jobs and keep HTTP handlers
responsive. It must use these same locks and journals, authenticate operators in
shared/networked deployments,
restrict accessible paths/VMs, and implement job cancellation with cleanup. Do not
expose arbitrary command YAML or private run directories to unauthenticated clients.
The Python HTTPS proxy fronts a private loopback server with mandatory login.
Both dashboard and cached status API require a session; the backend also requires
a per-start proxy secret. It starts no workflow jobs and exposes no execution
endpoint. See [HTTPS and login](https-login.md). Guest probes run
in a background polling thread, separate from HTTP requests.

Data semantics:

- Attempt JSON is authoritative; dataset files are derived exports.
- Latest attempts determine summaries. All-attempt views preserve retry evidence.
- Workflow journal status and coordinator activity are separate observations.
- Inspection during execution can reflect adjacent record updates; it is not a
  transaction across the whole run. Idle export acquires both workflow/evaluator locks.
- Result exports are analysis datasets, not a complete replay/backup package or a
  redacted participant bundle. Logs, prompts and private suite files are omitted.
- Reading a run never invokes an engine or a guest agent. Recovering one may.

Existing standalone evaluator use remains supported. The dependency direction is
orchestrator → evaluator → configured CAF engine. The evaluator does not depend on
the orchestrator or a future web framework.

## Planned local desktop deployment

When the macOS, non-Proxmox Linux, and Windows backends are implemented, the
orchestrator must support running locally for the current OS user without PVE
accounts, realms, groups, or a PVE connection. This local mode must not require
login, an account file, TLS certificates, or the Python HTTPS reverse proxy.
The browser will connect directly to a loopback-only local WebUI; host operations
will use the launching OS account's permissions.

Authentication and the HTTPS proxy are deployment concerns, not requirements of
the shared orchestrator/evaluator engine or every VM backend. Keep the PVE
authentication provider and group-to-role mapping specific to deployments that
select them. Select local versus shared/networked access explicitly, independently
of OS or hypervisor selection: Linux can host either a local application or PVE.
Local mode is not an automatic fallback when PVE authentication fails.

This is a recorded implementation requirement, not an available mode yet. The
current `serve` command still requires HTTPS and login. Existing Proxmox behavior
is unchanged; future shared/networked deployments retain explicit authentication
and HTTPS configuration.

## PVE user scope

PVE mode uses `UserDashboard`, canonical-identity workspaces and `PVEAccess`.
`GET /api/status` returns only that account's available local VMs, role selections
and private runs; `POST /api/roles` validates the session, CSRF and every selected
VM. `GET /api/runs/{id}/status` and `/results` resolve IDs only beneath the session
owner's runs directory. The browser cannot submit an owner or host path.

The authenticated `user-run`, `user-resume` and `user-recover` CLI commands use
`authorized_operations` from the evaluator's Proxmox transport. The callback is
captured by each GuestAgent and runs before every qm subprocess. PVE permissions
are checked at dispatch, not just when a workflow starts. Trusted administrator
CLI calls and standalone evaluator calls can still use the transport without a
user context. Future browser execution endpoints must use the scoped entry points,
not the unrestricted administrator API. No browser execution endpoint exists yet.
