# Workspace abstraction — fresh-start model

This build uses a fresh `workspace-2` database. It does not convert, open with
fallbacks, or delete the former database. Use a separate empty `HUB_DATA_DIR` and
initialize new connections. Do not deploy this schema over an existing Hub
data volume. Working folders, NFS exports and VPS files are not database state and
are never deleted by choosing a new database.

## Hard cutoff, not backward compatibility

The workspace architecture is a direct replacement, not an additive upgrade.
There is no requirement to preserve former Role/grant semantics, ambient Current
Space, device/root Project records, old authorization URLs or saved workspace
App cards. Do not add migration adapters, import aliases, fallback identities,
inert compatibility resources or flags which restore the former model.

Old databases and mixed old/new schemas are refused without modifying them. Even
a current schema marker does not permit retired model tables or columns. New
connections must be created against a new workspace database. Refusal to delete
an unrelated existing data volume is a safety boundary, not legacy support.

Old tests which require a removed feature to keep working must be deleted, not
made green by restoring that feature or by disguising an old fixture as the new
model. The dedicated Role unit/integration/UI suites are removed. Their former
migration and Role-specific behavior is not a release requirement. Continued
features are tested directly against workspace members, Profiles, Connections,
source bindings and the new execution model; cross-space denial, privilege limits,
private receipts, revocation and no-replay behavior still require real tests.

`tests/test_workspace_cutoff.py` enforces absent APIs/modules, rejection of former
authorization fields, refusal of retired database structures, and removed App
URI/metadata aliases. OIDC has one callback, `/auth/oidc/callback`; the former
provider-ID callback is not a redirect or compatibility endpoint. The fixed-review
App uses only `ui://codepier/changes-v1.html` and `com.codepier/binding`.

Remaining old test modules are cleanup/replacement work, not a mandate to preserve
old runtime behavior. Do not infer a full-suite pass merely from removing obsolete
tests, and do not disable tests of retained features to conceal a regression.

## Objects and identities

Global Resources register Devices, Folders, Storage Pools, VPS metadata and external MCP
connectors. A Space is a workspace containing explicit bindings to those resources.
Members receive paired resource/action policies directly; there is no Role table,
Role editor or Role grant mode. Profiles are workspace-owned connection policies.
A Connection has its own immutable ID, workspace ID and Profile ID. Token renewal
keeps that identity. Another Connection using the same Profile still has separate
private operations and execution sessions.

Resource management, membership management and resource use are separate direct
member capabilities. A Profile cannot carry management capabilities or delegate
beyond the member's current rights. Fixed connections retain their explicit consent
ceiling, including target/mapping identity; live connections follow Profile changes
only after explicit consent. Both recheck current member rights and local Runner
constraints. Profile changes cannot secretly rebind a Connection to another Space.

There is no account/session “Current Space”, selector header or default-Space
fallback. Open `/#spaces/<id>` directly; multiple workspace pages do not mutate a
shared login state. Resource detail URLs derive context from their own IDs. MCP
arguments never choose a Space; the authenticated Connection supplies it internally.
Project discovery remains `project_query`, with logical paths and ordinary actions.

## Setup order

1. Initialize a separate Hub database and pair Devices. Local root authorization
   is still configured on each Runner; a workspace binding cannot expand it.
2. Under **Resources**, define a **Folder** (one Device and absolute directory)
   or a **Storage Pool** (logical base and existing mount mappings). Pool creation
   validates its mappings and required file/shell defaults atomically. Confirm
   each mapping exposes the same files; this does not mount or synchronize data.
3. In **Workspace → Folder / Storage Pool**, import either resource as one source.
   Select a root/subdirectory, read/write/execute capabilities and, for a Pool,
   the provided mappings. No wrapper Folder or separate Device assignment is needed.
4. Pool imports independently inherit file and shell defaults or override them.
   Excluding an inherited default requires an eligible override; it never imports
   the missing Device or silently selects another mapping.
5. Create each Project with exactly one `source_id` and relative `subpath`.
   `.` uses the source root. Project shell overrides can only narrow its source.
6. Configure direct member policy and Profiles, then create a Connection.

Device allocation is independent. Importing a Folder/Pool authorizes only that
source's scoped operations; it does not create `space_devices` entries or grant
`devices.read`. Removing an import leaves global inventory, independent Device
bindings and physical files intact. A VPS launcher is likewise a validated technical
reference, not a prerequisite whole-Device grant. Only the inventory owner or
instance administrator can select those resources during workspace configuration.

Pool defaults are `default_fs_device_id`, `default_shell_device_id` and
`shell_allowed_device_ids`. An import stores `files` and `shell` settings as
`inherit` or `custom` (shell also supports `disabled`), rather than copied defaults.
An empty extra shell allowlist includes all eligible mappings within that source.
An omitted/empty mapping selection imports the Pool's currently enabled mappings;
new mappings do not silently expand an existing import. The editor selects these
explicitly and requires at least one mapping.

Pool inventory membership does not make every mapping available in a workspace.
A Project sees only devices provided by its selected workspace source.
A device elsewhere in the same workspace without that source's mapping is not an
execution option. An empty additional allowed-device selection means all eligible
provided mappings, not all devices in the Hub. Disable shell explicitly instead
of using an ambiguous empty list. Every active shell source needs a valid default.

A storage binding can supply A/B while pool inventory also has C. Projects using
that binding cannot select C. Project custom settings can narrow the workspace
choices or change the default within them; they cannot import a new device, folder
or mount. Saving configuration validates dependent projects and uses versions to
prevent concurrent edits. Filesystem validation may return `VALIDATION_PENDING`:
observe that operation and repeat the same save/key, not a newly created request.

## Path and command resolution

For logical base `/storage/dev`, source subpath `team`, Project `codepier` and `cwd=tests`,
Device A mounted at `/srv/dev` executes in `/srv/dev/team/codepier/tests`.
The caller passes project-relative `cwd`; the Hub composes and validates it.
An abstract path is not automatically an actual filesystem mount on every Runner.
Do not rewrite arbitrary command strings or stdout using substring replacement.
Programs in shell commands should normally use relative paths under resolved cwd.

Shell default precedence is **Project override → source override → Pool default**.
Files use **source override → Pool default**. A direct Folder always uses its own
Device. There is no workspace-wide shell override to bypass source restrictions.
Defaults are resolved live; Pool edits that would invalidate an enabled import or
Project are rejected atomically. File and shell availability are independent.
An offline default fails its operation without failover. Ordinary calls omit
`execution_device_id` and do not discover devices first.

`get_execution_options(project, action)` returns only the authorized source's
choices and logical/physical path descriptions. Its instructions limit use to an
explicit user request to inspect or select execution location. This is model usage
guidance, not proof that a model never calls a discoverable tool unprompted. The
Hub does enforce the configured default when no override is supplied and checks
every explicit override against the workspace source.

## Connection and OAuth setup

Authorize in two separate steps: select a workspace, then select one of its
Profiles. Changing workspace clears the Profile selection and increments the
pending selection revision. Profile creation opens that workspace's editor and
returns to the original pending authorization; creating a Profile does not grant
consent. Confirm the displayed current policy before issuing credentials.

The MCP scope is `codepier.workspace`. `/mcp` rejects old authorization/role/space/
profile query selectors. No Profile or workspace binding means no Connection;
there is no fallback to a user's general account rights. A browser navigating to
another workspace does not change existing MCP tokens. Use a new Connection and
fresh authorization to choose another workspace or Profile.

`get_profile` returns this Connection's stable identity (not the shared Profile's
ID). `get_access_context` reports Connection, permission Profile, current scopes
and visible projects, without exposing a workspace selector.

## Whole VPS resources and display

Register VPS metadata in Resources, then assign the whole VPS to a workspace with
its Runner and saved SSH reference. Top-level SSH does not require a Project or a
local Project folder. The remote OS account still determines OS access; whole-VPS
assignment is not a root privilege escalation.

Set `display_mode` to `top_level`, `project_only` or `both`. Project modes require
one or more same-workspace Projects. This controls web/MCP discovery, not separate
authority: both entries use one Space VPS ID and one Connection policy. A Project
entry does not constrain remote SSH to the Project's local directory. `vps` lists
top-level entries; `vps(project=...)` lists that Project's entries. Execution uses
`target="vps:<space-vps-id>"` through the saved Runner.

## SSH and stateful execution

The Runner's `ssh.targets` maps a saved reference to an OpenSSH Host alias and an
optional absolute custom config file. OpenSSH owns IdentityFile, ssh-agent,
ProxyJump and known_hosts resolution; credentials are not uploaded to the Hub.
There is no password/sshpass compatibility adapter and no custom SSH-config parser.
Host-key checking defaults to strict. The Hub selects a registered reference,
not arbitrary hostnames or SSH options from model arguments.

```json
{
  "ssh": {
    "enabled": true,
    "targets": {
      "production-server": {"host_alias": "production", "shell": "sh"}
    }
  },
  "sessions": {
    "enabled": true,
    "max_sessions": 16,
    "idle_seconds": 1800,
    "lifetime_seconds": 86400
  }
}
```

Merge this into the existing Runner configuration; keep its identity, secret and
root authorization. Local sessions also require allowed shell execution. Do not
turn off command-policy controls merely to make a session open: stateful shells
cannot implement a static per-command restriction and explicitly refuse that case.

Use `exec` and `process` for one-shot calls. Use `session` for open/exec/read/write/
resize/signal/status/close. Persistent commands share their original cwd/environment
and are serialized; per-command output is length-framed, not parsed from prompts.
PTY input/output is a bounded terminal byte stream with cursor/loss reporting,
not per-command exit status. Mutating input requires an idempotency key.

Ordinary opens omit `shell`: SSH uses the shell saved on its Runner target;
local sessions default to `sh` on POSIX and Windows PowerShell on Windows.
Explicit SSH shell selection must match that saved target.
The resolved shell is returned in Session status. Opening a Session does not apply
the per-command timeout to the lifetime of the shell; setup is bounded separately,
and each persistent command obeys the Runner command ceiling. Tightening Runner
Session/input policy blocks new and queued command/PTY input, while observation
and close continue to address the original handle.

Local persistent shells and PTYs use real POSIX processes on Unix. Windows
persistent sessions use native Windows PowerShell or PowerShell 7 with framed
per-command output. Interactive Windows sessions use native ConPTY with `cmd`,
Windows PowerShell or PowerShell 7; console input is UTF-8 and successful writes
report the bytes actually accepted by the pipe. Suspended startup establishes
process ownership before user code runs, and child trees are contained in a
Windows Job Object for timeout/close cleanup. Unsupported mode/dialect pairs fail
explicitly rather than pretending to retain shell state. SSH uses the same tool
interface with its saved POSIX remote shell.

Native Windows acceptance exercises actual console input and file effects,
framed command state, resize, timeout, policy changes and cleanup. It is separate
from the existing native-CLI terminal subsystem and from portable mocked contracts.
Passing it is not certification of every Windows service installation or real
remote SSH configuration.

A session stays pinned to its original target/connection. Runner loss or restart
reports a lost session, never silently creates a replacement. SSH timeout,
cancellation or client termination cannot prove the remote process and all its
side effects stopped. Observe original operation/session IDs and report uncertainty.
RCH workers are not automatically storage mappings or SSH launchers; configured
test hooks remain separate from the selected CodePier execution device.

## Development verification boundary

Focused tests exercise fresh schemas, direct policies, source inheritance, OAuth
steps/renewal, real local processes, actual Hub/Runner transport and fake OpenSSH
fixtures. Real Chromium/WebKit test cases also exercise independent workspace
pages, folder-bounded Shell choices, two-level OAuth consent, profile creation and
return, and all three VPS display modes through actual HTTP/MCP calls. Browser
runtime availability is reported separately; template tests are not browser passes.
Native Windows Session acceptance runs on a Windows CI worker; browser cases run
in both Chromium and WebKit. No production NFS, external VPS, live IdP or deployed
upgrade acceptance is claimed by these disposable fixtures. The [earlier validation record](WORKSPACE_REFACTOR_VALIDATION.md)
describes the original workspace refactor checkpoint, not subsequent source changes.

The repository-wide regression suite uses current-schema fixtures. Run
`scripts/check_full_regression.py` against the source being accepted; focused
success is not full CI success. Development validation does not deploy a release
or automatically upgrade a database.

## Source API and local validation

Inventory uses `/api/folders` and `/api/storage-pools`; Pool creation includes
`mappings` and explicit defaults. Workspace imports use
`/api/spaces/{space_id}/sources`, with `source_kind=folder|storage` and exactly
one `folder_resource_id` or `pool_id`. Projects and delegated creation use
`source_id`; scoped delegation uses `source_rules` with exact `sources` IDs.
`project_query(operation="sources")` lists only authorized workspace sources.
The former workspace `/folders`, `/storage` and `/shell` APIs, `folder_id`,
`folder_rules` and `workspace-1` databases are rejected; there are no aliases or
migrations. Versions, original validation receipts and fixed Connection target
fingerprints still fence changes across asynchronous saves.

A POSIX host with system OpenSSH can run the disposable real-SSH lab:

```sh
.venv/bin/python scripts/check_openssh_execution.py --output /tmp/new-openssh-proof.json
```

It creates temporary loopback sshd servers, keys, known_hosts and ssh-agent state,
then exercises direct keys, ssh-agent and ProxyJump through Runner one-shot,
persistent-shell and PTY execution. This proves those real transport paths locally;
it does not certify production NFS mounts, a specific VPS or native Windows SSH.
The optional CodePier-managed ControlMaster pool remains deferred.
