# Personal Space bootstrap and Legacy retirement

Fresh installations create no Legacy Space. Local `hub init` and an explicitly configured first-login OIDC administrator receive a normal **Personal** Space and an owner membership. Instance-administrator authority is an independent user flag; being a Space owner, selecting a role or supplying a project URL does not confer it.

Each user has a stable `iam_users.personal_space_id`. Provisioning and repeated startup prefer that user's existing owned personal Space; they do not rename Legacy, create another Personal on each login, or silently change valid explicit selections. New-user admission creates no implicit Legacy membership. Existing configured group mappings still represent explicit membership policy and are evaluated separately.

Panel requests with an explicit Space header/query are checked against that exact current membership. When no selector is supplied, CodePier chooses an authorized normal Space, preferring the user's Personal Space. Legacy requires explicit selection. New resource writes always specify their authorized Space; fresh resource tables have no Legacy default. Instance-level audit events may have `space_id=NULL`, so provider startup does not manufacture a compatibility container.

The login page reads the configured bootstrap availability. With `CODEPIER_OIDC_BOOTSTRAP_ADMIN=first-login`, an enabled JIT provider and no active instance administrator, it offers the configured SSO bootstrap without requiring a local `hub init`. Admission rules, group requirements and the serialized first-administrator check still apply. Without that supported configuration, the local-recovery initialization guidance remains. This is separate from configuring the identity provider's callback.

## Upgrade behavior

The scoped migration has its own `personal_space_schema=1` marker and runs in the existing Store transaction. It does not reset databases, move projects/resources, change encrypted credentials/keys, revoke sessions or grants, rewrite operation/artifact/conversation ownership, or discard historical audit rows. It preserves the audit table's IDs, triggers and AUTOINCREMENT sequence when making unscoped instance events possible.

For the previous new-install shape containing the same user's valid Personal Space plus an unnecessary Legacy container:

1. Keep the existing Personal ID and owner membership; establish the stable pointer.
2. Inspect every SQLite table for a `space_id` column and every foreign key referencing Spaces, including differently named columns. Check keyed project-save receipts too.
3. If references are only membership controls or historical audit provenance, retire Legacy and its memberships from normal use. Keep an inactive tombstone for referential integrity and original provenance. It is absent from selectable/restorable Spaces and is never recreated on startup.

No tables are silently removed just because the known device/project counts are zero. Existing grants, policies, invitations, group mappings, operations, artifacts, workflows, native ownership/uploads, conversations, replay records or unknown Space references prevent automatic retirement. Such a genuinely populated historical installation retains its Legacy IDs, memberships and data, and old credentials remain bound to that original Space. A new user still receives only their own Personal Space unless explicit membership policy says otherwise.

**Retirement boundary:** automatic cleanup handles unused compatibility containers only. Populated Legacy remains an explicit compatibility view. Transferring or retiring its business data requires a separately reviewed operation with a complete dependency and authority plan; this change provides no implicit global transfer or provenance rewrite.

A saved explicit active Space selection remains valid. A saved selection of the newly retired unused Legacy no longer resolves; the panel selects the same user's valid Personal Space. Explicit requests for the inactive tombstone fail, rather than being redirected to another Space.

Before a real upgrade, preserve the database and matching key material under the installation's normal operating procedure. Older binaries do not understand this bootstrap/retirement policy; a rollback requires a compatible database/key backup. This source delivery tests disposable upgrades only and changes no deployed database, cluster, identity provider or credential.

## Navigation

- **Resources → Devices / Agents** (`/#resources/devices`) owns existing execution-node onboarding, state, lifecycle and project mapping. Merely navigating never starts, updates or restarts an Agent. `/#devices` redirects here.
- A Project's resource detail opens **Development Tools** for that exact project/workspace. Links use `/#project/PROJECT_ID/tools?workspace_id=WORKSPACE_ID&tab=validation`; `/#integrations` remains a project-selector compatibility entry. If the selected mapping or permission disappears on refresh, the view keeps its original context and shows an unavailable message instead of selecting another project. Drafts and original operation receipts survive navigation; the area adds no workflow tracking.
- The same Project detail opens **Artifacts / Downloads** at `/#project/PROJECT_ID/artifacts`. The list filters to that project; an authorized-project overview and the old `/#artifacts` remain available. Download URLs carry the original authorized Space. Snapshots, expiry, source-operation references and grant-private access retain their original contracts.
- **Access → Space Members** (`/#access/members`) owns memberships, invitations and use/delegation assignment. `/#members` redirects here. These remain separate from role policy, client consent and resource administration, with the same administrator gates.

The sidebar has no duplicate routine Devices, Members, Development Tools or Artifacts entries. CLI/workbench/audit and unrelated administration remain reachable. Conversations continue to store identifiers and resource/receipt associations only; artifact bodies and chat transcripts are not copied into the index.

Related public guides: [Resources and Access](RESOURCE_MODEL.md), [Conversations](CONVERSATIONS.md), [OIDC and membership](MULTIUSER_OIDC.md).
