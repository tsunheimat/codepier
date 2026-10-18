# Workspace refactor validation — 2026-10-18

## Latest hard-cutoff update

The user explicitly requires a direct cutoff, not retention or migration of any
former application model. This overrides any interpretation below that all old
positive tests must be restored or preserved. See the hard-cutoff contract in
`WORKSPACES.md`.

This change removes the three dedicated Role unit/integration/UI suites (593
lines), the old readable-App-alias expectation, the provider-ID OIDC callback,
saved workspace App notices/aliases and the dual-brand App binding. The current
review App and stable `/auth/oidc/callback` remain. Workspace initialization also
rejects retired tables or columns even under the current schema marker. Rejection
never mutates an existing database or tries to upgrade its tokens, projects or
permissions. No runtime compatibility helper replaces the deleted Role test helper.

The new `test_workspace_cutoff.py` and updated App retirement rejection tests run
in the focused workflow. **524 tests passed** in one completed combined pinned
Python 3.13 Linux run, including the existing 468 new-model/backend/browser cases
and 56 cutoff/catalog/retirement cases. No failures, errors, skips or deselections;
one upstream TestClient deprecation warning. Source checks, the configured mypy
scope, generated assets and packaging/credential-pattern checks also passed.
The actual new-model Chromium/WebKit browser cases remain in that combined run.
This update does not claim a new native Windows or completed GitHub Actions run;
the previous exact-head CI results below are historical evidence.

A fresh repository collection reports **3697 collected with 30 collection
errors**. The three-error reduction comes from deleting the obsolete Role suites,
not fixing three retained features. Remaining modules which import old helpers
must be retired if they assert removed behavior, or replaced with tests of retained
new-model features. Restoring the old helpers, dummy re-exports, migration paths,
skips and broad collection exclusions are not the resolution. Do not describe
these errors as a requirement for backward compatibility or the selected suite as
full-repository acceptance. PR #10 remains Draft; no merge or deployment occurred.

## Previous verification checkpoint

Status at the previous checkpoint: **new-model acceptance passes; full-repository
release acceptance remains blocked**. That record covers code/test commit
`43fe86421fef18c045c98b70a75fc26a67c22c85` on `refactor/workspace-abstract`, based on
main `ecd0753f8e8e6e1c37afb96845949f1f83d2f8f0`. Later documentation-only changes do
not represent an additional code validation run.

No production database, deployed Hub/Runner, actual VPS, or main branch was
modified. All Hub/Runner restarts mentioned below use disposable loopback test
processes and isolated data directories, never the deployed service.

## Implemented model

Roles and ambient Current Space are replaced by direct workspace membership
policies, workspace-owned Profiles and immutable Connection → Space + Profile
identities. Inventory resources are separate from workspace bindings. Each
Project references one workspace Folder plus a bounded subpath. Its execution
choices come only from mappings supplied by that Folder in the same workspace.
Default routing does not require agent discovery and does not silently fail over.

Whole VPS resources have a workspace binding, Runner and saved OpenSSH reference.
Top-level/project-only/both are discovery categories for the same resource, not
extra grants or a local project-directory sandbox. One-shot, persistent Shell and
PTY execution keep original target and connection identities. POSIX execution,
native Windows PowerShell sessions and Windows ConPTY have real-process tests.
The fresh schema refuses old databases; it does not migrate, delete or fall back
to the removed Role/password transport/runtime model.

## Fixes verified in this continuation

- **Workspace editor navigation:** modal dismissal used an asynchronous browser
  history traversal which raced the newly created workspace navigation, especially
  on WebKit. Save completion now awaits the actual traversal and checks its view
  and login context. Back/forward behavior, independent tabs, held clicks, stale
  drafts and explicit navigation are tested through real pages.
- **Native Windows ConPTY:** console writes have actual UTF-8 pipe receipts and
  suspended startup establishes process ownership before execution. Explicit
  console standard handles prevent redirected parent service/test streams from
  leaking into the terminal or causing premature EOF. Windows PowerShell and
  PowerShell 7 persistent state, cmd/PowerShell terminal input, resize and cleanup
  are verified on a real Windows CI worker. PTY tests require file effects rather
  than mistaking echoed input for command execution.
- **Cancellation:** the Runner referenced a nonexistent SessionManager attribute
  on cancel packets, disconnecting before termination. The correct original
  execution manager is now used. Local shell cancellation is terminal; SSH
  cancellation preserves `needs_review`, local cancelled flags and remote outcome
  uncertainty. Original-key observation and repeated cancellation do not rerun a
  command or turn an error receipt into success.
- **Regression fixture port:** SSH, VPS, VPS UI and local Shell tests now exercise
  actual workspace membership/Profile/Connection and whole-VPS bindings, not
  removed Role, default-space or password APIs. Coverage includes authorization
  before transport, injection/quoting, read-only access, cross-workspace denial,
  fixed versus live consent, concurrent updates, CSRF/Origin, private receipts,
  output redaction, Hub restart recovery, and revoked queued work never executing.

## Environments

The project's configured `remote-test` executor runs the selected suite on a
separate Linux test host using CPython **3.13.15** and hash-locked development
dependencies. Local WebKit uses a disposable browser library/sysroot wrapper;
no production system packages, browser profiles or user cookies were changed.
GitHub Actions uses CPython **3.13.16**, the same dependency lock, Ubuntu 24.04 for
backend/browser jobs and Windows Server 2022 for native execution acceptance.

Browser jobs install Chromium and WebKit with system dependencies. The seven
`test_frontend_core.py` cases use a real Chromium page and therefore belong in
that browser job, not a backend job without a browser installation. They are not
mock DOM or Node-only tests, and moving the job retains every assertion.

SSH tests execute a deterministic local process fixture through the actual
Hub/Runner OpenSSH adapter. They are not evidence of a live VPS, real keys,
ProxyJump, production NFS or a real identity provider.

## Observed results

| Check | Result |
| --- | --- |
| Combined new-model, routing, execution, SSH/VPS/Shell and browser suite | **468 passed**, no failures/errors/skips/deselections; one upstream TestClient deprecation warning |
| Backend/contracts portion of that suite | 433 cases, included in 468 |
| Real Chromium/WebKit workspace/VPS and Chromium core UI portion | 35 cases, included in 468 |
| Native Windows PowerShell/ConPTY acceptance CI job | Passed on Windows Server 2022; separate from the Linux run |
| Ruff over `agent hub shared scripts tests` | Passed |
| Existing incremental mypy gate | Passed; its configured scope remains 3 source files |
| Python compilation and JavaScript syntax | Passed |
| Generated panel assets | Passed, no outdated assets |
| Packaged-source / credential-pattern scan | Passed, no findings |
| Git whitespace checks | Passed |
| Full repository collection | **3652 collected, 33 collection errors**; not a full-suite pass |

The 468-case result is one completed run on commit `43fe864`, not the sum of
successive attempts. Earlier isolated counts of 300, 97, 36 and browser subsets
must not be added again. Portable Windows adapter tests also run on Linux and
must not all be described as native OS acceptance or counted twice.

The configured three-job GitHub workflow completed successfully for the same
code/test head:

- Run: https://github.com/tsunheimat/codepier/actions/runs/39125061114
- Backend/static job: `117668325261`, success.
- Chromium/WebKit/core UI job: `117668325262`, success.
- Native Windows job: `117668325032`, success.

These are **focused workspace checks**, not the repository's separate full
CodePier CI workflow. Earlier failures were reproduced and fixed, not relabelled:
WebKit navigation raced history; Windows redirected standard streams and a
PowerShell test command needed correction; cancel packets disconnected the Runner;
and browser cases were initially placed in a job missing the browser runtime.

## Reproduction

Use fresh development dependencies and browser runtimes; this does not upgrade a
live Hub or replace its data directory:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m playwright install --with-deps chromium webkit
.venv/bin/python -m pytest -q \
  tests/test_workspace_model.py tests/test_workspace_policy.py \
  tests/test_workspace_http.py tests/test_workspace_acceptance.py \
  tests/test_execution_workspace.py tests/test_workspace_integration.py \
  tests/test_workspace_panel_contracts.py tests/test_routing_contracts.py \
  tests/test_execution_policy_interpreters.py tests/test_session_windows_contracts.py \
  tests/test_external_plugins_contract.py tests/test_core_tools.py \
  tests/test_core_output_schemas.py tests/test_ssh.py tests/test_vps.py \
  tests/test_shell.py tests/test_workspace_browser.py tests/test_vps_ui.py \
  tests/test_frontend_core.py --junitxml=workspace-acceptance.xml
```

The native Windows job separately runs `tests/test_execution_windows.py` and
`tests/test_session_windows_contracts.py`, requires a real ConPTY API and installed
Windows PowerShell/PowerShell 7, and rejects a result with native acceptance skips.
See `.github/workflows/workspace-refactor.yml` for exact setup and artifact paths.

## Remaining release blockers

**Full repository fixtures and removed-interface tests still need deliberate
porting.** The prior checkpoint had 39 collection errors; the start of this
continuation had 36, and the latest collection has 33 after the SSH/VPS ports.
Remaining groups include direct IAM/identity, OAuth/OIDC, MCP Gateway,
conversations/session activity, project creation/privacy and removed Role/schema
fixtures. Tests which collect may still rely on old `tests/support.py` setup and
removed default-space/grant endpoints; collection alone is not behavioral proof.

Do not restore Role/password/migration adapters just to satisfy old tests, or
silently skip failures. Port meaningful behavior to explicit workspace fixtures,
retire tests of intentionally removed features with replacement coverage, and
complete the independent repository-wide CI gate. In particular, finish the IAM
fixture family before claiming OIDC/Gateway/browser acceptance through it.

**Deployment acceptance remains separate.** Real SSH credentials/hosts/ProxyJump,
production NFS, live OIDC providers and cross-platform service install/restart
have not been certified by the disposable execution/browser fixtures. Native
Windows and WebKit are no longer unimplemented or unrun items, but their focused
passes do not certify every deployment configuration.

Keep this PR Draft. Use a separate empty `HUB_DATA_DIR` for manual workspace
evaluation. Never point this breaking schema at the existing Hub data volume,
and never delete source folders, shared storage or VPS contents as part of a
configuration/database initialization.
