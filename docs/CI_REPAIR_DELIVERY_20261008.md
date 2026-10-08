# Navigation and Personal Space CI repair

Local source verification on 2026-10-08, starting from clean `main` at
`a087cbc6077eb30c1606a7d547e49453c4c6a892`. Origin remains
`https://github.com/tsunheimat/codepier.git`.

The task branch is `fix/personal-space-ci-blockers`. These local implementation
commits are unpublished:

- `efe7de0c9cd56b1d46f19dd45dfb0a7ce0cb83af`: patched MCP SDK pins, generated card/notices/manifest, issuer regression test.
- `41923cb7d3432d4a8d18ffc630d0c5e02eb3892b`: explicit fixture ownership/Spaces, Personal-aware real OIDC acceptance, current navigation tests, desktop Resources density.
- `61373b0a651ae3e845d82273643b829bce115711`: scoped durability-failure fixture.
- `ac600e0f9a8bf95160a543725167ef02f94b08b0`: scoped execution-policy principals and unscoped anonymous-audit expectations.

**Implementation tested:** `ac600e0f9a8bf95160a543725167ef02f94b08b0`.
The following delivery commit changes this report only; its revision is reported
in the delivery response. This engineering report follows the repository-only
delivery-report convention. The existing public user guides remain selected in
both release bundles.

## Decisive original CI failures

The [regression run](https://github.com/tsunheimat/codepier/actions/runs/37727661857)
was inspected by completed jobs/steps, including the additional Ubuntu and macOS
jobs. All original jobs have now completed. The separate successful
[image run](https://github.com/tsunheimat/codepier/actions/runs/37727661865) applies
to `a087cbc`, not to these repairs or a deployed installation.

| Jobs | Failure and classification | Repair |
| --- | --- | --- |
| Authentik `113149405877` | Stale harness: selects nonexistent active Legacy after fresh admin bootstrap; Space creation returns 404. | Read authenticated `/api/iam/me`, require a normal default Personal owner and separate instance-admin flag, select that ID, then create the Team. Real provider coverage remains. |
| Windows `113149406098` | Six stale protocol fixtures omit mandatory `devices.space_id`. | Create the real Personal owner and scope device, project and caller; retain all wire combinations, one-write, recovery and original-payload assertions. Also assert operation provenance and absence of fresh Legacy. |
| Ubuntu/macOS shard 0 `113149406201`, `113149406285` | Dependency: locked client 2.0.0 falls in the advisory range. | Pin client/core 2.2.0; regenerate the shipped card, manifest and notices. |
| Ubuntu/macOS shard 1 `113149406232`, `113149406371` | Stale current-schema inserts, retained-panel identity fixtures, navigation expectations, asset expectation for retired workflow UI and removed handoff tab. | Explicit ownership/Spaces and current UI bootstrap/default; test Resources/Access routes and Conversations association in Development Tools. |
| Ubuntu/macOS shard 2 `113149406224`, `113149406230` | Stale runtime/review fixtures, alias-race insert, login-copy location, SSH fixture without explicit VPS policy, routine grant controls and old UI titles. | Scope fixtures and alias insert; test the actual login renderer; use explicit VPS role/route fixture and advanced fixed-grant controls. |
| Ubuntu/macOS shard 3 `113149406206`, `113149406055` | Stale install/audit/policy fixtures, Legacy-only native SSE expectation, assumed new-user Legacy membership, old navigation/hash assertions. Four 1280×720 layout cases are a product regression. | Team sharing uses explicit membership; SSE uses the authenticated Space; compatibility hashes reach canonical views. Compact desktop Resources tabs/project cover spacing so all project actions fit without hiding controls or changing mobile touch targets. |
| Evidence aggregation `113160448198`, `113160448209` | Consequence: “Missing or duplicate shard reports”; shard 0 stopped before regression. | Require a complete fresh matrix and both aggregation jobs downstream; no check suppression. |

Expanded local checks also exposed the unexecuted shard-0 durability fixture's
missing operation Space, two MCP execution-policy principals missing their
historical fixture Space, and three anonymous-login audit assertions that still
expected a manufactured Legacy default. They now express the accepted contracts.
The audit assertions require `space_id is None` and no authenticated owner, while
retaining the denial of visibility in Team history. No product authorization or
database constraint was relaxed.

## Dependency boundary and compatibility

Dependency outcome: **fixed**. The
[official advisory GHSA-6qxp-vccf-f47h / CVE-2026-104850](https://github.com/advisories/GHSA-6qxp-vccf-f47h)
lists client `>=2.0.0,<2.2.0` as affected and 2.2.0 as the first patched release.
Client 2.2.0 requires core 2.2.0; ext-apps 2.0.3 accepts these through its existing
`^2.0.0` peer ranges. Only those two locked package versions changed. No server
SDK is installed in this dependency tree. Other dependencies and advisory
checks remain unchanged.

A no-network probe against a disposable installation of the exact baseline lock
shows one request carrying dummy credentials to a foreign issuer and an issuer
stamp lost by the old persistence schema. The same probe against the repaired
installation rejects before any foreign request and retains the issuer stamp.
Both versions complete the trusted-issuer control with exactly one request.
The committed regression additionally exercises absent-metadata URL fallback
and verifies persistence of the binding. No real credentials/provider were used.

Repository inspection found no production JS SDK OAuth provider/token-exchange
caller: the card uses App/PostMessageTransport. This is a source-backed
reachability inference, not a claim about external MCP hosts. The affected
library was nevertheless present in the shipped build dependency tree. The
independent read-only investigation and candidate review found no concrete
surviving CodePier route or App/AppBridge compatibility regression. Functional
browser tests exercised the actual rebuilt card, authenticated tools, pending
receipt recovery and immutable paginated review.

The managed temporary security probe is under
`/tmp/codex-security-artifacts-9ea5c47d06d30c52d9f84b2c96f699b3dcfc5e4743489f593a2a2e1b9908fb97/artifacts/`;
`sdk-issuer-probe-results.txt` contains only request counts/booleans.

## Final verification

Tests ran from `/tmp/codepier-ci-repair-public-ac600e0`, extracted from the public
archive and bound to the implementation commit. Selected public source bytes
were checked again after all regression activity and were unchanged.

| Verification | Result |
| --- | --- |
| Backend/upgrade/security/preservation selection | 850 passed, 0 failed/errors/skipped; 168 browser cases excluded for the separate browser batch. |
| Whole selected browser modules, Chromium and WebKit | 233 passed, 0 failed/errors/skipped; 74 WebKit cases. |
| Combined selected pytest evidence | 1,083 distinct passing cases, no overlap between the two batches. This is a focused selection, not the exhaustive repository suite. |
| Original Windows-core selection on Linux | All 127 cases passed within the backend batch; all 20 protocol tests passed. This is not native Windows acceptance. |
| Real isolated Authentik 2026.8.3 | All 7 checks passed, including two ordinary users, real code/PKCE/JWKS, explicit dynamic consent, role expansion/refresh and target-user offboarding. |
| Independent official Python MCP SDK 2.2.0 | Both legacy and auto protocol cases passed against the disposable Hub/Agent. |
| Clean locked npm install and audit | Passed; 0 advisories after the patch. Baseline install/audit still reports the original high advisory. |
| Primary and compatibility Python dependency audits | Both passed, no known vulnerabilities. |
| Prettier / Ruff / Mypy / Git whitespace | Passed; Mypy checks the configured 3 source files. |
| Core/App builds, integration assets, panel assets | Reproduced committed output; no `web` diff. |
| Public-source and panel-update release checks | Passed, including current and 1.13 updater selection, document links and generated assets. |
| Archive commit binding and post-regression inventory | Both verified. |

The App card is 633,540 bytes, SHA256
`ad7d4649d8c1ef44a34bb5208ed4655633fdc0dea30021e0adc0ad630df089ed`.
Public input inventory SHA256 is
`f6c27cb1f082a54e7dee5d782c9259adef48a29d0eccc87f6d01c26f9ad9b366`.
The public archive SHA256 is
`9bde592a1c4e13f473fd86bf88e710f524cd3ba79b4491b777f4316d25720c07`;
panel-update archive SHA256 is
`cdb9b306b21180fd37d29533a60483b9932dbe78d12ca032b908dff2cecfc840`.

Evidence in `.work`: `ci-final-backend-ac600e0.xml`,
`ci-final-browser-ac600e0.xml`, `ci-final-authentik-ac600e0.json`,
`ci-repair-public-proof.json`, `ci-repair-public-check.json`,
`ci-repair-panel-check.json` and the selector JSON files. These are ignored local
outputs, not public source or deployment evidence.

### Commands and selector reproduction

The source checks used explicit local executables to avoid this environment's
PATH test interception:

```bash
cd /mnt/vibe-coding-share/develop/codepier/web/mcp-apps
/home/matthew/.nvm/versions/node/v24.18.0/bin/node /home/matthew/.nvm/versions/node/v24.18.0/lib/node_modules/npm/bin/npm-cli.js ci --ignore-scripts --registry=https://registry.npmjs.org
/home/matthew/.nvm/versions/node/v24.18.0/bin/node /home/matthew/.nvm/versions/node/v24.18.0/lib/node_modules/npm/bin/npm-cli.js run format:check
/home/matthew/.nvm/versions/node/v24.18.0/bin/node build-core.mjs
/home/matthew/.nvm/versions/node/v24.18.0/bin/node build.mjs
/home/matthew/.nvm/versions/node/v24.18.0/bin/node /home/matthew/.nvm/versions/node/v24.18.0/lib/node_modules/npm/bin/npm-cli.js audit --registry=https://registry.npmjs.org
cd /mnt/vibe-coding-share/develop/codepier
/usr/bin/python3.13 scripts/build_integration_assets.py
/usr/bin/python3.13 scripts/build_web_assets.py --check
git diff --exit-code -- web
.work/redesign-venv/bin/ruff check agent hub shared scripts tests --output-format concise
.work/redesign-venv/bin/mypy
git diff --check
.work/redesign-venv/bin/python -c 'from pip_audit._cli import audit; import sys; sys.argv=["pip-audit","--local"]; audit()'
.work/redesign-venv/bin/python -c 'from pip_audit._cli import audit; import sys; sys.argv=["pip-audit","--path",".work/ci-compat-venv/lib/python3.13/site-packages"]; audit()'
/usr/bin/python3.13 scripts/build_source_bundle.py --public --output .work/ci-repair-public.zip
/usr/bin/python3.13 scripts/check_release.py --bundle .work/ci-repair-public.zip --output .work/ci-repair-public-check.json
/usr/bin/python3.13 scripts/build_source_bundle.py --public --panel-update --output .work/ci-repair-panel.zip
/usr/bin/python3.13 scripts/check_release.py --panel-update --bundle .work/ci-repair-panel.zip --output .work/ci-repair-panel-check.json
/usr/bin/python3.13 scripts/release_acceptance.py prepare --root /mnt/vibe-coding-share/develop/codepier --archive /mnt/vibe-coding-share/develop/codepier/.work/ci-repair-public.zip --destination /tmp/codepier-ci-repair-public-ac600e0 --proof /mnt/vibe-coding-share/develop/codepier/.work/ci-repair-public-proof.json --commit ac600e0f9a8bf95160a543725167ef02f94b08b0
```

The compatibility environment was created locally with `uv venv --python
/usr/bin/python3.13 .work/ci-compat-venv` and `uv pip install --python
.work/ci-compat-venv/bin/python --require-hashes -r requirements-compat.txt`.
The extracted tree links the local test environments and locked node_modules.
Its source modules resolve inside the extracted tree.

Recreate the backend selector JSON from this list, preserving the whole modules:

```text
tests/test_protocol_negotiation.py tests/test_native_windows.py tests/test_windows_service.py
tests/test_optimization_store.py tests/test_regression_plan.py tests/test_smoke.py
tests/test_agent_install_api.py tests/test_audit_runtime.py tests/test_audit_store.py
tests/test_audit_shared_cli.py tests/test_chat_sse.py tests/test_chat_history_backend.py
tests/test_computer_approval_events.py tests/test_computer_approvals.py tests/test_iam_agent.py
tests/test_key_rotation.py tests/test_mcp_tasks.py tests/test_native_supervisor_regressions.py
tests/test_oidc_acceptance_transport.py tests/test_project_admission_and_authority.py tests/test_ssh.py
tests/test_execution_policy.py tests/test_review_20260930_core.py tests/test_chat_complete_regressions.py
tests/test_installer_commands.py tests/test_tool_receipt_recovery.py tests/test_personal_spaces.py
tests/test_oidc_bootstrap.py tests/test_oidc_integration.py tests/test_iam_integration.py
tests/test_iam_review_regressions.py tests/test_roles.py tests/test_access_profiles.py
tests/test_conversations.py tests/test_resource_access.py tests/test_resource_upgrade.py
tests/test_mcp_tasks_http.py tests/test_mcp_sdk_dependency.py tests/test_generated_release_hygiene.py
tests/test_release_assets.py tests/test_integration_finish.py tests/test_integrations_stack.py
tests/test_operation_continuation.py tests/test_v140_compatibility.py tests/test_public_release.py
tests/test_release_acceptance.py tests/test_chat_fullstack.py tests/test_chat_panel_navigation.py
tests/test_codepier_browser_migration.py tests/test_continuous_access_ui.py tests/test_integrations_browser.py
tests/test_panel_session_and_editor.py tests/test_panel_update_autoreload.py tests/test_ui_comfort.py
tests/test_ui_editorial.py tests/test_ui_redesign.py tests/test_ui_reference.py
```

The browser selector JSON contains:

```text
tests/test_chat_complete_regressions.py tests/test_chat_fullstack.py tests/test_chat_panel_navigation.py
tests/test_claude_browser.py tests/test_continuous_access_ui.py tests/test_integrations_browser.py
tests/test_management_ui_unification.py tests/test_mcp_apps_host.py tests/test_navigation_refinement.py
tests/test_panel_session_and_editor.py tests/test_panel_update_autoreload.py tests/test_product_browser.py
tests/test_tool_receipt_recovery.py tests/test_ui_comfort.py tests/test_ui_editorial.py
tests/test_ui_redesign.py tests/test_ui_reference.py tests/test_ui_unification.py
```

The exact batch invocations were:

```bash
cd /tmp/codepier-ci-repair-public-ac600e0
MCP_COMPAT_PYTHON=/mnt/vibe-coding-share/develop/codepier/.work/ci-compat-venv/bin/python /mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python -c 'import json, pytest; files=json.load(open("/mnt/vibe-coding-share/develop/codepier/.work/ci-final-backend-selectors.json")); raise SystemExit(pytest.main(["-q",*files,"-m","not browser","--disable-warnings","--tb=short","--basetemp=/tmp/codepier-ci-final-backend-ac600e0","--junitxml=/mnt/vibe-coding-share/develop/codepier/.work/ci-final-backend-ac600e0.xml","-o","cache_dir=/mnt/vibe-coding-share/develop/codepier/.work/pytest-ci-final-backend"]))'
PATH=/tmp/codepier-ci-docker-bin-s4bexmmr:$PATH /mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python scripts/check_oidc_authentik.py --output /mnt/vibe-coding-share/develop/codepier/.work/ci-final-authentik-ac600e0.json
PLAYWRIGHT_BROWSERS_PATH=/tmp/codepier-ci-webkit-libs-blwgql6f/browsers PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 MCP_COMPAT_PYTHON=/mnt/vibe-coding-share/develop/codepier/.work/ci-compat-venv/bin/python CODEPIER_UI_SCREENSHOTS=/tmp/codepier-ci-final-screenshots CODEPIER_UI_UNIFICATION_SCREENSHOTS=/tmp/codepier-ci-final-unification /mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python -c 'import json, pytest; files=json.load(open("/mnt/vibe-coding-share/develop/codepier/.work/ci-final-browser-selectors.json")); raise SystemExit(pytest.main(["-q",*files,"--disable-warnings","--tb=short","--basetemp=/tmp/codepier-ci-final-browser-ac600e0","--junitxml=/mnt/vibe-coding-share/develop/codepier/.work/ci-final-browser-ac600e0.xml","-o","cache_dir=/mnt/vibe-coding-share/develop/codepier/.work/pytest-ci-final-browser"]))'
/usr/bin/python3.13 /mnt/vibe-coding-share/develop/codepier/scripts/release_acceptance.py finish --root /tmp/codepier-ci-repair-public-ac600e0 --archive /mnt/vibe-coding-share/develop/codepier/.work/ci-repair-public.zip --proof /mnt/vibe-coding-share/develop/codepier/.work/ci-repair-public-proof.json
```

On this Ubuntu 25.04 host, WebKit 26.6 uses its Ubuntu 24.04 fallback build.
Missing shared libraries were downloaded/extracted into a disposable directory
and supplied to a disposable browser copy. The local-only environment flag
bypasses Playwright's system-package inventory check, which cannot see those
libraries. The real WebKit process and all application assertions ran. No CI,
advisory or application checks were disabled; no system packages were installed.
Downstream CI must use its normal supported-host `install --with-deps` setup.

Docker access for the isolated provider used a temporary CLI wrapper executing
`sudo -n`, preserving only the harness-generated fixture environment variables.
The harness owns its loopback Compose project, disposable Hub store and generated
credentials, and cleans up that project's containers/volumes. No daemon settings,
live provider/accounts, deployed credentials or deployed database were changed.

## Preserved behavior and rollout boundary

No backend migration or authorization code changed in this repair. Resources /
Access / Conversations, Devices under Resources, contextual project tools and
artifacts, Members in Access and compatible hashes remain. Fixed consent
non-expansion, explicit dynamic consent, resource/action pairing, VPS execution
routes, account/tool scope, live revocation/expiry checks and grant-private
histories remain enforced. The tests add no workflow steps/progress or duplicate
chat history to Conversations.

Fresh/local-admin/OIDC bootstrap retains normal Personal ownership and no new
Legacy membership/default. The isolated unused-Legacy upgrade shape retains
identity, session, provider credentials, keys, Personal ownership and original
audit provenance. Populated/unknown-reference Legacy stores retain explicit
compatibility and original operation/artifact/conversation history. Their
business-data transfer/retirement remains the accepted separate operating
boundary; nothing is automatically moved or discarded.

Before rollout, require the full new-revision regression workflow: all four
Ubuntu and macOS shards, both complete-evidence aggregation jobs, real isolated
Authentik acceptance, Windows core plus real PowerShell/bootstrap/scheduled-task
recovery, and all dependency/build/bundle checks. Require the new-revision image
candidate build and empty/legacy/root-owned data boot checks in the separate
publication process. The earlier successful image run cannot establish these
new-revision results. No remote CI was dispatched here.

Native Windows/macOS execution, exhaustive full-suite CI, actual ChatGPT host
acceptance, physical SSH hosts and deployment remain unverified locally. Host
metadata in conversation tests is simulated. No paid provider or external
executor was used. No consequential product decision remains unresolved.

One bare npm build invocation was intercepted by the environment's remote test
runner and completed before it could be stopped. That result is excluded;
explicit local Node builds were rerun and reproduced the committed bytes.
The deployed testing workload was not changed. No push, merge, tag, image
publication, live migration/reset or deployment was performed, and the
operational file-copy checkout was not touched.
