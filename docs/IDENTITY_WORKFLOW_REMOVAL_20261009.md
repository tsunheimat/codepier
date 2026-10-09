# Account navigation and workflow removal

Local delivery for the 2026-10-09 assignment. No push, merge, deployment or live host/credential/database change was performed. No live reset, backup or restore was run. All mutable acceptance data belongs to isolated test fixtures.

## Source and scope

- Rechecked baseline: clean `main`, tracking `origin/main`, at `41ee549537731acc8058b7a4ad3e048835b0b35a`.
- Implementation and tested revision: `f0bb2d930fb60511d0b044d82412a5a1bfbf197d` (`Consolidate account navigation and remove retired workflow archives`).
- Working branch: `feat/identity-workflow-removal`. The subsequent delivery-document commit does not change tested public source; its exact tip and parity receipts are recorded in `.work/identity-removal/final-binding.json`.

`/#identity` is the single account navigation entry. My Account retains Space selection/creation/invitations, login identities, re-authentication links and browser sessions. Instance administrators additionally have Users and SSO tabs at `/#identity/users` and `/#identity/sso`. The old identity-admin bookmark canonicalizes to SSO. Ordinary users do not request admin lists and receive a permission state for direct admin-tab navigation; direct administrative API calls remain denied by the existing server authorization. Space ownership does not grant instance administration. Tab changes also fence stale responses/dialogs and keep list filters separate.

The workflow service, renderer, archive tab/routes, read/write/handoff tools, workspace/query facades, workflow sharing, SSE handling, old guidance URI and bridge recovery hints have been removed. Dashboard inputs reject old workflow selectors and its output contains only bounded authorized operation receipts. Artifact cursor pagination now has an independent shared helper. Stable identities, Profiles, consent ceilings, grants, native CLI, resources, independent execution/cancellation/recovery, idempotency, artifacts and audit remain supported.

Fresh stores create no workflow tables. Existing workflow tables/rows/events/replay receipts remain inert and are neither deleted nor converted to Conversations. Upgrade tests preserve their stored content and schema; there is no supported product read facade. The current conversation association index still stores supplied identifiers, optional labels/original URLs, authenticated provenance, resource/operation references and activity times, without goals, steps, progress or chat transcripts.

Current product guides and shipped review guidance describe the removed capability. The earlier redesign delivery report is explicitly marked historical and superseded for workflow compatibility.

## Verification

| Check | Actual result at the tested revision |
| --- | --- |
| Complete regression, shard 0 | 921 passed |
| Complete regression, shard 1 | 768 passed |
| Complete regression, shard 2 | 847 passed |
| Complete regression, shard 3 | 894 passed |
| Combined collection/execution | **3,430 passed; 0 failed, skipped or missing**, 214 modules / 222 jobs |
| Browser-marked cases | 805, included in the total above |
| Coverage and source/archive aggregation | Passed; all four shards verified; no source drift |
| Prettier / browser builds / generated assets | Passed |
| Ruff / mypy / whitespace checks | Passed; mypy retains the repository's three-file incremental scope |
| Full-source and panel ZIP checks | Passed; panel updater compatibility checked for 1.13.0 and current |
| Python / isolated MCP SDK / npm advisory audits | 74 / 29 Python distributions and the npm dependency tree checked; no reported vulnerabilities |
| Supplemental artifact catalog check | Five records across three pages; six invalid cursors rejected; cross-grant cursor filtering and restart persistence passed |
| Additional Conversations capture | Passed on the same unmodified archive, with simulated host metadata, two projects and two original operation references |

The full-suite changed-Python-statement report is 18/29 (62.07%), with no unmeasured files. Nine uncovered lines belong to the extracted cursor helper, whose valid/invalid paging behavior was then checked by `check_artifact_pagination.py` against the same committed archive. The report also lists the manual `browser_check.py` adapter asset-loading line and the MCP guidance-resource line; the latter's current/removed URI behavior is asserted by the real HTTP integration suite despite that measurement gap. The supplemental check is separate evidence, not added to the 3,430 test count or retroactively included in the suite's coverage percentage. Python coverage does not measure the JavaScript UI.

The complete suite runs from a verified public archive extracted outside the checkout. Every shard records full collection, per-case execution phases, coverage and identical before/after source fingerprints. The four-shard merge verifies exactly-once coverage; a separate release-acceptance check binds them to the committed public archive. An initial start using temporary directories on the shared mount was interrupted before verification; it is excluded from accepted results. Completed runs use local `/tmp` storage so Git fixtures have correct file ownership.

The build gates include Prettier, MCP/core browser builds, integration asset generation, panel hash regeneration/checking, Ruff, mypy, diff whitespace checks, public release/source-link checks and both full-source and panel-update ZIP validation. The accepted source archive has 622 public files and 44 panel entry assets. The full-source ZIP SHA-256 is `16934c880c440bea2141be38ecf7ce2fe2cd7c13ad0ec5e5460ac6b8387d95b9`; the panel ZIP SHA-256 is `39688c60eb3c359fc33eb36de34579bf31586f16cd7cd6a9fcf607ed21816889`.

## Browser evidence and boundaries

`tests/test_identity_navigation.py` exercises real Hub authorization through a loopback HTTP/ASGI adapter, with a signed fixture IdP. It covers administrator and ordinary-user views in Chromium and WebKit at 1440×1000 and 390×844, direct route/API denials, same-page canonical bookmarks, stale discovery responses after tab changes, user/provider forms, discovery, group mapping, reconciliation, complete identity linking and browser-session revocation. Re-authentication's browser link/return target is asserted; signed login/re-authentication semantics are covered by the OIDC backend suites. This adapter leaves SSE inert; existing real Hub/Agent browser suites cover streams and invalidation.

Screenshots are in `.work/identity-removal/evidence/`, with hashes in `screenshots.json`. Representative files:

- `alice-account-chromium-1440.png` and `alice-account-webkit-390.png`: ordinary account view.
- `alice-admin-denied-webkit-390.png`: direct administrative navigation denied.
- `owner-account-chromium-1440.png`, `owner-users-webkit-390.png` and `owner-sso-webkit-390.png`: consolidated administration.
- `owner-discovery-chromium-1440.png`, `owner-group-mapping-webkit-390.png` and `alice-linked-chromium-1440.png`: account/SSO actions.
- `conversations-index-chromium-1440.png`, `conversations-index-chromium-390.png` and `conversations-detail-chromium-390.png`: settled association index/details, supplied original URL and existing operation references.

The local host is Ubuntu 25.04, Python 3.13.3, Node 24.18.0 and Playwright 1.63.0. WebKit uses the existing isolated browser/library setup; `PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1` bypasses only its package-inventory preflight. Both real browser engines execute, with no assertions skipped. Native Windows/macOS runs, hosted CI, a real Authentik/other IdP, a real ChatGPT/client session, live SSH/MCP services, paid model calls and deployment acceptance were not performed. The supplied earlier fresh-storage reset is not acceptance of this revision.

## Reproduction and receipts

All commands use explicit local interpreters and `REMOTE_TEST_BYPASS=1`. Browser runs set `PLAYWRIGHT_BROWSERS_PATH=/tmp/codepier-ci-webkit-libs-blwgql6f/browsers`; compatibility checks use `.work/ci-compat-venv/bin/python` through `MCP_COMPAT_PYTHON`.

The checked source is `/tmp/codepier-identity-public-f0bb2d9`. For each index 0–3, the runner used:

```sh
.venv/bin/python scripts/check_full_regression.py \
  --output /tmp/codepier-identity-regression-f0bb2d9/shard-INDEX \
  --workers 2 --timeout 1200 --shard-count 4 --shard-index INDEX --coverage
```

Accepted receipts are retained under `.work/identity-removal/`:

- `verified/complete.json`: complete, exactly-once four-shard result.
- `verified/archive-verification.json`: tested commit and public archive binding.
- `verified/shard-{0,1,2,3}/`: source fingerprints, collection/plan, JUnit reports, per-job receipts and coverage; no fixture databases or private temporary trees were copied.
- `verified/coverage.json`, `verified/coverage.xml`, `verified/.coverage` and `verified/diff-coverage.json`.
- `public-check.json`, `panel-check.json`, `source-full.zip`, `source-panel.zip` and `archive-proof.json`.
- `format.log`, `build.log`, `integration-assets.json`, `environment.json` and the three `*-audit.json` reports.
- `artifact-pagination.json` / `check_artifact_pagination.py` and `conversations-capture.json` / `capture_conversations.py`: supplementary checks/captures against the verified archive.
- `evidence/screenshots.json`: hashes for 42 selected screenshots from the test/capture runs.
- `final-binding.json`: final documentation tip, clean branch and identical public/tested-source proof.

Aggregation uses `scripts/merge_regression.py --coverage`, followed by `scripts/release_acceptance.py verify --commit f0bb2d930fb60511d0b044d82412a5a1bfbf197d` against all four retained shard summaries. Changed-line coverage uses `scripts/diff_coverage.py --base 41ee549537731acc8058b7a4ad3e048835b0b35a`.

No known functional blocker remains within the requested local scope. The coverage and external-service/platform limits above remain explicit; local acceptance does not certify a deployment.

## Changed files

`A` = added, `M` = modified, `D` = removed. This list includes the delivery document added after implementation verification.

```text
M	CHANGELOG.md
M	README.md
M	agent/context.py
M	docs/ACCESS_PROFILES.md
M	docs/ARCHITECTURE.md
M	docs/CHATGPT.md
M	docs/CONVERSATIONS.md
M	docs/CORE_TOOLS.md
M	docs/DYNAMIC_ROLES.md
M	docs/LONG_OPERATIONS.md
M	docs/MULTIUSER_OIDC.md
M	docs/MULTIUSER_OIDC_STATUS.md
M	docs/REDESIGN_DELIVERY.md
M	docs/RESOURCE_MODEL.md
M	hub/access_profiles.py
M	hub/artifacts.py
M	hub/core_tools.py
M	hub/iam.py
M	hub/iam_api.py
M	hub/iam_schema.py
M	hub/integrations.py
M	hub/mcp.py
M	hub/runtime.py
M	hub/store.py
M	hub/tool_router.py
D	hub/workflows.py
M	hub/workspace_status.py
M	scripts/browser_check.py
M	scripts/mcp_stdio_bridge.py
M	shared/contracts.py
M	shared/core_contracts.py
M	shared/core_output_schemas.py
A	shared/cursors.py
M	shared/integration_contracts.py
M	shared/query_contracts.py
M	skills/review-and-verify/SKILL.md
M	tests/historical_workflows.py
A	tests/operation_fixture.py
M	tests/test_agentdock_bridge.py
M	tests/test_agentdock_integration.py
D	tests/test_agentdock_workflows.py
M	tests/test_audit_bridge_deploy.py
M	tests/test_conversations.py
M	tests/test_core_integration.py
M	tests/test_core_tools.py
M	tests/test_devtools_assets.py
M	tests/test_iam_hardening.py
M	tests/test_iam_ui.py
A	tests/test_identity_navigation.py
M	tests/test_integration.py
M	tests/test_integrations_stack.py
M	tests/test_management_ui_unification.py
M	tests/test_mcp_workspace_retirement.py
M	tests/test_product_browser.py
M	tests/test_readonly_recovery.py
M	tests/test_resource_upgrade.py
M	tests/test_roles.py
M	tests/test_ui_editorial.py
M	tests/test_ui_redesign.py
M	tests/test_ui_unification.py
M	tests/test_v140_core.py
A	tests/test_workflow_removal.py
M	tests/test_workspace_status.py
M	web/app.js
M	web/assets-manifest.json
M	web/identity.js
M	web/index.html
M	web/integrations.js
M	web/product.js
M	web/styles.css
M	web/ui.js
D	web/workflows.js
A	docs/IDENTITY_WORKFLOW_REMOVAL_20261009.md
```
