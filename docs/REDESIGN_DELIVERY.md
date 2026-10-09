# CodePier resource/access/conversation redesign delivery

> Historical redesign report. Its workflow archive preservation and compatibility directions are superseded by [workflow removal](CONVERSATIONS.md#workflow-removal) and the consolidated [account area](RESOURCE_MODEL.md#consent-administration-and-identity). The test results below describe that earlier revision, not the current tree.

Verified on 2026-10-07 in the local checkout `/mnt/vibe-coding-share/develop/codepier`.

- Starting revision: `5410d26ba583900f8dc2361fe06bda26276a43a6`.
- Implementation and test revision: `cfeee6e567da8f4ddd81d853ae17945cd174613b`.
- Local branch: `develop/resources-access-conversations`.
- The subsequent delivery commit changes documentation only. These test counts belong to the implementation revision above.
- No push, merge, CI dispatch, publication, deployment, live database upgrade/reset or real account/host testing was performed. Dependency installation and the official OpenAI reference lookup used network access. Two initial test-launcher invocations were intercepted by the environment's remote-test wrapper and failed before any tests ran; all reported verification below ran locally through the explicit interpreter.

## Delivered behavior

**Resources → Access (Roles / Client Connections) → Conversations** is the main navigation. Existing CLI, workbench, browser/computer, artifact, audit and operation viewers remain available.

Resources have separate Project, MCP Service and VPS configuration. Details collect configuration/availability, role access, owned conversations and permitted existing operation/audit records. Project/MCP and Project/VPS associations grant no authority. VPS metadata read and remote-account execution are separate permissions. SSH requires a configured, validated Project/Agent route plus project execute, explicit VPS execute and the Agent's local opt-in; a project directory is not a remote-account sandbox.

Access groups role editing, client connections and advanced stable Profile/legacy grant settings. Creating a Bearer connection atomically creates/reuses its stable identity and grant. Fixed connections freeze resource/action pairs and MCP tool definitions; current policy/identity/account checks can reduce access but role additions cannot expand the original ceiling. Dynamic future-role consent and external MCP consent are explicit. Existing fixed grants remain unchanged. Administration and resource use have separate authority checks; role labels carry no privilege.

Conversations persist platform/identifier, optional label/actual supplied URL, authenticated provenance, resource/receipt references and activity times. They store no transcript or task/progress state. Calls with supported optional metadata correlate per authenticated user/Space/grant, including concurrent conversations and multiple resources. Missing metadata does not break tool use. The anonymous `openai/session` value never becomes a fabricated ChatGPT URL. See the [official reference](https://developers.openai.com/plugins/reference).

Old `#workflows` bookmarks open the read-only archive; other old resource/access hashes resolve to their consolidated tabs. Historical workflow rows, events and replay receipts remain intact. Legacy reads remain callable; valid workflow creation/progress updates return `WORKFLOW_RETIRED`. Normal tool help advertises conversation associations and omits workflow tracking. The current model-visible catalog contains 14 tools, including `conversations`; original execution receipts, cancellation, live permission rechecks, idempotency and private histories remain independent.

## Verification results

Environment: Python 3.13.3, pytest 9.1.1, pinned development dependencies, Playwright 1.63.0, Chromium 153.0.8010.12.

| Local group | Result | Evidence |
| --- | --- | --- |
| Authorization, schemas, conversations, isolated upgrade and historical archive | 343 passed; 0 failed/errors/skips | `.work/committed-authority.xml` |
| Real local Hub/Agent integration, product/role/MCP browser flows and developer tools | 97 passed; 6 WebKit cases deselected | `.work/committed-integration.xml` |
| Native CLI, computer fixtures, durable operations, artifacts, bridge/protocol and release asset regressions | 170 passed; 0 failed/errors/skips | `.work/committed-preservation.xml` |
| Existing VPS/Profile interactions and nineteen management routes in desktop/mobile light/dark views | 26 passed; 16 WebKit cases deselected | `.work/committed-legacy-browser.xml` |
| Total selected tests | **636 passed, 0 failures/errors/skips; 22 WebKit cases deselected** | Four XML reports above |

JUnit files and fixture screenshots are ignored local evidence, not release artifacts. WebKit was not installed and was not exercised. The full repository test suite was not run.

Additional checks passed:

- `.work/redesign-venv/bin/ruff check hub shared web tests --output-format concise`
- `/usr/bin/python3.13 scripts/build_web_assets.py --check`: no outdated panel assets.
- `git diff --check`
- Python syntax parsing for all 96 Hub/shared source files and Node syntax checks for 11 changed panel JavaScript assets.

The tests exercise actual authorization, including Project A write/execute with Testing VPS execution and Production VPS metadata-only access; denied project/action combinations and MCP tools/accounts; role edits, disabled identities, revoke/expiry, fixed non-expansion and explicit dynamic consent; user/Space/grant isolation; MCP definition republication against fixed consent. They also exercise concurrent and multiple-resource conversation correlation, absent/malformed metadata, valid/invalid explicit URLs, private receipt association and actual Hub close/reopen persistence.

The upgrade fixture reconstructs the pre-redesign schema on disposable data, then opens it through the real migration. It verifies existing resource/account ciphertext, policies, Profiles, grants/tokens, operations, audits, workflow events/replays and key bytes remain unchanged. Old associations gain no access or execution route; association-only queued SSH requests are blocked before delivery. Fake SSH integration retains dispatched-operation recovery and credential redaction.

Browser checks serve actual panel assets from isolated Hub instances (the MCP gateway browser cases use the real ASGI app through a local HTTP adapter). They exercise resource creation, role rules, client/token handling, inline OAuth identity creation after explicit consent, supplied conversation links, archive routing, dirty editors, filters/focus and passive refresh while a VPS control is pressed. Computer/provider/CLI and SSH effects use deterministic fixture executables; external MCP transport uses a mocked fixture backend. No real SSH host, MCP account, model provider or ChatGPT account was used.

## Reproduce locally

These commands are run from the repository root with a local shell. The direct `pytest.main` entry avoids this environment's shell test-launcher interception. Test data uses separate `/tmp` directories so Git ownership checks work without modifying global `safe.directory`.

If the ignored local environment is absent:

```bash
uv venv --python /usr/bin/python3.13 .work/redesign-venv
uv pip install --python .work/redesign-venv/bin/python --require-hashes -r requirements-dev.txt
```

Install Chromium locally through Playwright if it is not already available; no browser account is needed.

### authority

```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q", "tests/test_roles.py", "tests/test_access_profiles.py", "tests/test_resource_access.py", "tests/test_conversations.py", "tests/test_resource_upgrade.py", "tests/test_mcp_gateway.py", "tests/test_iam_integration.py", "tests/test_iam_hardening.py", "tests/test_core_tools.py", "tests/test_core_output_schemas.py", "tests/test_query_entrypoints.py", "tests/test_readonly_recovery.py", "tests/test_agentdock_workflows.py", "tests/test_workspace_status.py", "--disable-warnings", "--tb=short", "--basetemp=/tmp/codepier-committed-authority", "--junitxml=.work/committed-authority.xml", "-o", "cache_dir=.work/pytest-committed-authority"]))'
```

### integration

```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q", "tests/test_product_browser.py", "tests/test_agentdock_integration.py", "tests/test_vps.py", "tests/test_core_integration.py", "tests/test_mcp_gateway_ui.py", "tests/test_roles_ui.py", "tests/test_devtools_flow.py", "-k", "not webkit", "--disable-warnings", "--tb=short", "--basetemp=/tmp/codepier-committed-integration", "--junitxml=.work/committed-integration.xml", "-o", "cache_dir=.work/pytest-committed-integration"]))'
```

### preservation

```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q", "tests/test_native_cli.py", "tests/test_native_cli_integration.py", "tests/test_computer_integration.py", "tests/test_operation_continuation.py", "tests/test_operation_continuation_integration.py", "tests/test_integrations_stack.py", "tests/test_v140_core.py", "tests/test_generated_release_hygiene.py", "tests/test_release_assets.py", "tests/test_bridge.py", "tests/test_coding_integration.py", "tests/test_integration.py", "--disable-warnings", "--tb=short", "--basetemp=/tmp/codepier-committed-preservation", "--junitxml=.work/committed-preservation.xml", "-o", "cache_dir=.work/pytest-committed-preservation"]))'
```

### legacy browser

```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q", "tests/test_vps_ui.py", "tests/test_access_profiles_ui.py", "tests/test_management_ui_unification.py", "-k", "not webkit", "--disable-warnings", "--tb=short", "--basetemp=/tmp/codepier-committed-legacy-browser", "--junitxml=.work/committed-legacy-browser.xml", "-o", "cache_dir=.work/pytest-committed-legacy-browser"]))'
```

## Remaining host acceptance and operating limits

Local simulated `openai/session` metadata proves CodePier's correlation behavior; it does not establish that an actual ChatGPT host sends that metadata, displays the OAuth scopes correctly, refreshes its tool catalog or preserves the same anonymous identifier across a host-specific resume. Actual ChatGPT/other-client acceptance, physical remote-host acceptance, WebKit and deployment remain unverified.

For later testing on an authorized nonproduction installation:

1. Back up the database and matching key material before a real upgrade. Configure a writable/tasks-enabled Project and its Agent; separately select that project as the Testing VPS route. Configure Production VPS metadata independently.
2. In **Access → Roles**, grant the worker Project A read/write/execute, Testing VPS read/execute, and Production VPS read only. Grant MCP tools only for the intended account/binding. Connect the client in **Access → Client Connections** and inspect its actual permissions.
3. Verify Testing SSH succeeds and Production SSH is denied using a test account. Confirm association changes confer no access. Check Agent opt-in/offline feedback and the original operation receipt/cancellation behavior.
4. In an actual ChatGPT session, call permitted tools against two resources and a second concurrent conversation. Verify the owner/grant-scoped records appear. If the host supplies no supported metadata, record that absence and use an explicitly supplied real client identifier; tool use should continue.
5. Supply the actual visible conversation URL separately and verify **Return to original conversation** opens that supplied URL. Do not derive it from the anonymous host identifier or expect the registry to fetch a transcript/final answer.
6. Restart the test Hub, confirm conversation associations and receipt queries persist, and verify a separate grant/user/Space cannot read the same identifiers' private histories. Recheck revoke, expiry and fixed/dynamic role edits with disposable connections.
7. Open old resource/access/workflow bookmarks, read historical records and verify a legacy workflow mutation receives the explicit retirement error without changing stored data.

New fixed connections snapshot resource-use rules; device-read/project-creation delegation requires explicitly consented dynamic role authorization. Resource detail operation/audit views are bounded to 50 records and conversation details to the most recent 100 receipt references; underlying histories are retained. Original conversation links require validated HTTPS syntax and an allowed ChatGPT origin/path for ChatGPT records; validation does not establish ownership or remote reachability.

A real upgrade does not automatically choose routes or grant VPS access. Older Hub binaries must not operate a database containing these new consent snapshots; rollback requires a matching pre-upgrade database/key backup. This delivery provides source and isolated migration evidence, not an in-place downgrade or live upgrade acceptance.

Product setup/compatibility details: [resource and access model](RESOURCE_MODEL.md), [conversation associations](CONVERSATIONS.md), [VPS](VPS.md), [role consent](DYNAMIC_ROLES.md).
