# Runner-derived VPS resources — 2.1 cutoff

The resource chain is **Runner SSH approval → VPS Resource → Space selection →
member/Profile/Connection**. Only Devices manage SSH configuration and discovery.
A Space consumes the persisted VPS inventory; it neither re-enters a Runner/SSH
alias nor refreshes an SSH configuration already owned by its parent.

## Configure once, then select

In **Resources → Devices → Full management → Runner settings**, enable SSH and
approve the exact aliases the Runner may use. Existing SSH keys, `ssh-agent`,
`known_hosts`, ProxyJump and OpenSSH configuration remain on that Runner. Remote
settings writes still require the Runner owner's local opt-in; see
[Runner settings](RUNNER_SETTINGS.md).

When a current Runner connects or reloads its configuration, its authenticated
inventory report automatically creates one VPS Resource per approved alias. The
initial resource name is the alias itself. Discovery alone does not authorize a
host and does not create a resource for an unapproved candidate. Different
Runners may use the same alias and produce separate resources.

**Resources → VPS** shows these derived resources. Rename them, edit descriptions
or disable them here; later reports preserve those choices. There is no manual
VPS registration form or `POST /api/vps`. Technical identity cannot be edited here:
change SSH configuration in Devices. To remove a derived resource permanently,
first revoke its alias on the Runner and remove any workspace bindings. Disabling
or removing a database resource never deletes remote files or stops remote jobs.

## Workspace list editor

Open **Space → VPS**. Each parent resource is one row with a selection checkbox,
workspace display alias, optional description and display position. Positions are
`top_level`, `project_only` or `both`; the latter two require Projects from this
same Space. Filter rows, select several and **save once**. Project categories are
presentation, not a remote-directory sandbox. The workspace alias is not the SSH
Host alias and does not change the connection target.

**Space → Devices** uses the same inline selection pattern with a workspace alias.
Device allocation remains independent of Folder/Storage Pool source mappings; it
does not grant project-free local Shell access or change a Project's execution
node. A VPS's Runner is a technical dependency, not a separate Device allocation
that must be repeated in the Space.

Batch saves are atomic, revision-checked and idempotent. Invalid rows roll back
the complete save. A concurrent editor cannot overwrite a newer selection. An
uncertain save is not automatically retried; reload the saved list to reconcile.
Deselected bindings are disabled while original session/receipt identities remain
available for authorized recovery. Explicit removal is still restricted when
session records reference a binding.

## Selection means Shell permission

VPS policy has no separate `read` / `execute` actions. A rule is either:

```json
{"vps": ["workspace-vps-id"]}
```

or the explicit future-resource choice:

```json
{"all_vps": true}
```

The Profile editor offers **全部当前及未来 VPS（仅此工作空间）**. It saves a selector,
not a list of today's IDs. When all applicable policy ceilings select all VPS,
future VPS assigned to that Space are available through the same Connection.
Explicitly selected VPS still remain a finite set. Empty selection grants none.

This all/future VPS choice is explicitly preserved even in a fixed Connection;
it is not permission for future Projects or arbitrary Profile changes. Finite
fixed selections retain original target fingerprints. Every operation still
intersects current member rights, current Profile and the Connection's consent,
and checks the Space, parent resource, Runner identity and local SSH approval.
A Profile cannot reach another Space, authorize an unapproved host, enable SSH,
or bypass a disabled resource. Only workspace administrators may author future
resource selectors; delegated Profile managers cannot expand their member rights.

MCP discovery exposes `shell_access`, `can_execute`, and parent status, not the
retired VPS action list. `target="vps:<space-vps-id>"`, one-shot `exec`, persistent
Shell and PTY retain their original targets and no-replay semantics.

## Parent status, not lower-layer scanning

Resources and Spaces show the recorded upstream status: configured, disabled,
withdrawn, unavailable/unverified, or Runner offline. An offline resource can be
selected without pretending it is connected. Merely opening a Space, saving a
selection, renaming a resource or re-enabling a resource does not invoke SSH
scan/probe. The old five-minute discovery gate is removed from those actions.

Use **Devices → Runner settings → SSH Host discovery / explicit Test connection**
to inspect or refresh the actual SSH configuration. Static discovery never runs
`Match exec`, ProxyCommand, DNS or a connection. An explicit test invokes native
OpenSSH and records bounded metadata and diagnostics. Space/Resource views inherit
that observation; they do not rerun it. `configured` means configuration is
available, not proof of connectivity; a successful test remains a timestamped
observation, not a continuous health guarantee.

A revoked alias becomes withdrawn rather than silently disappearing or changing
IDs. SSH configuration/target changes advance the target identity and fence old
queued or session input. Original status/output/cleanup requests do not recreate
or replay work. Stopping a local SSH client does not prove remote effects stopped.

## Automatic startup data cutoff

Current marker: `vps_schema=runner-inventory-2` inside `workspace-2`.
**No new environment variable or operator migration command is required.**

For the known `resource-owned-1` layout, startup converts stored data in one
transaction: it adds upstream observations and Device aliases, removes VPS action
classifications, and preserves resource IDs, custom names, Space bindings and
non-VPS data. Former VPS rules that included `execute` become whole-VPS selections.
Former read-only VPS rules are dropped, never silently elevated to Shell. Review
and explicitly select those VPS again when Shell access is intended.

For unchanged Profiles that explicitly selected all VPS and whose current active
member policy also allows all VPS, the cutoff repairs the old accidentally
expanded fixed snapshot into an all/future selector. A changed Profile, narrower
member policy or explicitly finite selector does not acquire a wildcard. Original
Connection identity remains unchanged. The first identical current Runner report
marks an existing resource configured without inventing a new target version.

The earlier known Space-owned VPS layout with no marker receives the existing
backed-up, VPS-only destructive reset automatically. Its old VPS identities and
related policy/receipts are discarded; current Runner reports then repopulate
resources. Other workspace schemas and unknown/mixed/future versions are not
guessed or erased. A malformed policy or failed foreign-key check rolls back the
transaction instead of leaving a partially converted schema.

Users, devices, projects, storage, unrelated operation/session records, the master
key and actual local/remote files are preserved. No legacy API, policy adapter or
runtime fallback is retained. Update both Hub and external Runners to this build;
an old Runner without inventory reporting is shown as requiring an update, not
silently trusted. Back up the complete database and matching key before deployment.
Source commits and successful tests do not deploy or reset a production database.
