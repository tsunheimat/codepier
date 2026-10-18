# Workspace refactor validation — 2026-10-18

Status: **development checkpoint, not release-ready**. This record applies to
`refactor/workspace-abstract`, based on main `ecd0753f8e8e6e1c37afb96845949f1f83d2f8f0`.
No production database, deployed Hub/Runner, actual VPS, or main branch was changed
by the validation described here.

## Implemented model

The branch replaces Role-based authorization and ambient Current Space with direct
workspace membership policies, workspace-owned Profiles and immutable
Connection → Space + Profile identities. Resource inventory is separate from
workspace bindings. Projects reference exactly one workspace Folder plus a bounded
relative path, and only choose execution devices supplied by that Folder in that
workspace. Shared Storage has logical/physical mappings; shell defaults and allowed
choices live in workspace resource configuration. Explicit selection is optional,
ordinary execution uses its configured default, and unavailable defaults never
silently select a different device.

Whole VPS resources use a workspace binding, Runner and saved OpenSSH target
reference. Top-level/project-only/both are discovery categories for the same
resource, not duplicate grants or a project-directory sandbox. Local/SSH one-shot
execution and POSIX persistent Shell/PTY sessions use separate target/backend and
lifecycle components. The new schema refuses old databases rather than migrating,
deleting, or falling back to the removed runtime model.

The continuation also fixes two reproduced Session defects: omitted SSH shell
selection now resolves the saved target's shell instead of forcing `sh`; opening
a retained shell is not rejected by a lower per-command timeout. New/queued input
rechecks tightened Runner Session policy without disabling observation or cleanup.

## Reproduced environment

Tests run through the project's configured `remote-test` executor on its dedicated
Linux test environment, from the isolated refactor worktree. CPython **3.13.15**
was installed into `.work/verify-py313`; dependencies were installed using
`uv pip install --require-hashes -r requirements-dev.txt`. Test databases, local
Hub/Runner processes, browser cookies and OpenSSH process fixtures are synthetic
and disposable. The SSH fixture executes local commands; it is not a real VPS.

## Results

| Check | Observed result |
| --- | --- |
| Combined new-model, routing/interpreter and Chromium acceptance | **145 passed, 7 deselected**, 1 upstream TestClient deprecation warning |
| New-model backend, HTTP, source and execution suite alone | **88 passed** |
| Actual Chromium workspace/OAuth/VPS browser cases | **7 passed** |
| Ruff: `agent hub shared scripts tests` | Passed |
| Existing incremental mypy gate | Passed; its configured scope is 3 files, not the entire application |
| Generated panel assets check | Passed, no outdated assets |
| Packaged-source release check | Passed; no credential-pattern findings |
| Repository whitespace check | Passed |
| Full repository collection on pinned Python 3.13 | **3369 collected, 39 collection errors** |

The backend-only and Chromium counts are subsets of the combined 145 and must not
be added again. The seven deselections in the combined run are the WebKit cases,
explicitly excluded because this test host cannot launch its WebKit binary. An
earlier attempt ran the original three cases for each browser: all three Chromium
cases passed, while all three WebKit setups failed with missing `liblcms2.so.2`.
No WebKit case is counted as passed and no persistent skip was added to the tests.

### Reproduction

Use a fresh Python 3.13 environment with the locked development dependencies:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest -q \
  tests/test_workspace_model.py tests/test_workspace_policy.py \
  tests/test_workspace_http.py tests/test_workspace_acceptance.py \
  tests/test_execution_workspace.py tests/test_workspace_integration.py \
  tests/test_workspace_panel_contracts.py tests/test_routing_contracts.py \
  tests/test_execution_policy_interpreters.py tests/test_workspace_browser.py \
  -k 'not webkit' --junitxml=workspace-acceptance.xml
```

The checked-in workflow `.github/workflows/workspace-refactor.yml` runs the 138
non-browser cases separately, and installs Chromium **and WebKit with their system
dependencies** for the full 14-case browser job. This file is a reproducible CI
configuration, not a claim that a GitHub Actions run has already passed.

Browser cases exercise real page scripts and API/MCP calls for independent tabs,
workspace navigation, source-bounded project shell choices, OAuth space changes
clearing the prior profile, profile creation returning to the original pending
consent, no connection creation before final confirmation, and all three VPS
visibility modes. They do not use the user's browser, login cookies or credentials.

## Remaining release blockers

1. **Repository-wide test/fixture port.** Thirty-nine test modules still import
   removed Role/schema/SSH interfaces, including `hub.roles` and
   `agent.ssh.prepare_ssh`. These errors were observed, not skipped or changed to
   pass. Preserve relevant behavioral coverage while replacing obsolete fixtures;
   do not restore runtime compatibility adapters to satisfy old test imports.
   The shared-source coordination contract was ported to `source_key` and retains
   its overlap/sibling/recovery assertions across different workspace IDs.
2. **Windows execution sessions.** The new Session tool explicitly rejects
   unsupported Windows PTY modes; its ConPTY implementation and native Windows
   persistent-shell dialect remain unfinished. Existing native-CLI ConPTY support
   is not proof that the new tool supports Windows.
3. **WebKit and deployment acceptance.** Run the configured browser job with system
   dependencies. Real SSH hosts/keys/ProxyJump, production NFS, live OIDC providers
   and cross-platform service restart/upgrade have not been certified by the
   disposable fixtures.

Do not merge this checkpoint as a release or point it at an existing Hub data
volume based on the focused result. Use a separate empty `HUB_DATA_DIR` for manual
workspace evaluation. Source folders and VPS contents must remain untouched by
any database reset or initialization.
