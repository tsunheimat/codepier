# Workspace validation and merge gates

CodePier 2.0 is a hard cutoff to the workspace model. An old database, Role,
ambient Current Space, former grant API or old Runner protocol is not an input
that a release must migrate or keep working. Rejection tests cover these removed
interfaces; retained features use the actual current schema and authentication.

## What constitutes acceptance

A selected test run, successful image build or green workspace-focused workflow
is not a substitute for the complete CodePier CI workflow. Evaluate the exact PR
head and base through the checked public-source archive, rather than treating
results from an earlier commit as current. The workflow requires:

- Complete Linux and macOS regression shards and their aggregate evidence.
  Every selected test runs once; the evidence verifies identical source
  inventories and collection hashes, no missing cases and no source changes
  during execution. A collection success alone is not an execution result.
- Actual Chromium and WebKit page/form interaction, native Windows core and
  PowerShell/ConPTY acceptance, plus the isolated Windows service-recovery check.
  Portable process fixtures are not evidence of native Windows behavior.
- A real isolated Authentik instance with two users, discovery/JWKS validation,
  browser login, two-stage workspace/Profile consent, separate Connection
  identities, token renewal and upstream offboarding. Mock OIDC tests remain
  useful unit coverage, not a replacement for this gate.
- Locked Python/JavaScript dependencies, frontend formatting and reproducible
  asset builds, static checks, source packaging and credential-pattern scans.
  The existing mypy gate covers its configured files, not the whole application.

Workflow definitions live in `.github/workflows/ci.yml` and
`.github/workflows/workspace-refactor.yml`. The run associated with a particular
commit, and its retained JSON/JUnit/coverage receipts, are the status authority.
This document does not encode a test total or describe an in-progress run as
passed. Historical checkpoint totals in PR comments are not cumulative coverage.

## Reproduce from an isolated checkout

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
python3.13 -m venv .venv-compat
.venv-compat/bin/python -m pip install --require-hashes -r requirements-compat.txt
.venv/bin/python -m playwright install --with-deps chromium webkit
npm --prefix web/mcp-apps ci --ignore-scripts
npm --prefix web/mcp-apps run format:check
npm --prefix web/mcp-apps run build
.venv/bin/python scripts/build_integration_assets.py
.venv/bin/python scripts/build_web_assets.py --check
.venv/bin/python -m ruff check agent hub shared scripts tests
.venv/bin/python -m mypy
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/check_full_regression.py \
  --output /tmp/codepier-workspace-proof-unique --workers 2 --timeout 1200 --coverage
```

Choose a new evidence directory for each run. Do not edit the source during the
final run. The alternate environment tests interoperability with a separately
locked external MCP SDK; it does not restore the removed CodePier application
model or allow old credentials to select a workspace.

A dedicated remote test host can provide an isolated browser dependency sysroot
rather than installing libraries on a live application host. Record that host
adaptation separately: it must not alter application behavior, test selection,
assertions, outcome reporting or credentials. Tests still use the actual browser
engine. CI installs its matching browser system dependencies directly.

## Regression boundaries

The retained tests exercise concrete authority and effects, not only response
shapes. Coverage includes direct Member/Profile intersections, immutable
Connection identities, profile creation returning to pending OAuth consent,
cross-workspace denial, private artifact/operation histories and live revocation.
Folder tests prove one source reference, bounded paths, real device validation,
atomic save/rollback, same-key retry and no device substitution. Whole-VPS tests
prove all three discovery modes point to the same resource and do not require a
fictitious local Project.

Execution checks cover Local/SSH one-shot, persistent Shell and PTY, cancellation,
original-operation recovery and terminal uncertainty without replay. UI checks
include independent workspace pages, global overviews that enumerate only live
resource bindings, held clicks, stale replies, uncertain writes, dirty-form leave
confirmation, responsive layouts and actual OIDC-style root-to-workspace hash
navigation while an earlier read is in flight.

A browser routed into a disposable ASGI app must forward only that browser
context's cookies. A shared HTTP test client's cookie jar must never inject a
previous test user's identity. Synthetic fixture cookies and transport overrides
are not production credentials and must not be included in published evidence.

## Separate deployment boundary

Merge acceptance does not deploy the application or certify a particular fleet.
Use a separate empty `HUB_DATA_DIR`, initialize the new account/workspaces and
create new Profiles and Connections. Upgrade Hub and intended Runners together;
a missing/old tool handshake is rejected rather than inferred as compatible.

Real NFS mounts, device folder mappings, SSH Host aliases/keys/ProxyJump and an
operator's actual identity-provider configuration require explicit deployment
validation. Shared paths must already expose the intended bytes; CodePier does
not synchronize unrelated clones or turn full Shell/SSH into a directory sandbox.
Never delete an old data volume, project folder, shared export or VPS contents to
make the fresh-schema startup succeed. Publishing a release, replacing a running
service and initializing production storage remain separate authorized actions.
