# Resource-owned VPS and saved OpenSSH targets

A VPS resource owns one canonical **Runner + SSH Alias** target. A Space imports
that resource and supplies presentation only; it never stores a second Runner or
connection reference. Whole-VPS execution needs no fictitious local Project or
folder. Optional Project associations classify the same complete VPS, not a
Project-directory sandbox.

## Create and use a VPS

1. In the device's **Runner settings**, enable SSH and explicitly approve the
   required literal alias. Credentials and OpenSSH configuration stay on that
   Runner. Read [Runner settings](RUNNER_SETTINGS.md) for local owner approval.
2. In **Resources → VPS**, enter only name, SSH Alias, connecting Runner, and an
   optional description. The alias field supports typing and discovered options.
   Refresh queries that selected Runner; only a recent, uniquely owner-approved
   alias can be saved. Identity/version metadata are generated, not form fields.
3. In a Space, choose the existing VPS card first, then set its display name,
   display position, optional description, and optional Project categories.

Display positions are `top_level`, `project_only`, or `both`. They do not create
additional connections or change the SSH target. Resource enable/disable and
explicit connection testing are separate actions. Metadata comes from a real
probe, never from a user-entered provider/region guess. Offline or stale discovery
is labeled as such and cannot create a new authority.

The Runner is a technical dependency of the saved resource. Importing or using a
VPS does not require a separate Space Device allocation, nor does it grant local
Runner/project access. Resource ownership, Space/profile VPS actions, fixed/live
connection consent and Runner-local SSH approval remain separate controls.

## Target identity and execution

Resources store an internal target reference, target fingerprint and monotonic
target version. Fingerprints cover the approved settings and every statically
included SSH config file. The public API and MCP list expose the alias and
observed metadata, not credentials or a caller-selectable connection reference.
Old Space-binding Runner/reference fields and old host/provider/region/system/
notes resource inputs are rejected, not translated.

Local and SSH one-shot execution share `ProcessSupervisor`. Native OpenSSH
handles authentication, keys, SSH Agent, ProxyJump, host-key checking and remote
shell setup. Persistent-shell and PTY sessions have distinct original session
IDs, immutable target envelopes, command/input receipts and recovery semantics.
There is no password cache, sshpass transport or fallback to a different Runner.
An old Runner lacking the resource-owned settings/transport capability cannot
receive a new VPS target.

A queued operation cannot silently follow a changed resource, alias, Runner or
SSH file. The Hub checks the saved resource identity and the Runner checks its
current local target fingerprint before execution. Display-only edits preserve
the identity. Retargeting increments its target version, including an ABA change
back to the same alias; fixed-consent connections must be reviewed again.

Existing accepted work is never replayed against a replacement target. Inspect
the original operation/session receipt after timeout, disconnect or cancellation.
Stopping a local SSH client is not proof that every remote effect stopped.
Sessions do not recover cwd/environment by silently opening another shell after
Runner restart. Changed local SSH configuration fences new input to an old
session; original status/read/cleanup operations do not recreate it.

## VPS-only hard cutoff (no legacy runtime)

The resource-owned model is `vps_schema=resource-owned-1` within `workspace-2`.
An existing workspace-2 database with old VPS tables fails startup with
`VPS_CUTOFF_REQUIRED`. Startup does not delete or modify that database, and no
runtime migration or old-field adapter exists. Earlier workspace schemas still
require their separate workspace cutoff; this command is not a general migrator.

Stop the Hub, resolve/stop old remote jobs and sessions explicitly, and preview:

```sh
python -m hub.vps_cutoff --data-dir /path/to/hub-data
```

After reviewing counts and confirming the remote-process state, apply:

```sh
python -m hub.vps_cutoff --data-dir /path/to/hub-data \
  --drop-vps --confirm-remote-processes-stopped
```

The command acquires the normal Hub instance lock, refuses a running Hub, creates
a private consistent SQLite backup, and resets VPS resources, Space bindings,
Project classifications, VPS sessions, associated operation receipts and old VPS
policy/consent entries. It creates **empty new VPS tables**. It does not convert
any target, keep old IDs usable, or reconstitute old grants.

Users, devices, projects, folders, storage mappings, unrelated operations/sessions,
non-VPS policy rules, master key and actual project/remote files remain intact.
The tool neither connects to SSH nor claims to stop remote processes. The backup
is an operator recovery artifact, not a legacy execution mode; retain its matching
`master.key` and protect both. To roll back a failed deployment, stop the Hub and
restore the complete original backup with the matching old application version,
not individual old VPS rows into the new schema.

Upgrade Runners, restart the Hub on the new model, then register reviewed VPS
resources again and import them into Spaces. Re-create only the intended VPS
permissions/consent. Old operation/session IDs are not valid targets to replay.

## Acceptance

`python scripts/check_openssh_execution.py --output <fresh-report.json>` exercises
a disposable loopback OpenSSH lab: direct keys, SSH Agent, ProxyJump, one-shot
receipts, persistent cwd/environment, PTY input/resize, timeout/disconnect and
cancellation without replay. Its fixtures do not certify a production VPS fleet.
The application test suite additionally covers owner authorization, revision
conflicts, discovery safety, cutoff preservation, resource identity changes and
real Chromium/WebKit forms. Native Windows/macOS behavior requires those CI jobs;
Linux test results alone do not claim native-platform verification.
