# Upstream 1.14.3, IAM and multi-MCP integration

This is an unreleased development integration, not an upstream release or deployment.
It combines upstream `84e24d53b33ca24e3d54609f65df8c14c347f29e` with the IAM/Gateway
branch `68aa9c8d2bf279b4579779f304b28410f23a3fe9`. The merge preserves both parents;
all 19 upstream commits after the shared 1.13 baseline are included in its ancestry.
A Git ancestry check does not substitute for runtime acceptance.

## Architecture now in use

- `hub/app.py` composes services and routers. Accounts, devices, projects, activity,
  settings and system APIs live under upstream `hub/api/`; IAM/Profile/Role/OIDC and
  Gateway management are separate routers. OIDC and Gateway participate in shutdown.
- `hub/principal.py` owns the single Principal type and original credential fences.
  Authentication, Space membership, user epoch, identity freshness, Profile and Role
  remain separate authority checks. An async scheduling wait never renews authority.
- `hub/store.py` uses upstream ownership-checked SQLite access, a single DB worker,
  cancellation draining and nested rollback-only transactions. IAM migrations remain
  transactional. Pure database endpoints run through `database_endpoint`; network
  operations split admission and completion into separate synchronous phases.
- `hub/gateway/router.py` provides one public catalog and explicit native/remote
  resolution. Unknown names cannot become an arbitrary backend fallback. Runtime
  remains responsible for native Agent operations; Gateway does not pretend remote
  MCP calls are durable Agent operations or exactly-once executions.
- Upstream resource scheduling, wire epochs, call logging, native file-import
  checks and native process cleanup are retained. New call-log projections, watch
  updates and details enforce the fork's Space/private-record visibility rules.
- OIDC discovery/cache writes, login state consumption, entitlement scheduling,
  refresh-token compare-and-swap and reconciliation use the Store worker. A failed
  or cancelled sync never extends entitlement freshness.
- Upstream key rotation also covers provider secrets, upstream identity tokens,
  pending PKCE verifiers, backend accounts and encrypted Gateway results. Gateway
  cursor/idempotency identity uses a persistent encrypted seed, not the current
  master-key bytes, so rotation does not create new request identities.
- Frontend sources include IAM and Gateway modules through `index.source.html`.
  The native MCP Apps and panel content-hash manifests are rebuilt with the locked
  frontend tooling. Do not hand-edit generated HTML or asset inventories.

## Deliberate public API changes

The native public catalog is the nine upstream tools plus two identity tools:

```
workspace read write edit exec process vps browser computer
get_profile get_access_context
```

Role and fixed-mode native catalogs share this shape. Authorization is determined
by the credential, not a `profile`, `authorization`, Space or account selector in
untrusted tool arguments. Existing `full`/`coding` query values remain harmless
presentation aliases for the compact catalog; they do not restore old tools.

`devices_list` and `projects_create` are private implementation contracts. Public
calls now use:

```json
{"name":"workspace","arguments":{"operation":"devices"}}
{"name":"workspace","arguments":{"operation":"project_create","idempotency_key":"create-intent-001","options":{"alias":"example","device_id":"DEVICE_ID","root":"/approved/path","mode":"write"}}}
```

Read the exact schema and required role action through
`workspace(operation="help", tool="workspace", action="project_create")`.
The model cannot use this operation to bypass local path validation, delegation
limits, canonical-path checks, mapping overlap protection or post-wait revocation.
Old public native aliases are rejected, not silently replayed under new semantics.

External MCP tools retain reviewed schemas under `namespace__tool`, plus an
explicit receipt lookup tool. `get_profile` remains the authoritative connection
identity; external identity metadata cannot replace it. An external catalog is
paginated and permission-filtered. Clients must rediscover the changed catalog.

## Fresh setup and security settings

No deployment/data upgrade is performed by this change. Fresh databases create IAM
schema 10 and Gateway schema 1. Existing safe migrations are retained, but old
application binaries are not a supported downgrade path for new Role policies.
There is no automatic data reset and no shared production state in tests.

Gateway routing is enabled by default, but without configured, reviewed and
explicitly delegated connectors it exposes no external account capability. Setting
`CODEPIER_MCP_GATEWAY=0` is an instance security switch; it is not a switch back to
the old MCP architecture.

Fixed grants remain useful for bounded consent; dynamic Role grants remain useful
for continuing delegation. External-MCP delegation still requires explicit human
consent. This is a security boundary, not a promise that arbitrary new backend
accounts or scopes can be authorized without their owners' approval.

See [MCP Gateway](MCP_GATEWAY.md), [multi-user/OIDC](MULTIUSER_OIDC.md),
[Profiles](ACCESS_PROFILES.md) and [Roles](DYNAMIC_ROLES.md).

## Validation and remaining acceptance

Regression tests cover the upstream native contract and file/command path, the
fork's IAM/OIDC and cross-Space rules, Gateway transport/no-replay behavior, SQLite
worker credential revocation, private call-log projections, key rotation of all
new secret stores and validator cancellation during process creation.

Local runs use Python 3.13 and the supplied pinned dependency wheels. Hash-locked
requirements resolution, frontend formatting/build, Ruff, the configured incremental
mypy gate and source-release consistency are checked separately from pytest. Test
logs/JUnit files in the delivery package record exact completed runs; repeated
runs are not independent test coverage.

Chromium desktop/mobile tests exercise the actual Hub API and DOM. Some Gateway
DOM tests use a real ASGI app through a browser routing adapter; these are distinct
from real TCP Hub/Agent tests. Local WebKit lacks system libraries and must be
validated in CI. Windows/macOS, full multi-shard regression, real Authentik and
actual private MCP providers are not certified by the local subset.

Backend OAuth browser authorization/refresh, stdio runners, MCP Apps/resource
proxying, sampling, elicitation and arbitrary backend resource adapters remain
outside the first Gateway implementation. A reviewed tool allowlist is not an OS
sandbox or a generic per-repository permission engine.
