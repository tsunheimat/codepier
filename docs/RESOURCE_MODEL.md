# Resources, Roles / Client Connections, Conversations

CodePier is a reusable MCP bridge for ChatGPT Web and other clients. Roles are configurable permission sets; labels such as worker, reviewer or manager carry no special authority.

## Setup in the panel

1. Open **Resources** (`/#resources/projects`). Configure a Project, an execution node (`/#resources/devices`), an MCP Service (`/#resources/mcp`), or a VPS (`/#resources/vps`). Each type keeps its own settings and availability checks.
2. Open **Access → Roles** (`/#access/roles`). Pair actions with the specific resources they target. For MCP select the approved account/binding and individual tool names.
3. Open **Access → Client Connections** (`/#access/connections`). Select a role and create a Bearer connection, or copy the role OAuth URL for ChatGPT. The OAuth confirmation window can create the stable connection identity inline or reuse an existing identity.
4. Read actual permissions in Client Connections. Resource details group configuration/availability, role access, related conversations, and permitted existing operation/audit records.

Project details are the primary entry for Development Tools (exact project/workspace) and Artifacts / Downloads (selected-project filter). Space members, invitations and role-use/delegation assignment live in **Access → Space Members**. Fresh installations start with a real Personal Space; unused compatibility containers retire conservatively. See [Personal Spaces and navigation](PERSONAL_SPACES.md).

Example worker policy:

| Resource | Allowed actions |
| --- | --- |
| Project A | read, write, execute |
| Project B | read |
| Testing VPS | read, execute |
| Production VPS | read |
| MCP binding for an approved account | only the selected approved tools |

Project A execution does not authorize Production VPS execution. Testing VPS must also have an explicit Project A / Agent execution route, Project A must allow execution, and the Agent must permit Shell/SSH. SSH commands use the remote account's permissions; the project directory is not a remote sandbox. VPS read is metadata access, not permission to run a remote “read-only” command. Checking a VPS connection runs commands and requires execute.

Project/VPS and Project/MCP associations describe relationships. They never grant permission. A VPS can be associated with several projects but has one explicit execution route. A service can have several separate accounts and approved bindings; permissions never select arbitrary credentials or reveal them.

## Consent, administration and identity

The single **Account / Identity** navigation entry is `/#identity`. **My Account** contains Space selection/creation/invitations, linked login identities, re-authentication and browser sessions. Instance administrators additionally have **Users** (`/#identity/users`) and **SSO** (`/#identity/sso`) tabs. Users manages accounts; SSO manages OIDC providers, discovery, group mappings and reconciliation. The former `/#identity-admin` bookmark resolves to the SSO tab. There is no standalone admin page. Space ownership alone does not enable these tabs, and direct API calls retain instance-admin checks. Space membership/role administration remains under **Access → Members**.

New Bearer connections default to **fixed**. Their paired resource/action policy is frozen at creation and intersected with current role policy, Profile ceilings, current account rights and resource/local gates. Role additions, new projects, new VPS and new MCP tools do not expand them. Fixed MCP consent also pins the approved tool definitions; publishing changed definitions requires a new explicit connection. Disabled or removed role assignments, disabled identities/resources, revoked grants and expired credentials are rechecked.

**Dynamic** connections require an unchecked, explicit consent control for future role changes. Within this consent, later explicitly configured resources/actions follow the role; `all_projects` and `created_projects` are separate explicit selectors. There is no wildcard VPS permission or MCP tool rule. External MCP use requires separate per-grant consent; approving an account or associating resources never creates that consent.

Existing fixed grants are not converted. Their original scopes, project ceilings (including any explicitly consented future-project selector), tokens and stable Profile identities remain intact. Old fixed grants have no newly inferred VPS or external MCP permission. Grant-private operation and conversation histories remain independent even when several grants share a Profile.

Resource use, resource administration and authorization administration are distinct checks. A role named “manager” or “admin” does not become a Space/instance administrator. Existing explicitly bounded device/project creation delegation remains in the role editor's advanced controls. Model arguments and conversation metadata cannot choose a role, account owner, Space or grant.

New fixed connections snapshot resource-use rules. Existing device-read/project-creation delegation requires explicitly consented dynamic role authorization; selecting a role in a fixed connection does not confer that administrative delegation.

**Access → Advanced identity and compatibility** (`/#access/advanced`) contains Profile configuration and original fixed-grant configuration. Rebinding/disabling a Profile can invalidate credentials; it does not move private histories or revive revoked grants. Original grant, token and OAuth APIs remain supported.

## API and compatibility

- `GET/POST /api/client-connections` provides the consolidated connection view and atomic stable-identity/grant creation. Creation takes a role version, explicit consent mode and idempotency key. A replay returns the original grant ID; the token is displayed only on the first response, never persisted as plaintext for replay.
- `GET /api/resources/{project|vps|mcp}/{id}` collects type-specific configuration and permitted existing records. MCP IDs here identify approved bindings.
- `PUT /api/projects/{id}/mcp-services` associates approved bindings, with an expected-association check. VPS association APIs retain their existing concurrency checks.
- Existing bookmarks `#projects`, `#vps`, `#mcp-gateway`, `#roles`, `#connect` and `#profiles` open their corresponding consolidated tabs.
- [Conversations](CONVERSATIONS.md) indexes resource and operation associations. The retired workflow/archive subsystem, its routes and its compatibility tools are removed. Original execution receipts, state queries, cancellation, auditing and operation idempotency remain independent.

## Upgrade and rollback

The resource migration is additive and transactionally applied when a Hub opens its data directory. It has its own `resource_model_schema=1` marker alongside the existing IAM/gateway schema markers. It adds conversation/association tables, nullable grant consent snapshot fields and a nullable VPS execution-route field. It does not rewrite old policies, credentials, tokens, histories, workflow events, replay receipts or encryption keys.

Old VPS associations remain, but do not become grants or execution routes. An administrator must choose a route and configure explicit VPS rules before new SSH submissions. Previously queued association-only SSH requests are blocked before delivery; already dispatched operations retain their original receipts and recovery behavior. Changing only a resource's purpose associations no longer revokes an otherwise explicitly authorized route; changing the route, connection or credential blocks pending delivery. Cancellation still uses the original operation and cannot guarantee remote process termination.

Back up the database and matching key material before a real upgrade. Do not run an older Hub against a database containing new resource policies or fixed snapshots: old binaries do not understand these authorization gates. Rollback requires a compatible pre-upgrade database/key backup. This task exercises upgrade/restart only on isolated fixtures; it does not migrate a live database or certify a deployed image.
