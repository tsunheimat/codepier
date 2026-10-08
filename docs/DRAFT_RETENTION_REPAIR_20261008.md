# Project-tool draft retention repair, 2026-10-08

Baseline: `e1f06c10239c14c8c75cd7d2c5a3dd24fe328139`.
Implementation revision: `aa053c0b45e0020ba001bc7da908f98a471dc253`.
Final documentation commit contains this report only; the final source binding
checks that its selected public files and regression snapshot match the tested
implementation.

## Complete observed CI disposition

[Regression run 37754845533](https://github.com/tsunheimat/codepier/actions/runs/37754845533)
finished with ten successful jobs and two failed jobs. All eight shards
completed. The only original failures were these two nodes in
[macOS shard 3, job 113236751810](https://github.com/tsunheimat/codepier/actions/runs/37754845533/job/113236751810):

```text
tests/test_navigation_refinement.py::test_resources_devices_project_tools_artifacts_and_access_members[1440-1000]
tests/test_navigation_refinement.py::test_resources_devices_project_tools_artifacts_and_access_members[390-844]
```

Both returned an empty command textarea instead of
`printf retained-navigation-draft` after leaving and returning to Development
Tools. The unchanged 5000 ms assertion failed; the module had two failures and
12 passes. The final collector evidence and independently read GitHub job/log
results agree. Ubuntu aggregation passed all 3409 tests at the baseline.

macOS aggregation job `113241371045` received all four shard reports and
correctly rejected the failed shard with “A shard is failed, skipped,
incomplete or stale.” It is an aggregation consequence, not another test
failure. No regression/aggregation classification or gate changed.

The baseline's image run 37754845560, real Authentik and native Windows checks
passed. These are baseline observations, not acceptance of the repaired
revision. No candidate has been deployed by this task.

## Cause and deterministic evidence

Classification: **product focus/render race**. `changeTab` rerendered the entire
Development Tools page even when the user selected its current tab. That render
awaits real project/device inventory while the current command form stays
editable. When it completes, the old form is replaced and the callback focuses
the tab. It can finish between focusing a textarea and inserting text. The
text then never reaches the command input listener; there is no saved draft to
restore. Increasing the assertion timeout cannot recover that lost input.

The baseline navigation module passed locally (14/14), as its Ubuntu sibling
did. A controlled real-browser reproduction then held delivery of one actual
authorized `/api/projects` response and released it at textarea focus. It used
the real Hub/Agent and actual responses, not canned permission or resource
data. Against the original product code, it reproduced the same empty-text
assertion and recorded:

```json
{"events":[{"event":"focus","connected":true}],"connected":false,"active":"i-tab-validation","value":"","drafts":{"scoped validation form":{}}}
```

The original local diagnostic used real scoped IDs/paths; the draft key is
shortened here for readability. There was no command input event. After the
repair, all four Chromium/WebKit desktop/mobile cases record focus followed by
an input event containing the command, with the original form still connected
and the text restored after navigation. This explains the product mechanism
without claiming the native macOS log recorded focus events: that log contains
the empty value only. Native macOS confirmation still requires fresh CI.

Some initial diagnostic gate setups were interrupted when they held later
navigation reads as well as the intended refresh. They are not counted as
verification. The completed reproduction and final regression release the
one-shot gate before normal navigation; they contain no blind retry.

## Change and retained behavior

Selecting the already-active Development Tools tab now leaves the form
mounted. The explicit refresh control still refreshes; switching tabs,
projects and workspaces keeps its existing behavior. No draft store, backend
authority, receipt, idempotency or permission rule changes.

The new four-case browser regression keeps the visible text assertion,
checks the form remains connected, checks no redundant render or inventory
request starts, and verifies the same draft after leaving and returning. It
never re-enters the original text after navigation. The two original cases
retain their assertion and additionally check that the worktree draft stays
absent in the original directory and another project, then returns in the
exact original worktree.

The complete flow modules preserve explicit refresh, project/workspace
mapping, original operation recovery after a lost response, pending-operation
and fixed-key semantics, logout/access revocation and private histories. The
accepted Resources/Access/Conversations, Personal/Legacy retirement,
MCP client/core 2.2.0, MCP save/inventory readiness and short-desktop spacing
changes remain intact. There is no workflow progress or transcript store.

## Verification commands and environment

Final local results at the implementation revision:

| Check | Result |
| --- | --- |
| Exhaustive shard 0 | 935 passed; zero failed/skipped/missing; 405.955 seconds |
| Exhaustive shard 1 | 746 passed; zero failed/skipped/missing; 396.770 seconds |
| Exhaustive shard 2 | 837 passed; zero failed/skipped/missing; 544.753 seconds |
| Exhaustive shard 3 | 895 passed; zero failed/skipped/missing; 339.841 seconds |
| Complete aggregation | **3413 passed, verified=true, scope=complete**, 213 unique modules, 221 jobs; 801 browser-marked cases (markers overlap, not additional totals). |
| Source proof | All four post-run archive checks and final commit/archive verification passed. Source snapshots unchanged; no duplicate phases, missing/unexpected cases, retries, forced terminations or failed modules in this cohort. |
| Affected complete flows | Navigation refinement 18 passed, including the committed isolation assertions; development-tool/integration-browser/continuous-access flows passed in the full cohort as well as the 57-case focused run. |
| Real isolated Authentik | Provider 2026.8.3, all seven checks passed, production_changed=false. |
| Coverage | Parent/subprocess Python coverage captured and merged. Changed-production-Python observation: zero statements, percent=null. The frontend repair is exercised by real browser assertions. |
| Static/build | Prettier, core/MCP Apps build, generated panel assets, Ruff, mypy and diff checks passed. Committed generated assets remain coherent. |
| Dependencies | npm audit zero vulnerabilities; main and official-MCP compatibility Python audits found no known vulnerabilities. Locks/manifests unchanged, client/core 2.2.0 retained. |
| Release bundles | Full public and panel-update checks passed, 621 selected source files, 44 browser assets, zero credential-pattern findings; old 1.13 and current panel updater validation passed. |

Source identities:

```text
tested implementation: aa053c0b45e0020ba001bc7da908f98a471dc253
collection SHA256: 45e9c70eb823f9a34c566c670149c50f90434b8730d7140801bebcfdf8be9254
regression snapshot SHA256: d62de7893927887b5aabba3dcab26e19b8fdf75d4474a915babb6b29438ac90e
public file inventory SHA256: ade2a3ce296d0f1f0a37ccb6fd04b28e512dd9690e0609d7f0888a66481aff3b
full public ZIP SHA256: 46e160c85b6ee1fb533ca0674f4c91b8688b00e217edf84a5aefd597f2bb929a
panel-update ZIP SHA256: a689e7fcdcaa458197db5358330475f9bb269dbd4bc0e2c2d2f8ce261c13af59
```

The regression snapshot fingerprints file contents; the public archive
inventory also verifies sizes and modes, so these inventory hashes use
different formats. Private local receipts are retained at
`.work/drafts-ci-aa053c0/`: all shard summaries, phase/JUnit/collection/module
logs, full coverage and aggregation/source-proof results, and Authentik JSON.
Original execution files remain under `/tmp/codepier-drafts-aa053c0`. Other
receipts are `.work/drafts-{public,panel}-check.json`,
`.work/drafts-public-proof.json`, `.work/drafts-diff-coverage.json` and the
focused XML files listed below. Final documentation-only binding and parity
are recorded in `.work/drafts-final-source-proof.json` and
`.work/drafts-final-parity.json`; these do not substitute for fresh native CI.
No test database, synthetic fixture secret, environment or raw deployment
evidence is committed or copied into the public source archive.

All calls use explicit local interpreters with `login:false` and
`REMOTE_TEST_BYPASS=1`; no remote-command test wrapper is used. Local host:
Ubuntu 25.04, Python 3.13.3, Node 24.18.0, Playwright 1.63.0. The real Chromium
and WebKit engines run, with the local WebKit portability setup from the
previous repair:

```bash
export REMOTE_TEST_BYPASS=1
export PLAYWRIGHT_BROWSERS_PATH=/tmp/codepier-ci-webkit-libs-blwgql6f/browsers
export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
export MCP_COMPAT_PYTHON=/mnt/vibe-coding-share/develop/codepier/.work/ci-compat-venv/bin/python
```

This Ubuntu release is not supported by Playwright. WebKit uses a disposable
copied browser plus extracted Ubuntu 24.04 libraries; the second Playwright
setting skips only the host package-inventory preflight that cannot see those
libraries. No browser, authorization, draft or regression assertion is skipped.
Native supported-host CI still uses normal `install --with-deps` and must pass.

Completed focused commands (local setup and isolated basetemp):

```bash
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","tests/test_navigation_refinement.py","--tb=short","--basetemp=/tmp/codepier-drafts-baseline","--junitxml=.work/drafts-baseline.xml","-o","cache_dir=.work/pytest-drafts"]))'
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","-s","tests/test_navigation_refinement.py::test_selected_tools_tab_does_not_replace_form_during_typing[1440-1000-chromium]","--tb=short","--basetemp=/tmp/codepier-drafts-typing-before2","--junitxml=.work/drafts-typing-before2.xml","-o","cache_dir=.work/pytest-drafts"]))'
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","-s","tests/test_navigation_refinement.py","--tb=short","--basetemp=/tmp/codepier-drafts-navigation-fixed","--junitxml=.work/drafts-navigation-fixed.xml","-o","cache_dir=.work/pytest-drafts"]))'
.work/redesign-venv/bin/python -c 'import pytest; raise SystemExit(pytest.main(["-q","tests/test_devtools_flow.py","tests/test_integrations_browser.py","tests/test_continuous_access_ui.py","--tb=short","--basetemp=/tmp/codepier-drafts-flows","--junitxml=.work/drafts-flows.xml","-o","cache_dir=.work/pytest-drafts"]))'
```

The baseline command passed 14. The completed before-fix reproduction failed
one with the recorded focus trace. The initial fixed complete navigation module
passed 18, and the three additional complete modules passed 57. The later
worktree/original-directory/other-project assertions are verified by the
committed exhaustive cohort, not assigned retrospectively to the focused run.

Build, static and dependency commands:

```bash
cd web/mcp-apps
/home/matthew/.nvm/versions/node/v24.18.0/bin/node node_modules/prettier/bin/prettier.cjs --check ../*.js ../*.css ../core/*.mjs *.mjs
/home/matthew/.nvm/versions/node/v24.18.0/bin/node build-core.mjs
/home/matthew/.nvm/versions/node/v24.18.0/bin/node build.mjs
/home/matthew/.nvm/versions/node/v24.18.0/bin/node /home/matthew/.nvm/versions/node/v24.18.0/lib/node_modules/npm/bin/npm-cli.js audit --registry=https://registry.npmjs.org --json
cd ../..
/usr/bin/python3.13 scripts/build_web_assets.py
/usr/bin/python3.13 scripts/build_web_assets.py --check
.work/redesign-venv/bin/ruff check agent hub shared scripts tests --output-format concise
.work/redesign-venv/bin/mypy
git diff --check
.work/redesign-venv/bin/python -c 'from pip_audit._cli import audit; import sys; sys.argv=["pip-audit","--local"]; audit()'
.work/redesign-venv/bin/python -c 'from pip_audit._cli import audit; import sys; sys.argv=["pip-audit","--path",".work/ci-compat-venv/lib/python3.13/site-packages"]; audit()'
```

Full public archive and panel-update checks:

```bash
/usr/bin/python3.13 scripts/build_source_bundle.py --public --output .work/drafts-public.zip
/usr/bin/python3.13 scripts/check_release.py --bundle .work/drafts-public.zip --output .work/drafts-public-check.json
/usr/bin/python3.13 scripts/build_source_bundle.py --public --panel-update --output .work/drafts-panel.zip
/usr/bin/python3.13 scripts/check_release.py --panel-update --bundle .work/drafts-panel.zip --output .work/drafts-panel-check.json
/usr/bin/python3.13 scripts/release_acceptance.py prepare --root /mnt/vibe-coding-share/develop/codepier --archive /mnt/vibe-coding-share/develop/codepier/.work/drafts-public.zip --destination /tmp/codepier-drafts-public-aa053c0 --proof /mnt/vibe-coding-share/develop/codepier/.work/drafts-public-proof.json --commit aa053c0b45e0020ba001bc7da908f98a471dc253
```

The extracted source links the two local test environments and locked
node_modules. Source module origins are asserted inside the extracted tree.
The local engineering wrapper `.work/run_drafts_full.py` runs these unchanged
product verification scripts from that tree, sequentially for all four indexes:

```bash
.work/redesign-venv/bin/python .work/run_drafts_full.py
# Each underlying shard invocation, with index/directory changed for 1,2,3:
cd /tmp/codepier-drafts-public-aa053c0
/mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python scripts/check_full_regression.py --output /tmp/codepier-drafts-aa053c0/shard-0 --workers 2 --timeout 1200 --shard-count 4 --shard-index 0 --coverage
# Initial archive proof is copied into each output directory before the shard.
/mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python scripts/release_acceptance.py finish --root /tmp/codepier-drafts-public-aa053c0 --archive /mnt/vibe-coding-share/develop/codepier/.work/drafts-public.zip --proof /tmp/codepier-drafts-aa053c0/shard-0/archive-proof.json
/mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python scripts/merge_regression.py /tmp/codepier-drafts-aa053c0/shard-{0,1,2,3}/summary.json --coverage --output /tmp/codepier-drafts-aa053c0/complete/summary.json
/mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python scripts/release_acceptance.py verify --commit aa053c0b45e0020ba001bc7da908f98a471dc253 --output /tmp/codepier-drafts-aa053c0/complete/archive-proof.json /tmp/codepier-drafts-aa053c0/shard-{0,1,2,3}/summary.json
```

The same extracted source runs real isolated Authentik first, using its own
temporary Compose project, loopback ports, synthetic users and credentials:

```bash
PATH=/tmp/codepier-ci-docker-bin-s4bexmmr:/home/matthew/.nvm/versions/node/v24.18.0/bin:/usr/local/bin:/usr/bin:/bin \
  /mnt/vibe-coding-share/develop/codepier/.work/redesign-venv/bin/python scripts/check_oidc_authentik.py --output /tmp/codepier-drafts-aa053c0/authentik.json
```

The local Docker wrapper uses `sudo -n` for this disposable fixture and
preserves only its generated test variables. The harness cleans up its own
containers/volumes. It does not use the live provider or database.

## Remaining platform boundary

This task's results are local Linux evidence. A controlled Linux scheduling
reproduction and WebKit run are not native macOS acceptance. Native Windows
PowerShell/scheduled-task acceptance is also not inferred from portable tests.
No live workload, database, provider/account/credential, paid model or real SSH
server was operated. Main/publication, remote CI and rollout are handled
separately. The source-only repair does not deploy or migrate the old e829461
testing installation.

Rollout still requires fresh final-revision Ubuntu/macOS full shards and both
aggregations, native Windows/core/PowerShell/scheduled-task checks, real isolated
Authentik, both OS dependency gates and source-bound image/build/boot checks.
There is no unresolved product decision.
