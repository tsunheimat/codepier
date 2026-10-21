# Runner owner settings

Open **Resources → Devices → Full management → Runner settings**. This is a
panel-only, device-owner/instance-admin operation; a Space role, MCP grant,
Project execution permission or `allowed_roots` entry does not authorize it.
The built-in Hub Runner still exposes only its existing Hub-managed roots.

## Local approval is independent

Remote writes default to **off**, including on existing Runner installations.
Reading a snapshot, previewing a patch, editing a draft, and exporting a minimal
patch do not authorize a write. On the Runner, its local owner may explicitly run:

```sh
python -m agent.runner_settings --config /path/to/config.json enable-remote --confirm-owner
```

The running Runner must observe this local opt-in before the panel can apply a
patch. The panel cannot change `settings_management.enabled`, its Hub URL, its
state directory, its identity, its enrollment secret, or the built-in root policy.
Do not use a separate remote Shell operation to bypass local approval.

To withdraw approval locally:

```sh
python -m agent.runner_settings --config /path/to/config.json disable-remote
```

Both the current saved configuration and the running configuration must permit
a remote write. A locally revoked setting immediately blocks subsequent saves,
even before the watch loop has observed it. Ordinary local edits remain valid.

## Configuration, effective values and readiness

The form is generated from normalized backend configuration, defaults, and
entry templates. It covers name, Hub/state paths, allowed roots, named tasks,
Shell, SSH, persistent sessions, RCH, MCP policy, skills, Computer, integrations
(language servers/browser/file imports), checkpoint size, and management opt-in.
Advanced redacted JSON and per-section local patch export are available too.

A saved value is not a successful connection test. The panel distinguishes
saved configuration, effective values, pending fields, restart-required fields,
executable availability, native-permission/extension prerequisites, stale
observations, and explicit SSH test results. Offline cache is labeled stale and
cannot be applied. A local `status` invocation cannot observe the service's live
memory and reports `configured_unverified`, not healthy/applied.

`computer`, `integrations` and `state_dir` changes require a normal service restart;
the panel does not restart services or terminate jobs. Other supported settings
are loaded by the existing watcher. Existing sessions retain their original
idle/lifetime limits; new sessions use new limits. Revoked execution permissions
still prevent new commands/input. Shell runs as the Runner's operating-system
account: neither roots nor a selected Project are an OS security sandbox.

## Local preview and apply

Export a patch and copy the exact revision shown by the panel or local status:

```sh
python -m agent.runner_settings --config /path/to/config.json status
python -m agent.runner_settings --config /path/to/config.json preview \
  --patch /path/to/runner-settings.patch.json --expected-revision <sha256>
python -m agent.runner_settings --config /path/to/config.json apply \
  --patch /path/to/runner-settings.patch.json --expected-revision <sha256>
```

Each supplied top-level section replaces that section; omitted sections are
preserved. `null` removes an optional section and restores its validator default.
Map entries, tasks and roots can therefore be removed deliberately. A masked
private-value placeholder means **preserve the value at this exact path**, not
empty it. Do not edit/reorder a masked array; edit secret-bearing command arrays
locally. Identity and enrollment fields are never patchable through this tool.

All changes use the same whole-configuration validator as Runner startup. A
revision mismatch refuses the save instead of overwriting another editor. The
writer takes a local lock, creates a private content-addressed preimage backup,
checks the revision again, and writes atomically. Backups remain on the Runner in
`runner-settings-backups/`; audit entries record field names and revisions, never
patch values. The Hub audit also omits patch values. Keep backups and the Runner
state directory private: backups contain the original configuration, including
credentials. The operator controls their retention.

```sh
python -m agent.runner_settings --config /path/to/config.json rollback \
  --expected-revision <current-sha256> --backup-revision <backup-sha256>
```

Rollback is another validated revision-checked write. Remote rollback cannot
restore identity, Hub/state paths, or its own management opt-in. An uncertain
save is not retried with a new operation/idempotency key: inspect the original
receipt and local configuration. Save success is reported as pending until a
subsequent live snapshot confirms the effective values.

## SSH configuration and discovery

The SSH section manages `enabled`, optional absolute `config_files` used for
inventory, named `targets`, and `max_timeout_seconds`. A target explicitly
approves one literal `host_alias`, its optional `config_file` (`ssh -F`), shell,
host-key policy, connect timeout and keepalive settings. Adding an alias in the
form generates the internal target key; VPS resource forms do not expose it.

Discovery reads the default user/system configuration and bounded static
`Include` files, plus configured custom files. A custom `-F` inventory excludes
the system configuration and resolves relative Includes from the user SSH
configuration directory. Host patterns and negations are not selectable aliases.
`Match`, `Match exec`, ProxyCommand, LocalCommand and KnownHostsCommand are never
executed during discovery; discovery does not invoke `ssh -G`, perform DNS or
open an SSH connection. Missing/unsupported/unreadable/over-limit parsing is
reported. Dynamic Includes cannot establish a complete target fingerprint and
are not accepted as a reviewed target; use a bounded static configuration.

A discovered Host is **not** automatically approved. One exact alias must map to
one explicit owner-approved target. A duplicate alias is ambiguous and unavailable.
Wildcard Host defaults do not become grants. Secrets, private keys, SSH-agent
sockets and raw configuration text never leave the Runner through discovery.

**Test connection** is a separate, explicit owner action. It uses BatchMode and
the normal strict/accept-new host-key policy, without passwords, sshpass or a
host-key bypass. That operation does execute the local OpenSSH connection rules;
the owner must trust those rules. Only bounded parsed system/architecture
metadata and a result classification are returned, never raw SSH diagnostics.
Provider/region are not guessed. Readiness and test time remain visible separately.
