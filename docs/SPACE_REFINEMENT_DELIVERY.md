# Navigation and Personal Space refinement delivery

Verified locally on 2026-10-08 in `/mnt/vibe-coding-share/develop/codepier`.

- Accepted starting tip: `e829461403f7cd06fe404b8d83654633a2c11ba1`, retaining the redesign at `cfeee6e567da8f4ddd81d853ae17945cd174613b`.
- Local branch: `develop/resources-space-refinement`.
- Implementation: `10e573d34d1b3abe5be2f9cddb24afe5a2e50ae8`.
- Historical fixture scopes: `a5792b6f09809bb8ce221bea4691b208e048d13c`.
- Final implementation and tested revision: **`a2f3a1521ed41739904c6eb8c326b458ecd70c81`**.
- The following delivery commit changes this report only; the final documentation revision is reported in the delivery response.
- No push, merge, publication, CI dispatch, deployment or deployed-service restart, live database mutation/reset, real account/credential change, real provider or SSH-host testing, or paid-account use was performed. The deployed cluster and operational file-copy checkout were outside this work.

## Delivered behavior

The sidebar keeps **Resources / Access / Conversations** as the primary model.

**Resources → Devices / Agents** uses the original node onboarding, detail, project mappings, passive state refresh and lifecycle controls. Navigation alone issues no start/restart/update command. Old `/#devices` opens `/#resources/devices`; existing permission checks remain.

Project resource details open **Development Tools** with the exact project and optional worktree ID, even when the workbench points to another project. Contextual hashes survive reload and same-page hash changes. An unavailable selected project does not become another project during manual refresh. Existing event-stream entitlement invalidation may still end a session; that safeguard is preserved. Scoped drafts and original operation receipts remain separate from historical workflows. The ordinary tools heading has no WORKFLOW wording.

Project details also open **Artifacts / Downloads** with a project filter. Authorized cross-project overview and `/#artifacts` remain secondary entries. Download URLs carry the authorized Space; original snapshots, expiry and source-operation references remain. Generic snapshot/search submission and paged source-reading helpers removed alongside the old workflow script are restored as ordinary operation functionality. Lost replies retain the same idempotency key; reload recovery queries the original receipt without replaying saved input.

**Access → Space Members** contains the original memberships, invitations and role-use/delegation assignments. `/#members` opens this tab. Nonadministrators have no membership controls and receive actual backend denials. Role policy, human membership, client consent, stable Profile identity and resource authority remain separate. CLI, workbench, audit, account/identity administration and diagnostics remain reachable.

Conversations continue to store identifiers and resource/operation associations. This change adds no workflow progress, transcript or artifact-body store.

## Bootstrap and upgrade behavior

Fresh stores create no Legacy container. Trusted local initialization and explicitly configured first-login OIDC bootstrap create a normal Personal Space with owner membership. Instance-administrator identity remains a separate flag. Further users receive their own Personal Space without implicit Legacy membership.

The stable `iam_users.personal_space_id` prefers that user's existing owned Personal Space. Provisioning/restart is idempotent. A valid saved explicit Space selection stays selected; an unauthorized other user's Personal ID cannot become the selection. Requests without a selector choose an authorized normal Space. An explicit invalid/inactive Space fails rather than redirecting. Resource-write APIs pass the authenticated authorized Space, and fresh resource schemas have no Legacy write default.

The new `personal_space_schema=1` migration runs inside Store's serialized transaction. For an unused old Legacy container alongside an existing valid Personal Space, it retains Personal ownership/ID, external identity, sessions, provider configuration, encryption keys and credentials. It retires Legacy and its memberships as inactive tombstones, unavailable in the selector or restore list. Historical audit rows keep their IDs and original Space provenance; the audit schema permits unscoped instance events and preserves indexes, triggers and the AUTOINCREMENT high-water mark, including an empty audit table with deleted IDs.

Retirement examines every table's Space columns and every foreign key to Spaces, including other column names, plus keyed project-save receipts. Membership controls and audit provenance can retain tombstones. Resource/policy/grant/invitation/group-mapping/operation/artifact/workflow/native-ownership/conversation/replay or unknown references block automatic retirement.

**Populated Legacy boundary:** preserve that container's data, credentials, grants and provenance in an explicit compatibility Space. It is never the implicit default, and new users gain no automatic membership. Historical populated schemas can retain old SQL defaults for compatibility; current write APIs always specify authorized Space. Transferring or retiring business data requires a separately reviewed operation. No automatic transfer, renaming of Legacy to Personal or destructive global migration is included.

The fresh login UI uses configured OIDC bootstrap availability. Supported OIDC-only first login offers SSO initialization and Personal ownership without demanding local `hub init`. Without the explicit supported bootstrap configuration, recovery initialization guidance remains.

See the public [Personal Spaces guide](PERSONAL_SPACES.md) and [Resources model](RESOURCE_MODEL.md).

## Final verification

Environment: Python 3.13.3, pytest 9.1.1, Playwright 1.63.0, Chromium 153.0.8010.12. Every group below ran against the committed final implementation revision above.

| Group | Result | Local evidence |
| --- | --- | --- |
| Bootstrap, OIDC, authorization, consent, isolation, schemas, conversation/history and upgrades | 480 passed | `.work/space-final-authority.xml` |
| Actual served navigation, project tools/downloads, IAM browser flows and local Hub/Agent integrations | 119 passed; 14 WebKit cases deselected | `.work/space-final-integration.xml` |
| Native CLI/computer fixtures, operation recovery/cancellation/idempotency, source/Git/search/artifacts and release regressions | 224 passed | `.work/space-final-preservation.xml` |
| Nineteen management/compatibility routes, Agent installation/lifecycle, VPS/Profile controls in desktop/mobile themes | 42 passed; 16 WebKit cases deselected | `.work/space-final-management.xml` |
| Total | **865 passed; 0 failures/errors/skips; 30 WebKit cases deselected** | Four JUnit reports above |

JUnit files, temporary screenshots and ZIPs are ignored local evidence. WebKit was not installed. The full repository suite was not run.

New tests exercise fresh Store/local CLI/OIDC creation, repeated startup/login, distinct users, existing Personal selection, empty-business-resource upgrades with OIDC/session/provider/audit preservation, unknown foreign-key references, populated legacy credentials and original operation/artifact/conversation provenance. Existing suites exercise fixed-grant nonexpansion, explicit dynamic changes, revoke/expiry and user/Space/grant-private histories.

Browser checks use actual served panel assets. They cover legacy hashes, project/workspace reload, concurrent selected-context changes, draft navigation, project filtering, download bytes and Space-bearing links, member invitations and real nonadmin denials. A lost artifact reply is injected after the real local server accepts it; recovery returns the original snapshot with a fixed request key. Project-loss manual refresh is tested with the event stream simulated as unavailable while all HTTP permission checks remain real. Other tests retain real local event-stream/session behavior. OIDC bootstrap UI and some membership flows use a real ASGI app through a local HTTP adapter; IdP exchange uses deterministic mock transport.

The public packaging regression is repaired by selecting `docs/RESOURCE_MODEL.md`, `docs/CONVERSATIONS.md` and the new `docs/PERSONAL_SPACES.md`. Useful public links remain. Both public source and panel-update ZIP checks pass against current selected source; panel updater compatibility checks cover 1.13.0 and current. No check was suppressed.

| Local bundle | Files including manifest | SHA256 |
| --- | --- | --- |
| `.work/space-final-public.zip` | 621 | `d705082da88a3d79c1a022191b694a464535a48ee16aea099e61f3036f8d0931` |
| `.work/space-final-panel-update.zip` | 613 | `1fa4b5c6aede11efbc23cecb1b31f060af0c43ac4b3975ad418218a6aeee69eb` |

Release checks validate 44 browser assets and report zero credential-pattern findings. Ruff, generated-assets check, Git whitespace check, parsing all 97 Hub/shared Python files, and Node syntax checks for the seven changed/new JavaScript assets pass. The delivery-only report does not alter either public bundle's selected payload.

## Reproduce locally

Run from the repository root. The direct local interpreter avoids this environment's `-m pytest` launcher interception. Disposable Git/Hub/Agent data is under `/tmp`, without global Git safe-directory edits. Dependencies use the existing ignored local development environment.

### Authority
```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","tests/test_personal_spaces.py","tests/test_oidc_bootstrap.py","tests/test_oidc_integration.py","tests/test_oidc_acceptance_transport.py","tests/test_iam_integration.py","tests/test_iam_hardening.py","tests/test_roles.py","tests/test_access_profiles.py","tests/test_resource_access.py","tests/test_conversations.py","tests/test_resource_upgrade.py","tests/test_mcp_gateway.py","tests/test_core_tools.py","tests/test_core_output_schemas.py","tests/test_query_entrypoints.py","tests/test_readonly_recovery.py","tests/test_agentdock_workflows.py","tests/test_workspace_status.py","tests/test_continuous_access.py","tests/test_audit_api.py","--disable-warnings","--tb=short","--basetemp=/tmp/codepier-space-final-authority","--junitxml=.work/space-final-authority.xml","-o","cache_dir=.work/pytest-space-final-authority"]))'
```

### Integration and browser
```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","tests/test_navigation_refinement.py","tests/test_product_browser.py","tests/test_agentdock_integration.py","tests/test_vps.py","tests/test_core_integration.py","tests/test_mcp_gateway_ui.py","tests/test_roles_ui.py","tests/test_devtools_flow.py","tests/test_iam_ui.py","-k","not webkit","--disable-warnings","--tb=short","--basetemp=/tmp/codepier-space-final-integration","--junitxml=.work/space-final-integration.xml","-o","cache_dir=.work/pytest-space-final-integration"]))'
```

### Preservation
```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","tests/test_native_cli.py","tests/test_native_cli_integration.py","tests/test_computer_integration.py","tests/test_operation_continuation.py","tests/test_operation_continuation_integration.py","tests/test_integrations_stack.py","tests/test_v140_core.py","tests/test_generated_release_hygiene.py","tests/test_release_assets.py","tests/test_bridge.py","tests/test_coding_integration.py","tests/test_integration.py","tests/test_reliability.py","--disable-warnings","--tb=short","--basetemp=/tmp/codepier-space-final-preservation","--junitxml=.work/space-final-preservation.xml","-o","cache_dir=.work/pytest-space-final-preservation"]))'
```

### Management browser
```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","tests/test_management_ui_unification.py","tests/test_agent_install_ui.py","tests/test_vps_ui.py","tests/test_access_profiles_ui.py","-k","not webkit","--disable-warnings","--tb=short","--basetemp=/tmp/codepier-space-final-management","--junitxml=.work/space-final-management.xml","-o","cache_dir=.work/pytest-space-final-management"]))'
```

### Source, assets and bundles
```bash
.work/redesign-venv/bin/ruff check hub shared scripts web tests --output-format concise
/usr/bin/python3.13 scripts/build_web_assets.py --check
git diff --check
/usr/bin/python3.13 scripts/build_source_bundle.py --public --output .work/space-final-public.zip
/usr/bin/python3.13 scripts/check_release.py --bundle .work/space-final-public.zip --output .work/space-final-public-check.json
/usr/bin/python3.13 scripts/build_source_bundle.py --public --panel-update --output .work/space-final-panel-update.zip
/usr/bin/python3.13 scripts/check_release.py --bundle .work/space-final-panel-update.zip --panel-update --output .work/space-final-panel-check.json
```

## Limits and decisions

All migrations operate on disposable data. These fixtures establish source behavior for the supplied upgrade shape, not an exhaustive inspection or upgrade of the deployed database. Unknown references conservatively retain populated compatibility.

Live Authentik/SSO onboarding, physical remote hosts, paid providers, actual ChatGPT/other-client host acceptance, deployment and WebKit remain unverified. Existing client-metadata checks use local simulated host data; no transcript or final-answer fetching is claimed.

No consequential product decision remains unresolved within this scope. Populated Legacy retirement is the explicit operating boundary above; it does not block fresh installations or unused-container retirement.
