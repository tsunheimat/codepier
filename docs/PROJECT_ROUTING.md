# Shared NFS projects, action routing and RCH

CodePier can manage a **logical project independently of one device**. A space registers a shared NFS source, records the paths where its devices already mount that same storage, and configures default action routes. A project selects a bounded subfolder and optionally overrides routes or test recipes. The working agent remains the caller; this does not add an autonomous CodePier agent.

This model assumes one shared filesystem. CodePier does not mount NFS, copy working trees between devices, or synchronize separate clones. The administrator must verify that registered mounts really expose the same storage. Path names alone do not establish that relationship.

## Objects and compatibility

| Object | Purpose |
| --- | --- |
| Source root | A reusable logical name for shared storage in one space. |
| Source mount | One device's validated absolute path to that storage, with read/write and task ceilings. |
| Logical project | Project identity, source-root reference, relative subfolder, and project permissions. |
| Action route | A reusable device/provider selection for one action family. |
| Space default | Default route for an action family; not a permission grant. |
| Project override | Inherit the default, select another route, or explicitly disable that action. |
| Execution settings | Administrator-owned test recipes and bounded planning hooks. |

Legacy projects keep their existing device/root behavior until explicitly adopted. The migration makes the stored project location nullable and adds routing tables; it does not automatically move projects, expand grants, or change running device configurations.

Managed projects store `device_id=NULL` and `root=NULL`. For compatibility, public project projections may show the **file route's** device and path. Those display fields are not project ownership or a fallback executor. `execution_routes` and `project_query(operation="routes")` describe the actual per-action selections.

Detaching a source retains the logical project, its access policies, external plugin attachments, NFS files, devices and credentials. Source-dependent actions then fail with `PROJECT_SOURCE_UNBOUND`; there is no implicit fallback to the old device. Disabling a mount also preserves the underlying device and storage.

## Panel setup

Use **Resources → Projects**. The shared-NFS/routing sections are visible to space administrators.

1. Add a shared source, for example `development-share`.
2. Register an existing mount for each required device. The built-in node might use `/mnt/vibe-coding-share/develop`; a development device might use `/srv/develop`. Explicitly confirm that they represent the same NFS storage. Each path is validated through that device's local authorization, not the Hub's filesystem.
3. Create action routes and select the space defaults. A typical setup uses the built-in node for `files`, a development device for `git` and `shell`, and that device's RCH launcher for `test`.
4. Browse a source's authorized subfolders and select several to create projects in a single batch. Alternatively, use **Adopt shared source** on an existing project. Configure route permissions before using adopted projects from a client.
5. Select **Role authorization** on each route. Grant only the required route/project pairs, in addition to the project's ordinary read/write/execute permissions. Configure space/project recipes and hooks as needed.

Source/mount/route edits use expected versions. Batch creation uses an idempotency key and one database transaction: an alias collision prevents a partially-created batch. Directory validation can return a pending operation. Continue the same save with unchanged fields after observing that validation; do not submit a new batch with a new key to recover an uncertain result.

Route defaults are resolved per action. A disabled project override does not fall through to another device. No unavailable route silently falls back to an online device. Only explicitly routed managed projects use this model.

### Supported action families

| Family | Uses |
| --- | --- |
| `files` | File tools, directory/context/skills reads, source edits and related source operations. |
| `git` | Explicit Git execution and existing Git/worktree primitives. |
| `shell` | General commands, legacy named tasks, execution information and existing validation tools. |
| `test` | Explicit direct-argument or configured test execution, optionally through RCH. |
| `ssh` | SSH/VPS execution through its selected launcher device. Existing separate VPS authorization still applies. |
| `browser` | Browser session operations. |
| `computer` | Native desktop-control operations. |
| `native` | Native CLI session operations. |

Browser/computer leases, searches, backups and native session identifiers remain tied to their original device. Changing a route does not transfer an existing session. Managed `workspace_id` worktrees remain node-local and are explicitly refused for routed project actions; use the shared project source view. This is not cross-device worktree replication.

## Agent/client calls

Inspect authorized routes and configured tests:

```json
{"operation":"routes","project":"codepier"}
```

```json
{"operation":"tests","project":"codepier"}
```

Both are `project_query` operations. A file edit still uses the normal `read`, `write` and `edit` tools; the Hub selects the file route.

Run Git on the Git device:

```json
{
  "project": "codepier",
  "action": "git",
  "argv": ["git", "status", "--porcelain"],
  "idempotency_key": "git-status-example-001"
}
```

Run a test on the test route:

```json
{
  "project": "codepier",
  "action": "test",
  "argv": ["python3", "-m", "pytest", "-q"],
  "timeout_seconds": 1800,
  "yield_seconds": 0,
  "idempotency_key": "test-example-001"
}
```

These are `exec` arguments. `action="git"` requires argv beginning with `git`. Git/test actions do not accept a shell command string. Paths in their `cwd` are relative to the selected source view. A configured test can instead use `action="test", task="unit"`; `command`, `argv` and `task` are mutually exclusive.

**Action labels are routing contracts, not a shell sandbox.** An ordinary `exec(command="pytest ...")` still uses the shell route. CodePier does not guess a command's meaning with a regular expression or intercept arbitrary commands inside scripts. A client that may run unrestricted shell or Git commands can use that operating-system authority; do not grant that authority when only the controlled test route should be available.

Use the returned operation ID with `task_query`. Reusing the same idempotency key means observing the same original request, not requesting another test run. Results carry the selected route and provider, and project dashboards include authorized results across devices without implying the current source has been reverified.

## Hooks and test recipes

Space execution settings and project execution settings use the same schema:

```json
{
  "hooks": [
    {"name":"remote-tests","actions":["test"],"handler":"require_rch"}
  ],
  "tests": [
    {
      "name":"unit",
      "argv":["python3","-m","pytest","-q"],
      "cwd":".",
      "timeout_seconds":1800,
      "description":"Project unit tests"
    }
  ]
}
```

The hook phase is `before_dispatch`. Built-in handlers are `require_rch` and `deny`. Hooks are pure planning rules; they do not execute scripts or run the command themselves. Space hooks are mandatory, then route and project hooks are applied. Conflicting RCH requirements fail closed. A later project setting cannot undo a space denial or mandatory RCH requirement. Repeated matching RCH hooks still produce one execution plan, not duplicate executions.

Project test recipes can override a space recipe with the same name. Space/route hooks continue to constrain the resulting plan. Configured tests reject caller cwd/environment overrides and use the lower of the request and recipe timeouts. No hook or recipe is automatically loaded from an agent-editable repository file.

## RCH launcher setup

Install the supported RCH fork on the **selected external execution device**, along with its normal daemon, approved workers and SSH configuration. RCH workers do not all need to be CodePier devices. The built-in Hub file node does not gain shell, task or RCH access.

The device owner must opt in through its Agent configuration. Merge the following section into the existing configuration; do not replace its device identity, secret or allowed roots:

```json
{
  "rch": {
    "enabled": true,
    "executable": "/usr/local/bin/rch"
  }
}
```

The relevant allowed root and project must permit tasks and writes, and the device's `shell` policy must explicitly authorize this project. Enabling `rch` alone grants none of those permissions. The executable is an absolute deployment-owned path; the Hub never uploads an executable or reads the SSH private key.

A test route can use:

```json
{
  "provider":"rch",
  "rch":{
    "mode":"job",
    "require_remote":true,
    "worker":"",
    "profile":"",
    "required_tools":[],
    "result_dirs":[]
  },
  "hooks":[]
}
```

Job mode produces `rch exec --job -- <argv>`; compilation mode produces `rch exec -- <argv>`. CodePier always sets `RCH_REQUIRE_REMOTE=1`. Client environment overrides are rejected on RCH tests. Optional worker/profile selections map to `RCH_WORKER` and `RCH_PROFILE` (the documented profiles are `dev`, `prod`, and `test`). The selected device's trusted environment and RCH configuration still govern its approved worker pool.

`required_tools` and `result_dirs` apply only to job mode. Result directories must be bounded relative directories, not the entire source root or `.git`. RCH owns its normal source transfer and result collection. CodePier does not add separate-clone synchronization or claim that all RCH build workers share NFS. The NFS assumption applies to the CodePier devices' source views.

Before launching the test, the adapter checks the installed executable's strict-remote capability and verifies isolated durable-state handling through a read-only `jobs` probe using a temporary terminal marker. Failure starts no test and does not fall back locally. The probe does not create a real remote job. Each actual operation then gets its own private `RCH_STATE_HOME` under the Agent state directory, retaining the exact wrapper/build/worker identity.

### Recovery and cancellation

RCH recovery is a specialized `process` operation:

```json
{
  "operation":"rch_job",
  "project":"codepier",
  "idempotency_key":"recover-original-001",
  "options":{
    "original_operation_id":"<original 32-character CodePier operation ID>",
    "action":"recover",
    "timeout_seconds":30
  }
}
```

Supported actions are `observe`, `recover` and `cancel`. They use the retained RCH wrapper ID with `jobs attach`, `jobs recover` or `jobs cancel`. They never replay the original argv. Access remains private to the original caller/connection and requires current project/route permission and node-local authorization. A different route, source or missing identity is rejected rather than inferred. After changing those mappings, an administrator may need to inspect the original job directly on its original launcher; CodePier will not route recovery to a replacement device.

`process(operation="cancel")` cancels the CodePier node process, not proof of remote SSH termination. If RCH has not acknowledged a terminal outcome, the result is `needs_review` with `remote_execution_uncertain=true`. Use the original RCH control operation. A cancellation acknowledgement or recovery request alone is not necessarily terminal completion; inspect the returned lease state.

Routed operations on overlapping subfolders of the same registered source coordinate at the Hub, independently of device mount-path spelling. Unknown RCH outcomes retain a source hold; unrelated sibling projects can continue. Same-job recovery bypasses that hold, and verified terminal acknowledgement releases it. The administrative release-source endpoint requires explicit confirmation that remote work stopped; it does not kill a remote process or turn an unknown result into a successful test.

This coordination covers routed CodePier operations. It is **not an NFS distributed lock against arbitrary local processes, native agents, another Hub, or legacy mappings of the same files**. Do not edit the same tree through an uncoordinated writer during a test that must certify stable bytes. This release does not produce a cryptographic source-content receipt or a snapshot of every possible external dependency.

## Authorization and upgrades

Project permission and route permission are separate. A route rule selects an exact route ID and project IDs, or explicitly opts into all present/future projects. Route selection never grants general device-management access. Fixed connections expand their route/project choices at issuance and retain a source/route/settings snapshot; later role or configuration changes cannot broaden that connection. Dynamic-role connections follow their explicitly consented live policies.

Permission checks run before admission, before delivery, and when disclosing receipts. Changing a route, source or hook while work is queued never redirects the stored operation. Accepted/ambiguous work is probed through its original journal. Disabling a route does not silently kill already-running external work.

Every device serving managed source views must advertise `project_routing_protocol=1`. Older nodes are refused before new routed operations are sent. `routed_exec` and `routed_job_control` have distinct wire contracts. Update the Hub and selected Agents together; unchanged legacy project calls retain their existing semantics.

Back up the Hub database and matching key material before deployment. The transactional `project_routing_schema=1` migration preserves existing project IDs, indexes, triggers, policies and receipts while introducing nullable locations and new routing state. A source-code rollback alone is not a supported database downgrade; restore a matching backup or use a compatible version.

## API and developer verification

The panel-session/CSRF-protected administration APIs include:

- `/api/source-roots/{name}` and `/api/source-roots/{id}/mounts/{device_id}` for shared storage inventory; `/browse` for bounded subfolder discovery.
- `/api/action-routes/{name}`, `/api/project-routing/defaults/{family}`, and `/api/project-routing/execution` for space configuration.
- `/api/project-routing/projects` for a batch create, and `/api/projects/{id}/source`, `/routing/{family}`, `/metadata`, `/execution` for logical-project management.
- `/api/project-routing/operations/{id}/release-source` for explicit administrative release of an uncertain source hold.

Read `/api/project-routing` for the administrator's inventory and `/api/projects/{id}/routing` for a project's authorized route projection. Administration is not exposed as an arbitrary model-callable configuration tool.

Relevant implementation modules are `hub/project_routing.py`, `hub/routing_api.py`, `hub/routing_schema.py`, `hub/routing_policy.py`, `shared/routing_contracts.py`, `shared/execution_hooks.py` and `agent/routed_execution.py`.

Tests use temporary real Hub/Agent processes, built-in IPC and a deterministic RCH protocol fixture. They exercise shared bytes, permissions, subfolder boundaries, uncertainty, no replay, browser forms and forward database migration. They are not proof of a deployed NFS server, live RCH fleet, remote Git push, real VPS, native desktop, Windows/macOS service installation or production upgrade.

```bash
.venv/bin/python -m pytest -q tests/test_project_routing.py tests/test_routing_contracts.py tests/test_routing_security.py
.venv/bin/python -m pytest -q tests/test_project_routing_browser.py
.venv/bin/python -m ruff check agent hub shared scripts tests
.venv/bin/python scripts/build_web_assets.py --check
.venv/bin/python scripts/check_release.py
```

See [Development](DEVELOPMENT.md), [resource/access model](RESOURCE_MODEL.md), [external plugins](EXTERNAL_PLUGINS.md), [VPS](VPS.md) and [long-operation recovery](LONG_OPERATIONS.md) for the existing boundaries.
