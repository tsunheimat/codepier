# Conversation associations

**Conversations** (`/#conversations`) indexes where resource calls occurred. Users continue reading responses and giving instructions in their selected chat client. This registry stores no chat messages, final answers, generated summaries, goals, steps, progress, checkpoints, acceptance checklists or next actions. Existing native CLI history is not copied into it.

A record contains platform/client identity hints, a client-supplied conversation identifier, an optional display label and original HTTPS URL, authenticated user/Space/connection/Profile provenance, references to resources and existing operation receipts, and first/last activity. One conversation can reference multiple projects, VPS and MCP bindings. Concurrent conversations have separate associations; there is no global active project.

## Optional host correlation

The [official OpenAI reference](https://developers.openai.com/plugins/reference) documents `_meta["openai/session"]` as an anonymized conversation identifier for correlating calls within one ChatGPT session. CodePier stores that identifier without treating it as the identifier in a visible `chatgpt.com/c/...` URL. It neither constructs a URL from it nor uses it to fetch a transcript or final answer.

The following is a **local simulated host metadata** example, not evidence of acceptance by an actual ChatGPT account:

```json
{
  "name": "exec",
  "arguments": {
    "project": "ProjectA",
    "command": "printf example",
    "yield_seconds": 0,
    "idempotency_key": "explicit-command-001"
  },
  "_meta": {
    "openai/session": "host-provided-anonymous-identifier",
    "codepier/conversation": {
      "label": "Optional display label",
      "original_url": "https://chatgpt.com/c/actually-supplied-visible-id"
    }
  }
}
```

`codepier/conversation` is a CodePier extension. Other clients may supply `platform`, `conversation_identifier`, optional `label`, and optional `original_url` in that object. When `openai/session` is supplied, it determines the correlation identifier/platform; the extension can supply only the label and URL. Neither hint selects authentication or permissions.

URLs are separately validated: HTTPS, no user/password, whitespace, backslash, nonstandard port or local/private address. ChatGPT records require a recognized conversation path on `chatgpt.com` or `chat.openai.com`. Validation checks syntax and allowed origin/path, not ownership or remote reachability. A bad optional metadata URL is ignored while a valid host identifier may still correlate calls. Explicit UI/API URL submissions return an error atomically instead of saving an invalid link.

Missing or unsupported metadata leaves tool use intact and creates no fabricated identity. The panel's **Associate existing conversation** action or the `conversations` tool can index a real identifier provided by the client/user. This is association metadata, not a prompt or task description.

## Isolation, persistence and receipts

Lookup keys include authenticated Space, user and connection grant, plus platform and identifier. The same identifier in another grant (even one using the same Profile) creates a separate record. A grant can read only its own records; a panel user can read their own records in the current Space. Shared roles or an instance-administrator label do not expose another user's conversation index.

Each resource/operation association is authorized. Existing operation access and extra execute/computer/MCP tool gates apply when reading references; revoked resource access filters records from the view. Operation receipts cannot be copied into a conversation belonging to a different grant. Profile/connection provenance is server-derived and cannot be submitted as a conversation identity field.

Associations are SQLite records and survive Hub close/reopen. Admission links native and MCP receipts to the call's own correlation context; repeated execution keys retain the original receipt. Registry failures do not discard execution receipts; the runtime records a diagnostic write-error count. The separate timing observer's in-memory salt still resets timing windows on restart; it is not the conversation registry.

The detail panel displays permitted existing operations, with a bounded first 100 references; native records open the original operation viewer. MCP calls retain their private gateway `call_id` and recovery API. A supplied URL enables **Return to original conversation**. There is no generic API for obtaining a chat client's messages.

## Workflow removal

The workflow/archive subsystem has been removed. `/#conversations/archive` and `/#workflows` no longer resolve to an archive. No `workflows_*` tool, workspace `workflow_*`/`handoff` operation, project-query workflow facade, workflow sharing API, or `rd://workflow` guidance alias is supported. Unknown tools and unsupported operation arguments receive the normal protocol errors; there is no compatibility read service or `WORKFLOW_RETIRED` write handler.

`workspace_status` and `project_query(operation="dashboard")` read authorized recent operation receipts only. They reject the removed `workflow_id`, `workflow_cursor` and `evidence_offset` arguments and return no workflow, steps, progress, checkpoint evidence or archive pagination. Use `task_query` for existing operation status/history and `process(operation="cancel")` for cancellation.

Fresh stores create no workflow tables. On upgrade, any existing `workflows`, `workflow_events` and `workflow_replays` tables, indexes and rows remain inert and unchanged; startup neither deletes nor migrates them, and no product API reads or writes them. They are not converted to conversations. Existing IAM migration and Personal Space safety checks continue to preserve populated historical Spaces. No live reset or data deletion is required.

`rd://conversations` is the current guidance resource. Source baselines, immutable reviews, validation receipts, artifacts, browser/computer sessions, native CLI, audit and operation idempotency retain their own contracts. Stable identities, Profiles, grants and consent ceilings remain supported.
