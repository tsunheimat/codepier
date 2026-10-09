# Audit sessions and automatic correlation

The normal destination is **Audit** (`/#audit`): **All operations**, **By session** (`/#audit/sessions`) and **All events**. The former `/#conversations` bookmark redirects to By session. There is no primary Conversations page, manual registration form, second chat interface or return-to-chat dashboard. Resource details can still link to a session's Audit timeline.

Continue issuing instructions and reading answers in the chosen chat client. CodePier observes only calls that reach CodePier: an open ChatGPT tab that has never called it is invisible. Silence does not prove thinking, tab activity or completion of the conversation.

## Automatic identity

On the first authenticated relevant MCP request carrying `_meta["openai/session"]`, CodePier creates the association automatically, before tool resolution. Subsequent calls use the same persisted identity. No title, visible ChatGPT URL or manual setup is required. The [OpenAI plugin reference](https://developers.openai.com/plugins/reference), section “_meta fields the client provides”, describes this as an anonymized conversation identifier. It is not the identifier in a visible `chatgpt.com/c/...` URL; CodePier neither constructs a URL nor retrieves chat messages from it.

This is **simulated host metadata**, not evidence from a real ChatGPT Web account:

```json
{
  "name": "exec",
  "arguments": {
    "project": "ProjectA",
    "command": "printf example",
    "yield_seconds": 0,
    "idempotency_key": "explicit-command-001"
  },
  "_meta": {"openai/session": "host-provided-anonymous-identifier"}
}
```

The key remains authenticated Space + user + connection grant + platform + client identifier. Ten conversations sharing one connection remain ten sessions. Another grant, even with the same Profile and identifier, has separate history. Tokens, user IDs, HTTP request IDs and MCP transport session IDs are never substituted for conversation identity. Metadata never selects authority, credentials, a role, a Space or an execution route.

Other clients may supply the existing `codepier/conversation` extension with `platform`, `conversation_identifier`, optional `label` and optional `original_url`. With a valid `openai/session`, the extension contributes only optional label/URL. Known credential patterns in identifiers are rejected; labels are redacted. Invalid optional URLs or labels do not discard an otherwise valid host identity. URLs require HTTPS, permitted public syntax, no embedded credentials and, for ChatGPT, a recognized conversation path. URLs are secondary information in the timeline's correlation evidence, not a normal setup step.

Missing, invalid or unsupported metadata does not break normal tool use. Such calls remain under **Unassociated activity** with the observed reason. Older receipts and panel operations also appear there if there is no visible association. No synthetic session is manufactured. Administrative ability to view a native receipt does not reveal another user's session metadata.

## What a session card means

Cards have a compact stable local identifier, platform or optional label, connection reference, last observed activity, and the current or most recent tool/action and resources. The active count aggregates all visible linked receipts before pagination. A session can have simultaneous operations on several projects and MCP services. Each operation displays its actual resources; cumulative historical associations and a global active project are never used as the current project.

Active, uncertain and recent records are separated. Filters cover observed state, current/latest project, tool and session identifier/label. Up to 30 sessions appear per page; each card previews four current operations, with a count and timeline link for the remainder. The API returns up to 40 current items per card and an explicit truncation flag. A session with only historical association metadata is labelled accordingly.

The timeline uses the existing Audit call-log detail for native output, result, trace, timing and receipts. Gateway detail shows the authorized, redacted saved call result. Immediate/read calls retain only their method, tool/action, authorized resource/receipt references, timestamps, diagnostic IDs and outcome; they do not create another copy of arguments, file bodies, chat content or responses. Existing event Audit and CSV export remain available.

A native receipt in queued/running/reconnecting/cancelling stays active after its HTTP response returns. Unknown, interrupted and needs-review receipts remain visible as uncertain. A gateway `completed` receipt means **external call returned**, never proof that an upstream asynchronous job completed. Only an actual future authorized observation can establish that job's result. The UI does not infer a whole-conversation lifecycle.

## API, authority and persistence

- `GET /api/audit/sessions`: owner-private session projection in the current Space. Filters: `state=active|attention|recent|failed`, `project`, `tool`, `q`, `limit`, `offset`. It reports unassociated activity count and observation write-error count.
- `GET /api/call-log`: native receipts, private gateway receipts, and immediate/error observations. Optional `session=<con_id>` and `correlation=unassociated` combine with existing source/status/project/tool/text filters and stable creation-time cursor pagination.
- `GET /api/call-log/{id}`: existing authorized native detail, gateway result, or an `act_` observation. Observations link to authorized original receipts. Native/gateway detail includes up to 100 owner-visible call observations and request IDs for correlation checks.
- Relevant MCP tool results include `_meta["codepier/activity"]` with the local observation `id`, `session_id` (nullable), and `correlation`. This is diagnostic evidence, not a credential or permission grant. An HTTP request ID remains separately scoped to a single request.
- `GET/POST/PUT /api/conversations` and the `conversations` tool remain compatible for clients and integrations with valid association data. They are not required in ChatGPT's normal flow. Panel-created `user:<id>` records and automatic `grant:<id>` records are deliberately distinct; entering the same string does not bind a grant.

Session metadata and gateway results stay owner-private, including from Space or instance administrators. Existing native Audit visibility remains unchanged: instance administrators may read native receipts within the selected Space under the existing record policy. Native output/trace/receipt reads still apply original operation, resource, execute/computer and private-output checks. Current grant permissions are checked by the underlying tool paths; association never widens them. An account cannot copy receipts from another grant into a session.

The additive SQLite migration adds `audit_activity`, `audit_activity_resources`, `audit_activity_receipts` and lookup indexes. Existing conversations, resource associations, audit events, operations, credentials and encryption keys are untouched. Each request uses ContextVars propagated to the database worker; middleware resets them at the request boundary. Admission records each receipt and its actual resources. Idempotent replays keep their original operation; multiple observations can refer to it without another execution.

Authenticated tools/call (including resolution, validation and authorization failures), resources/read, prompts/get, and supported Tasks methods are observed. Panel tools/call gets an unassociated observation. Handshake/discovery traffic, malformed JSON before a usable authenticated request can be parsed, unauthenticated requests and unrelated management HTTP routes are not converted into sessions. The existing request diagnostics and event Audit cover their own scopes. There is no retroactive reconstruction of old immediate calls.

HTTP call outcomes are separate from durable receipt states. Startup marks unfinished call observations unknown (`HUB_RESTARTED`); native and gateway recovery keep their existing behavior. Observations and associations survive restart and have no new automatic history deletion. Gateway payload receipts still use their existing seven-day/quota retention; if an original receipt is pruned, its call observation remains, with resource references but no recovered output. Polling these read-only views does not create more observations.

The session view polls every five seconds and responds to existing native SSE invalidations. It pauses while hidden, allows manual/pause controls, protects focused/selected content, and rejects stale responses across navigation, logout and Space changes. Operation polling preserves expanded trace/output views. Gateway/immediate completion is covered by polling even without a native SSE event. No scheduler, goals, steps, summaries, checkpoints or transcript store has been added.

## Genuine ChatGPT Web acceptance still required

Repository tests simulate host metadata. They do not prove a particular real ChatGPT Web account or connector sends it. No real account/browser credentials or live sample were available for this implementation; no deployment or live fixture creation is part of this work.

After a separately authorized deployment, an operator can verify the observable behavior:

1. Use an already authorized ChatGPT connection. In each of ten genuine conversations, make a permitted CodePier call. Do not register conversations or copy URLs. Record only the non-sensitive `codepier/activity` IDs, local session IDs and original native/gateway receipt IDs.
2. Open Audit → By session as the same user in the same Space. Confirm ten distinct cards; repeat calls within each original chat must keep the same local session. If metadata is absent, verify the explicit unassociated reason instead of manually manufacturing an identity.
3. Submit allowed overlapping work across two projects and an approved MCP binding. Compare each card's actual operation/resource pairs with original receipts. A returned native submission must remain active until its receipt ends; a downstream asynchronous submission must not be displayed as job completion.
4. Open the session timeline and original receipt. Verify state/output/trace updates, including a safe read/error case, and verify history after a separately authorized restart. No reset is needed.
5. Check another user, Space and grant. Session metadata and private gateway output must stay private; native administrator access must remain exactly the existing policy. Do not share tokens, chat content or raw credentials in acceptance evidence.

## Workflow removal

The workflow/archive subsystem has been removed. `/#conversations/archive` and `/#workflows` no longer resolve to an archive. No `workflows_*` tool, workspace `workflow_*`/`handoff` operation, project-query workflow facade, workflow sharing API, or `rd://workflow` guidance alias is supported. Unknown tools and unsupported operation arguments receive the normal protocol errors; there is no compatibility read service or `WORKFLOW_RETIRED` write handler.

`workspace_status` and `project_query(operation="dashboard")` read authorized recent operation receipts only. They reject the removed `workflow_id`, `workflow_cursor` and `evidence_offset` arguments and return no workflow, steps, progress, checkpoint evidence or archive pagination. Use `task_query` for existing operation status/history and `process(operation="cancel")` for cancellation.

Fresh stores create no workflow tables. On upgrade, any existing `workflows`, `workflow_events` and `workflow_replays` tables, indexes and rows remain inert and unchanged; startup neither deletes nor migrates them, and no product API reads or writes them. They are not converted to conversations. Existing IAM migration and Personal Space safety checks continue to preserve populated historical Spaces. No live reset or data deletion is required.

`rd://conversations` is the current guidance resource. Source baselines, immutable reviews, validation receipts, artifacts, browser/computer sessions, native CLI, audit and operation idempotency retain their own contracts. Stable identities, Profiles, grants and consent ceilings remain supported.
