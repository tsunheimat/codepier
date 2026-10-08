# Exhaustive CI repair, 2026-10-08

Baseline: `b4a7e4fa2cd375c60d93d73db9434da36a3e2671`. Implementation tested:
`1fffe55c3c9db5e9cdeb0695af774a0ea5089ae9`. The delivery documentation is
added in a later commit without changing the tested public source inventory.

## Complete original failure disposition

The completed [regression run 37742832209](https://github.com/tsunheimat/codepier/actions/runs/37742832209)
had 12 jobs: five successful, seven failed. Five shards contained 21 failed
executions of 12 distinct test nodes. The other two failures were aggregation
consequences. All eight regression shards had completed; no unfinished shard
was counted as a pass. The operator's final sanitized evidence and independently
read GitHub job logs agree on this failure set.

| Failing tests | Count | Classification and repair |
| --- | ---: | --- |
| `test_execution_policy_edges.py::test_queue_does_not_retry_missing_grammar_forever`; `test_ambiguous_or_accepted_work_is_only_probed[False/True]` | 3 | Stale fixture: an unbound Principal defaulted to Personal while the intentionally historical fixture's Project was in Legacy. Bind the MCP Principal to the fixture owner's authorized Space. Retain zero-attempt grammar failure and probe-only/no-replay assertions. |
| `test_devtools_assets.py::test_helpers_are_loaded_before_the_workspace_and_assets_exist` | 1 | Stale active-asset inventory: workflows.js is retired from normal UI. Require product.js, helper ordering, asset existence/hashes and absence of the workflow script from normal HTML. Preserve syntax verification of historical compatibility source. |
| `test_workspace_privacy.py::test_audit_export_query_is_an_authorized_selector_not_authority` | 1 | Stale Legacy-default assumption. Use the owner's independently stored Personal identity, seed distinguishable Personal/Team/Legacy audit rows, verify selected-Space-only exports, explicit historical access, and rejection of another user's Personal or unauthorized Legacy selection. |
| `test_panel_performance.py::test_first_event_connection_does_not_repeat_boot_reads` | 1 | Stale unit state bypassed canonical navigation: projects is now resources/projects. Use canonical state and wait for the reconnect's observable render. Keep first connection at zero repeated reads, unrelated operations at zero renders and single-flight refresh assertions. |
| `test_v140_browser.py::test_browser_diagnostics_search_symbols_and_download` | 1 | Stale removed top-level Artifacts button. Navigate Resources -> Imago detail -> Artifacts, assert the exact project filter, and retain actual snapshot download bytes/name, code navigation, source-operation trace and recovery checks. |
| `test_ui_standards.py::test_field_semantics_invalid_reset_and_clear_search[chromium/webkit]` | 2 | Stale newGrant assumption: normal setup creates a Client Connection. Exercise the retained fixed-grant form through Access/Advanced and its visible technical-access control. Keep required-field, invalid/reset, hints, focus and accessible checkbox-group assertions. |
| `test_ui_comfort.py::test_desktop_essentials_fit_and_text_is_readable[1180-640-light/dark-chromium]` | 2 | Product layout regression observed on macOS: device action bounds ended at 646.203125px in a 640px viewport. Trim only Resources device fact/action spacing on short desktops. Preserve text sizes, visible controls, contrast and fit assertions. Add Chromium/WebKit light/dark coverage with realistically wrapped macOS/FQDN display metadata over real authorized inventory. Native macOS confirmation remains a downstream gate. |
| `test_mcp_gateway_ui.py::test_gateway_panel_end_to_end[chromium-390]` | 1 | Product race: save closed the dialog before refreshed inventory arrived, so the next account action could consult an empty/stale service list. Keep the busy dialog through refresh, clear credential fields immediately after successful save and recheck session/Space/dialog ownership after refresh. Test controlled delay of real Hub inventory responses, one account write despite duplicate submit, credential clearing, real discovery/publication/role consent and no secret in the page. |

The failed jobs were Ubuntu shards 0/1/2 (`113197309850`, `113197309743`,
`113197309864`) and macOS shards 0/1 (`113197309945`, `113197310023`). Ubuntu
shard 3 and macOS shards 2/3 succeeded. Aggregators `113202222331` and
`113202222390` correctly rejected failed shards with “A shard is failed,
skipped, incomplete or stale.” No gate or classification change was necessary.

Windows `113197309347`, Authentik `113197309615`, both dependency audits and
[image run 37742832196](https://github.com/tsunheimat/codepier/actions/runs/37742832196)
passed at the baseline. Those results are not assigned to this repair revision.

## Preservation

No backend policy, identity, bootstrap, migration, grant ceiling, receipt,
history or workflow-retirement code changed. The full collection exercises the
accepted Resources/Access/Conversations navigation, project contexts and old
hashes; Personal bootstrap/upgrade and populated Legacy compatibility;
resource/action isolation, fixed/dynamic consent and private histories;
protocol, idempotency, cancellation and operation recovery. MCP client/core
remain exactly 2.2.0. New tests delay real HTTP responses or vary display
metadata; they do not replace authority decisions or weaken assertions.

## Final local results

| Check | Result at the tested implementation revision |
| --- | --- |
| Full shard 0 | 935 passed; 0 failed/skipped/missing; 56 jobs; 426.155 s |
| Full shard 1 | 746 passed; 0 failed/skipped/missing; 55 jobs; 393.250 s |
| Full shard 2 | 837 passed; 0 failed/skipped/missing; 55 jobs; 545.723 s |
| Full shard 3 | 891 passed; 0 failed/skipped/missing; 55 jobs; 312.998 s |
| Complete aggregation | **3,409 passed across 213 unique modules, 221 jobs; verified=true, scope=complete**. Includes 797 browser-marked cases and all 129 cases in the eight affected modules. Markers overlap; they are not additional test totals. |
| Source/archive proof | All four finish checks and final verification passed. Identical source inventory before/after; no duplicate phases, unexpected tests, forced termination or failed module. |
| Coverage | All shards captured Python parent/subprocess coverage and merged successfully. Changed-statement observation: zero changed production Python statements, percent=null; frontend edits are verified by the actual browser tests, not a fabricated Python coverage percentage. |
| Real isolated Authentik 2026.8.3 | All seven checks passed: ordinary users/provider, fresh Personal administrator bootstrap without Legacy, discovery/JWKS/callback, real PKCE/group/private profiles, explicit CodePier dynamic OAuth consent, new-project/refresh identity, actual group-removal offboarding. |
| Dependency advisories | Main and official-MCP compatibility Python environments: no known vulnerabilities. npm audit: zero vulnerabilities. Fresh npm ci used the existing lock unchanged. |
| Build/static | Prettier, core/MCP Apps build, integration assets, generated panel-asset check, Ruff, mypy and git diff checks passed. Rebuilds preserve committed browser assets. |
| Public release | 621 selected source files, no credential-pattern findings, 44 browser assets; full ZIP and panel-update ZIP validated. Installed 1.13 and current panel-updater compatibility passed. |

The initial focused reproduction reproduced six original failures (44 passed,
six failed). A subsequent complete affected-module run found a local fixture
adaptation typo in the artifact test (list indexed by string); it was corrected
and the entire affected flow rerun. The final committed exhaustive cohort above
contains all eight affected modules at the same source revision, rather than
combining selected results from changing sources. The delayed-inventory
regression also failed against the original gateway implementation before its
product repair. No original failing assertion was disabled or replaced with a
permissive response.

Source identities:

```text
implementation commit: 1fffe55c3c9db5e9cdeb0695af774a0ea5089ae9
full collection SHA256: a9677a37aa32902311b504086e3b612041c86170ce6212960fa26acd0fc1a77d
regression snapshot SHA256: 7a38a4e46f842c58efd8082327faa2f07db3a30eca34813e9873757b3099af33
public file inventory SHA256: 9243f4bfe5ff7f7e9744932616d8f01878639f688c13a1d1dea0936ce1e32ccd
full public ZIP SHA256: c25de297c9bfe8a41d60a9294b6be533057575807da5958e48454362d55f5628
panel-update ZIP SHA256: 349660ae51a77226f72cd1cfe894f372fb54e0a762de6ed96f9dd4c7f658e121
```

The regression snapshot fingerprints file contents; the public archive
inventory also verifies file sizes/modes. These are intentionally different
fingerprint formats. Local receipts are retained outside public bundles in
`.work/exhaustive-ci-1fffe55/`: four summaries/JUnit/collection/phase reports,
module logs, aggregate summary, merged coverage and commit/archive proof, plus
`authentik.json`. Original execution receipts remain in
`/tmp/codepier-exhaustive-1fffe55`. Release/build receipts are
`.work/exhaustive-{public,panel}-check.json`, `.work/exhaustive-public-proof.json`
and `.work/exhaustive-diff-coverage.json`. No test databases, fixture secrets or
environments are copied into source bundles or committed.

## Verification method and commands

The earlier 850 backend plus 233 browser cases were a selected suite. This run
uses every collected test and the unchanged production CI runner, four shard
indexes, exactly-once event classification, Python subprocess coverage, source
snapshots and both aggregation/source-proof gates. Each module runs in a fresh
pytest process. Shards run sequentially on this host so an exclusive job cannot
overlap another shard. No failed cohort job is retried or excluded.

The public archive is extracted outside the checkout. `release_acceptance.py
prepare` binds every selected file to the implementation commit and archive;
source-module origins are asserted inside the extracted tree. Every shard gets
an identical archive proof and runs `finish` afterward. Installed dependencies
are linked to the extracted tree; test caches, databases and logs are outside
its public inventory. All regression commands use explicit local Python/Node
executables and `REMOTE_TEST_BYPASS=1`, avoiding the workspace's remote-command
wrappers.

Exact paths used here:

```bash
TASK_ROOT=/mnt/vibe-coding-share/develop/codepier
TASK_PY=$TASK_ROOT/.work/redesign-venv/bin/python
TASK_NODE=/home/matthew/.nvm/versions/node/v24.18.0/bin/node
TASK_NPM=/home/matthew/.nvm/versions/node/v24.18.0/lib/node_modules/npm/bin/npm-cli.js
TASK_SOURCE=/tmp/codepier-exhaustive-public-1fffe55
TASK_RESULTS=/tmp/codepier-exhaustive-1fffe55
TASK_COMMIT=1fffe55c3c9db5e9cdeb0695af774a0ea5089ae9
export REMOTE_TEST_BYPASS=1
export PLAYWRIGHT_BROWSERS_PATH=/tmp/codepier-ci-webkit-libs-blwgql6f/browsers
export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
export MCP_COMPAT_PYTHON="$TASK_ROOT/.work/ci-compat-venv/bin/python"
```

Build and static/dependency verification:

```bash
cd "$TASK_ROOT/web/mcp-apps"
"$TASK_NODE" "$TASK_NPM" ci --ignore-scripts --registry=https://registry.npmjs.org
"$TASK_NODE" node_modules/prettier/bin/prettier.cjs --check ../*.js ../*.css ../core/*.mjs *.mjs
"$TASK_NODE" build-core.mjs
"$TASK_NODE" build.mjs
"$TASK_NODE" "$TASK_NPM" audit --registry=https://registry.npmjs.org --json
cd "$TASK_ROOT"
/usr/bin/python3.13 scripts/build_integration_assets.py
/usr/bin/python3.13 scripts/build_web_assets.py --check
.work/redesign-venv/bin/ruff check agent hub shared scripts tests --output-format concise
.work/redesign-venv/bin/mypy
git diff --check
REMOTE_TEST_BYPASS=1 "$TASK_PY" -c 'from pip_audit._cli import audit; import sys; sys.argv=["pip-audit","--local"]; audit()'
REMOTE_TEST_BYPASS=1 "$TASK_PY" -c 'from pip_audit._cli import audit; import sys; sys.argv=["pip-audit","--path",".work/ci-compat-venv/lib/python3.13/site-packages"]; audit()'
```

Archive preparation (fresh destination required):

```bash
/usr/bin/python3.13 scripts/build_source_bundle.py --public --output .work/exhaustive-public.zip
/usr/bin/python3.13 scripts/check_release.py --bundle .work/exhaustive-public.zip --output .work/exhaustive-public-check.json
/usr/bin/python3.13 scripts/build_source_bundle.py --public --panel-update --output .work/exhaustive-panel.zip
/usr/bin/python3.13 scripts/check_release.py --panel-update --bundle .work/exhaustive-panel.zip --output .work/exhaustive-panel-check.json
/usr/bin/python3.13 scripts/release_acceptance.py prepare --root "$TASK_ROOT" --archive "$TASK_ROOT/.work/exhaustive-public.zip" --destination "$TASK_SOURCE" --proof "$TASK_ROOT/.work/exhaustive-public-proof.json" --commit "$TASK_COMMIT"
ln -s "$TASK_ROOT/.work/redesign-venv" "$TASK_SOURCE/.venv"
ln -s "$TASK_ROOT/.work/ci-compat-venv" "$TASK_SOURCE/.venv-compat"
ln -s "$TASK_ROOT/web/mcp-apps/node_modules" "$TASK_SOURCE/web/mcp-apps/node_modules"
```

From the extracted source, run each shard index 0, 1, 2, 3 with a fresh output
directory and copied initial archive proof:

```bash
cd "$TASK_SOURCE"
mkdir -p "$TASK_RESULTS/shard-0"
cp "$TASK_ROOT/.work/exhaustive-public-proof.json" "$TASK_RESULTS/shard-0/archive-proof.json"
REMOTE_TEST_BYPASS=1 MCP_COMPAT_PYTHON="$TASK_ROOT/.work/ci-compat-venv/bin/python" \
  "$TASK_PY" scripts/check_full_regression.py \
  --output "$TASK_RESULTS/shard-0" --workers 2 --timeout 1200 \
  --shard-count 4 --shard-index 0 --coverage
"$TASK_PY" scripts/release_acceptance.py finish --root "$TASK_SOURCE" \
  --archive "$TASK_ROOT/.work/exhaustive-public.zip" \
  --proof "$TASK_RESULTS/shard-0/archive-proof.json"
# Repeat for indexes 1, 2 and 3, changing both directory and index.
"$TASK_PY" scripts/merge_regression.py "$TASK_RESULTS"/shard-{0,1,2,3}/summary.json \
  --coverage --output "$TASK_RESULTS/complete/summary.json"
"$TASK_PY" scripts/release_acceptance.py verify --commit "$TASK_COMMIT" \
  --output "$TASK_RESULTS/complete/archive-proof.json" \
  "$TASK_RESULTS"/shard-{0,1,2,3}/summary.json
```

Real isolated Authentik verification, from the same extracted source:

```bash
REMOTE_TEST_BYPASS=1 PATH=/tmp/codepier-ci-docker-bin-s4bexmmr:/home/matthew/.nvm/versions/node/v24.18.0/bin:/usr/local/bin:/usr/bin:/bin \
  "$TASK_PY" scripts/check_oidc_authentik.py --output "$TASK_RESULTS/authentik.json"
```

This host needs a local `sudo -n docker` wrapper for its disposable Compose
fixture. It preserves only generated fixture environment variables. The
harness uses its own temporary Compose project, loopback ports, Hub, two users
and synthetic credentials, and cleans up its containers/volumes. It does not
use the deployed Authentik or testing installation.

## Platform boundary and rollout gate

Local host: Ubuntu 25.04, Python 3.13.3, Node 24.18.0, Playwright 1.63.0. Real
Chromium and WebKit engines run. Playwright does not support this Ubuntu host;
WebKit uses a disposable copied browser and extracted Ubuntu 24.04 libraries:

```bash
PLAYWRIGHT_BROWSERS_PATH=/tmp/codepier-ci-webkit-libs-blwgql6f/browsers
PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
```

Those environment settings were used for all local browser commands. The
second setting skips the system package-inventory preflight, which cannot see
the extracted libraries. It does not skip any test or weaken product/browser
security assertions. The full regression classification and fail-closed
aggregation are unchanged. Supported Ubuntu/macOS CI must still use the
repository's normal `playwright install --with-deps` setup.

The macOS/FQDN metadata case is local simulated display metadata over a real
authorized Hub response, not native macOS acceptance. Local Windows-core tests
are portable/unit checks, not actual PowerShell or scheduled-task acceptance.
No real ChatGPT host, SSH server, paid model/provider, testing cluster, live
database or live identity-provider operation was exercised.

Before rollout, require a fresh source-bound full CI on the final revision:
all four Ubuntu shards and aggregation; all four macOS shards and aggregation;
native Windows core, real PowerShell/bootstrap and scheduled-task recovery;
real isolated Authentik; both OS dependency audits; and image/release checks,
including candidate empty/legacy/root-owned boots. Existing baseline successes
cannot satisfy this final-revision gate. There is no unresolved product decision.
