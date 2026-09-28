# Proxmox login and the orchestrator role

The WebUI can authenticate existing Proxmox accounts. Membership in a designated
**PVE group** grants the application's **`orchestrator` role**. A correct password
alone does not grant access; even `root@pam` must belong to the configured group.
No separate orchestrator password or service API token is required in PVE mode.

PVE authentication is specific to this deployment mode. Future local
macOS/Linux/Windows deployments will not require PVE, realms, login, or the HTTPS
proxy; see the [local deployment requirements](interfaces.md#planned-local-desktop-deployment).

The group grants application access and host-mediated guest control **only for
VMs where the user has effective `VM.Audit`**, including inherited pool/group
permissions. The VM must be a non-template QEMU VM on the current Proxmox node.
This deliberately provides guest control beyond a read-only native PVE role:
enrollment in the dangerous orchestration-access group is the opt-in. No global
VM ACL or native administrator/monitor privilege is granted by this app.

Each user selects their own VM roles and sees their own saved runs. There is no
shared PVE dashboard fallback or global inventory cache. Ordinary templates do not
assign VM IDs automatically; [provision imports](provision-config.md) can initialize
authorized roles once, preserving existing saved choices. Private workspaces use canonical PVE identities; owner and
paths come from the session, never a browser-supplied username. Unknown or another
owner's run IDs do not disclose results. Legacy unowned runs remain available only
through trusted host CLI commands. Project sharing is not implemented.

The implementation uses PVE's [ACL-filtered resource list](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm)
and [effective permissions](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/AccessControl.pm).
Permission values indicate propagation: presence of `VM.Audit`, even with value
`0`, is a grant. ACL revocation, group removal, migration off this node and PVE
outages block subsequent host commands. Saving VM roles is not a permanent grant.

## Set up existing PVE users

On the Proxmox host, as an administrator, create the group once and add each
existing user (substitute the correct username and realm):

```bash
pveum group add caf-orchestrator --comment 'CAF orchestrator access'
pveum user modify researcher@pve --groups caf-orchestrator --append 1
```

`--append 1` preserves the user's other group memberships. The equivalent WebUI
steps are **Datacenter → Permissions → Groups** to create the group, and
**Datacenter → Permissions → Users → Edit** to assign it. No PVE Administrator,
User.Modify, or VM execution privilege needs to be granted to the person for this
application login. `@pve` accounts are managed by Proxmox; `@pam` accounts use the
host's Linux credentials. PAM accounts must exist on the selected authentication
node. The orchestrator does not create or modify PVE accounts or groups itself.

## Configure the orchestrator

Run `python3 install.py` for first-time setup, then
`uv run cyber-agent-flow-orchestrator` on the Proxmox host. The installer asks
before downloading a missing evaluator checkout, then runs `uv sync`.
The first launch creates `web.yaml` and a workflow/runtime template in the current
directory. Later launches reuse them; results default to `runs/`. Login still
requires group membership and VM permissions as described above.

The generated `web.yaml` uses this node's FQDN, the Proxmox CA path and the
`caf-orchestrator` group. You can also start from
[web.pve.yaml](../examples/web.pve.yaml). Edit `auth.url` to the PVE
node's hostname matching its TLS certificate. For the default Proxmox CA, use
`/etc/pve/pve-root-ca.pem` on the host. If the PVE endpoint uses a certificate from a
publicly trusted CA, omit `ca_file` to use the system trust store. There is no
option to disable TLS or hostname verification, and redirects are rejected.

```yaml
auth:
  provider: pve
  url: https://pve.lab:8006
  ca_file: /etc/pve/pve-root-ca.pem
  required_group: caf-orchestrator
  realms: [pve, pam]
```

The `auth` block goes in the **web settings**, not the experiment/runtime YAML.
`required_group` is mandatory. Only listed realms are accepted; `pve` and `pam`
are the defaults. Configured LDAP/AD password realms can be listed explicitly.
OpenID Connect redirects, WebAuthn/security-key challenges, and recovery-key login
are not implemented. Unsupported second factors never grant a session; use an
account with a supported factor or the normal PVE interface. Do not disable MFA
to work around this limitation.

The PVE API certificate is separate from the certificate served to your browser.
The ScenarioForge Proxmox installer generates the browser certificate on first
orchestrator installation at `/certs/cert.pem`, with its private key at
`/certs/key.pem`. Both `web.pve.yaml` and `web.lan.yaml` read these absolute paths.
The self-signed certificate lasts 365 days and covers localhost, loopback addresses,
and the Proxmox short hostname/FQDN. Reinstalling preserves an existing pair.

Manual installs get the same automatic certificate setup on first server launch:

```bash
python3 install.py
uv run cyber-agent-flow-orchestrator
```

`uv sync` installs dependencies only; the first launch generates missing
certificates. Existing pairs are preserved, including signed certificates. If only
one file exists, startup stops rather than replacing it. No `--locked`, `--no-dev`,
`serve`, `--runs-root`, or `--web-config` is required for the default launch.
Supply `serve WORKFLOW --web-config WEB_YAML --runs-root DIRECTORY` to override.
Default settings and workspaces are relative to the launch directory; continue
using the same directory. The generated experiment template needs your lab paths,
model, network scope and shared target lock before starting evaluation.

To switch to a CA-issued certificate, replace `/certs/cert.pem` with the PEM server
certificate followed by its intermediate chain, and `/certs/key.pem` with the
matching unencrypted PEM key. Keep the private key mode `0600` and readable by the
server account, then restart `serve`. The paths in YAML stay the same. There is no
automatic renewal or certificate reload. For LAN access, the certificate must
cover the hostname in `public_url`.

The example listens on loopback. From your workstation, forward it if needed:

```bash
ssh -N -L 8443:127.0.0.1:8443 root@YOUR_PROXMOX_HOST
```

Open **https://localhost:8443** using a browser that trusts the local certificate.
Sign in with the full username, such as `researcher@pve`. For direct LAN access,
change `listen` and `public_url` and supply a matching certificate as described in
[HTTPS setup](https-login.md#direct-lan-access).

Do **not** run `create-user` for PVE login or include `users_file` in this config.
PVE and local providers cannot be active together; an unavailable PVE endpoint
never falls back to local authentication. Existing local configurations continue
to work; the generated default configuration selects `auth.provider: pve`.

## Login, second factors and revocation

1. The backend submits the password to PVE's HTTPS `/access/ticket` API.
2. If PVE returns a second-factor challenge, the page requests a TOTP authenticator
   code. The signed PVE challenge stays server-side. The browser receives only a
   random, single-use challenge identifier, bound to its network peer and expiring
   after three minutes. Failed verification requires starting the login again.
   The optional code field on the initial page also supports realm-enforced OTP.
3. After complete authentication, the backend requests `/access/users?full=1`
   with the user's ticket and verifies that user's enabled/expiry state and exact
   group membership. PVE permits authenticated users to see their own entry;
   no elevated user-directory privileges are necessary.
4. The browser gets an opaque orchestrator session cookie. Its session information
   includes the canonical PVE username and `role: orchestrator`. Passwords are not
   stored. PVE tickets remain in process memory and are never sent to the browser,
   placed in command arguments, or written to the dataset.

Every protected request rechecks the PVE account and group; no positive membership
cache is used. Removing membership, disabling/deleting the account, or expiring it
invalidates access on the next protected request. For removal, edit the user's
groups in the PVE WebUI, preserving their other memberships. PVE password changes
alone do not necessarily revoke an already issued PVE ticket; disable the account
or remove the group to revoke orchestrator access.

PVE outages, TLS errors and malformed responses block protected responses. Logout
still works locally with the existing CSRF token during a PVE outage. The existing
login limits, secure cookie flags, same-origin checks, idle timeout and server-side
logout apply. PVE sessions are capped at two hours (or a shorter configured maximum)
because PVE tickets expire; tickets are not silently renewed. A server restart
clears sessions and pending challenges. This reuses credentials, not an existing
PVE browser session or single sign-on.

## Host commands and current scope

The backend runs `qm` under its Linux service account. Before each user-scoped
host command it verifies the active PVE identity/group, current VM.Audit permission
and local-node VM placement. The host account still needs qm privileges; PVE login
does not grant a shell or impersonate the user's Linux identity. No privileged
broker or sudoers policy is installed by this change.

Use the [authenticated user CLI](../README.md#multiple-pve-users) (`user-run`,
`user-resume`, `user-recover`) to execute with that same scope. A password and, when
required, TOTP are prompted for; tickets remain in process memory and expire within
two hours. `user-run` freezes role-resolved input files in the owner's workspace.
Resume uses those frozen files, independent of later UI selections. Every guest
exec, exec-status, transfer chunk, hook and recovery command passes the dispatch
guard. After revocation no further commands, including cleanup commands, are
sent; an existing transient guest job may continue until its configured runtime
limit. An authorized host administrator can recover it using the trusted CLI.

The original `run`, `resume`, `recover` and filesystem inspection commands remain
trusted host-administrator interfaces. They do not impersonate a PVE user and their
legacy outputs are hidden from the PVE dashboard. Local-account login retains the
shared administrator dashboard; PVE mode is required for this user isolation.
The browser can launch the fixed [sample catalog](webui.md#bundled-samples) using
background workers. Role selection and sample launch are CSRF-protected write
endpoints; results and CSV downloads resolve through the requesting user's workspace.
Every sample guest command rechecks current VM access, including fixture cleanup.

Commands inside an authorized VM retain that VM's network access and credentials.
This host VM authorization is not a guest network sandbox. Users sharing the same
VM can observe its running processes. Shared target/per-VM locks remain in place;
use the same absolute target lock for workflows that share a lab.

## Validation and API references

Automated tests use an isolated HTTPS PVE API double and the real Python proxy.
They cover TLS trust/hostname checks, group denial/revocation, disabled/expired
users, allowed realms, TOTP and realm OTP, incomplete/expired/replayed challenges,
PVE outages, rejected redirects, no local fallback, browser-cookie isolation,
per-user VM inventories/selections, forged IDs, node migration, dispatch guards,
private run access, and role-based CLI input resolution.
The optional PVE browser smoke test covers the actual login/TOTP screens.
No live Proxmox host has been used for these tests; verify login and membership
revocation on your installed PVE version before relying on it operationally.

```bash
uv run --group dev pytest -q
uv run --group dev python tests/browser_pve_smoke.py /tmp/caf-pve-preview
```

Protocol references: Proxmox's [ticket API implementation](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/AccessControl.pm),
[user index and group membership](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/User.pm),
[TOTP challenge client](https://github.com/proxmox/proxmox-widget-toolkit/blob/master/src/window/TfaWindow.js),
and [user/realm management](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc).

## Application maintenance permission

Version inspection uses ordinary scoped VM access. Application update and rollback
also require the separate `caf-maintainers` group by default. See
[application updates](application-updates.md) for enrollment, configuration and
recovery; no new PVE root/native VM execution privilege is granted to the user.
