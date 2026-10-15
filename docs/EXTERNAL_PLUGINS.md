# External plugins

CodePier external plugins are **declarative, project-aware adapters over approved MCP tools**. They do not run an autonomous agent, install another MCP server, receive backend credentials, or enter the device execution queue.

The version-one implementation supports JSON manifests, reusable space integrations, project attachments, explicit action grants and target consent, generated management forms, and existing gateway receipts. Python extension code, arbitrary HTTP transports, workflow orchestration and external task scheduling are not implemented.

Shared NFS project management, per-action devices, execution hooks and RCH are
implemented separately; see [Project routing](PROJECT_ROUTING.md). External
plugin attachments select remote service resources, not source-device ownership.

## Model

| Object | Ownership and purpose |
| --- | --- |
| Plugin manifest | Installed by the deployment administrator; defines schemas, actions and argument mappings. |
| Space integration | A named instance such as `gitlab-main`; selects reviewed backend bindings and tool slots once. |
| Project attachment | Associates an integration with a CodePier project and supplies external resource IDs. |
| Plugin rule | Explicit integration/action/project or integration/action/space permission, independent of direct MCP rules. |
| Plugin consent | A grant-private snapshot of the currently reviewed actions, mappings and targets. |

Several projects can use one integration without duplicating accounts or credentials. Detaching a project disables its attachment; it does not delete the project, shared integration, backend account or external repository.

Project-scoped actions require both an explicit plugin rule and current read access to the selected CodePier project. Space-scoped actions do not infer a project and cannot receive a project selector. A space action must still map to explicitly configured external targets; it does not mean access to all resources in the backend account.

No source-storage abstraction or NFS synchronization is introduced here. Devices may access the same NFS working tree through their existing mappings. External project IDs describe remote service resources, not filesystem locations.

## Panel setup

1. In **Resources → MCP Services**, register a service, configure its backend account, discover tools, and review/publish the required tools as an approved binding. Use the existing gateway flow; plugin configuration does not discover or publish tools automatically.
2. In the **External plugins** section of the same page, choose **Add plugin integration**, select the installed contract, set a stable alias, and map every declared tool slot to an exact approved backend tool. Review the contract and mappings before saving.
3. Select **Project bindings** on the integration, choose a CodePier project and fill the generated fields, such as a GitLab project ID. Repeat for additional projects. The detach option preserves shared resources.
4. Select **Role actions**, choose an existing role and one explicit project/space scope, and select its actions. Other role rules are preserved. Add project read permission separately when necessary; this form never adds direct MCP access.
5. Create a client connection from the role in **Access → Client Connections**, with external MCP delegation explicitly enabled. On that connection select **Plugin action consent**, read the actual external target IDs, backend account and tool, and approve the current selection.

The panel displays effective plugin actions separately from direct MCP tools. Role summaries also show plugin rules. Integration/attachment updates use expected versions. Changes to the selected target or manifest invalidate previous plugin consent rather than silently following the new target. Re-enabling or reattaching does not resurrect the old consent.

Space administrators configure integrations and attachments. Only the client connection's owner can approve or revoke its plugin consent. The catalog is readable by authenticated panel users, but installation remains a deployment operation. These management APIs do not become model-callable administration tools.

A grant with a broader direct MCP rule on the same account can bypass a plugin's narrower mapping. The consent preview warns about that configuration; remove unnecessary direct rules in the role editor. Plugin rules themselves never publish the raw backing tools to the caller.

## Write a plugin without Python

A manifest uses the same reviewed JSON Schema machinery as the gateway. Schemas must be object-rooted and explicitly set `additionalProperties: false`. All mapping sources select declared top-level fields.

```json
{
  "api_version": 1,
  "id": "pipeline-reader",
  "version": "0.1.0",
  "title": "Project pipeline reader",
  "project_config_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["remote_project_id"],
    "properties": {
      "remote_project_id": {"type": "integer", "minimum": 1}
    }
  },
  "backend_slots": {
    "gitlab": {
      "transport": "mcp",
      "tool_slots": ["pipeline_get"]
    }
  },
  "actions": {
    "pipeline.get": {
      "title": "Get a project pipeline",
      "scope": "project",
      "effect": "read",
      "input_schema": {
        "type": "object",
        "additionalProperties": false,
        "required": ["pipeline_id"],
        "properties": {
          "pipeline_id": {"type": "integer", "minimum": 1}
        }
      },
      "call": {
        "backend": "gitlab",
        "tool_slot": "pipeline_get",
        "arguments": {
          "project_id": {"from_project": "remote_project_id"},
          "pipeline_id": {"from_input": "pipeline_id"}
        }
      }
    }
  }
}
```

An omitted `space_config_schema` defaults to an empty closed object. This example's backend must accept integer `project_id` and `pipeline_id` parameters. Different MCP implementations may use different names or types: adapt and review the manifest rather than assuming every GitLab server implements this exact schema. The bundled `gitlab.json` adds bounded pipeline listing with a literal page size of 20.

Tool slots such as `pipeline_get` are plugin-local identifiers. At integration setup they are mapped to exact names from a reviewed MCP binding, and CodePier pins their schema fingerprints. Structural compatibility is checked on save; actual mapped arguments are fully validated by the gateway's bounded validation worker before invocation. Saving a mapping is not proof that a real external account or live service works.

### Mapping sources

| Selector | Value source |
| --- | --- |
| `from_input` | A declared action input field. |
| `from_project` | A field in the selected authorized project's attachment. |
| `from_space` | A field in the selected space integration configuration. |
| `literal` | An explicit JSON value in the reviewed manifest. |

Each backend argument has exactly one selector. There is no expression evaluator, arbitrary interpolation, nested-path language, network call or shell hook. A fresh argument object is constructed; arbitrary caller JSON is never merged into trusted target fields. Missing optional input fields remain omitted. Schema defaults are not silently applied during execution; use a literal when an argument must always be present.

For a project-scoped action the public schema adds the reserved `project` selector. A client calls a name such as:

```text
ext.gitlab-main.pipeline.get
  project: codepier
  pipeline_id: 456
```

The caller cannot replace the external repository ID, account, credential or endpoint. The namespace includes the integration alias, allowing several instances of the same plugin. Long names have a deterministic suffix and catalog collisions are rejected.

`effect` can be `read`, `write` or `execute`; it is reviewed descriptive metadata, not a sandbox or automatic permission. Public tool annotations remain conservative. Even an apparent read action uses a separately authorized backend account and tool.

Configuration should contain identifiers and non-secret settings only. Credentials belong to the existing gateway accounts. Never put tokens in space/project config: configuration is intentionally shown to administrators and consenting users.

### Installation and offline validation

Validate the contract without connecting to a service:

```bash
.venv/bin/python -m hub.external_plugins path/to/plugin.json
```

The command returns the manifest fingerprint and generated public action schemas. It imports no plugin Python code and does not authenticate or invoke backend tools.

To install additional declarative plugins, configure this **deployment-owned** environment variable:

```text
CODEPIER_EXTERNAL_PLUGIN_DIR=/opt/codepier-plugins
```

Mount that directory read-only in the Hub container. It must be an existing absolute non-symlink directory; individual manifests must be regular, non-symlink UTF-8 JSON files. Do not point it at an agent-writable project directory or a writable NFS source root. Changes require a Hub restart and subsequent integration review. No plugin installation from the model-facing tool surface is supported.

Only immediate `*.json` files are loaded. Python files and entry points are never imported. There can be at most 31 additional manifests plus the bundled plugin; each manifest is limited to 256 KiB. Duplicate IDs, unsupported contracts and malformed manifests fail startup rather than being ignored. Each plugin can declare up to 8 backend slots and 32 actions.

## Management API

All paths below use the existing authenticated panel session, selected space and CSRF checks for writes. Do not pass secrets through these APIs.

| Method and path | Purpose |
| --- | --- |
| `GET /api/external-plugins` | Installed manifests and hashes. |
| `GET /api/external-integrations` | Current space's integration inventory; space administrator only. |
| `PUT /api/external-integrations/{alias}` | Create/update; `expected_version: 0` creates, a current version updates. |
| `PATCH /api/external-integrations/{id}` | Enable/disable with an expected version. |
| `GET /api/external-integrations/{id}/projects` | List project attachments. |
| `PUT /api/external-integrations/{id}/projects/{project_id}` | Create/update a schema-validated attachment. |
| `DELETE /api/external-integrations/{id}/projects/{project_id}?expected_version=N` | Disable the attachment without deleting underlying resources. |
| `GET /api/external-integrations/grants/{grant_id}/consent` | Exact current action/target/account preview and review hash. |
| `POST /api/external-integrations/grants/{grant_id}/consent` | Explicit approval using `confirmed`, `expected_role_version` and `review_hash`. |
| `DELETE /api/external-integrations/grants/{grant_id}/consent` | Revoke this grant's plugin consent. |

An integration request selects the reviewed manifest and backend slots:

```json
{
  "plugin_id": "pipeline-reader",
  "manifest_hash": "<hash from the installed catalog>",
  "expected_version": 0,
  "config": {},
  "backends": {
    "gitlab": {
      "binding_id": "<approved gateway binding ID>",
      "tools": {"pipeline_get": "<exact approved backend tool name>"}
    }
  }
}
```

A role action rule is separate from `connector_rules`:

```json
{
  "integration_id": "<space integration ID>",
  "scope": "project",
  "projects": ["<CodePier project ID>"],
  "actions": ["pipeline.get"]
}
```

A space rule explicitly uses `scope: "space"` and `projects: []`. There are no wildcard project, integration or action selectors. A role is limited to 256 explicit plugin action/resource pairs.

## Execution, recovery and privacy

Resolution checks the current authenticated identity, role/grant, project read access when required, attachment, plugin consent, backend account access, reviewed tool and pinned manifest. The fully mapped request enters the existing gateway validation/dispatch path. Permission and configuration versions are checked again before send and before publishing a result.

The trusted response metadata adds `codepier/plugin` beside the original `codepier/callId`. Original gateway receipts remain grant-private and include the plugin route identity. Backend-supplied metadata cannot replace CodePier's envelope. Plugin calls do not obtain direct backing-tool grants.

Repeated calls with the same `codepier/idempotencyKey` can return the original completed receipt; a key reused for another target, mapping or argument set is rejected. A transport error after dispatch or unsupported returned content can leave an unknown outcome. Read `gateway_call_get` using the original ID; never automatically replay an uncertain mutation. Receipt retention remains the gateway's existing bounded retention policy, not permanent exactly-once delivery.

Submitting an external pipeline is not the same as its successful completion. A future mutation manifest must expose a separate observation action, retain the backend job ID and respect unknown outcomes. This version's bundled actions only read/list pipelines; they do not start or cancel them.

## Upgrade and verification boundaries

The additive `external_plugin_schema=1` migration creates integration/attachment/consent tables and a nullable `gateway_calls.plugin_context`. Existing policies, credentials and receipts are not granted new authority. Fixed client connections retain their original plugin action/resource ceiling; adding an action to a role does not extend an existing fixed connection. Both fixed and dynamic connections require renewed plugin target consent when mappings or reviewed manifests change.

Back up the Hub database and matching key material before deployment. Do not downgrade a database containing new authorization policies to an older binary; rollback requires a compatible database/key backup. Development fixtures are not a live database migration or deployed-service acceptance.

Developer tests use a deterministic MCP transport and real Hub/IAM APIs. Browser tests exercise the generated forms with Chromium and WebKit through those APIs; SSE itself is outside the browser fixture. They do not certify a real GitLab service, Windows installation, installed production image or deployment.

```bash
.venv/bin/python -m pytest -q tests/test_external_plugins_contract.py \
  tests/test_external_plugins.py tests/test_external_plugins_hardening.py
.venv/bin/python -m pytest -q tests/test_external_plugins_browser.py
.venv/bin/python -m ruff check hub/external_plugins tests/external_plugin_support.py tests/test_external_plugins*.py
```

See [MCP gateway](MCP_GATEWAY.md), [resource/access model](RESOURCE_MODEL.md) and [development verification](DEVELOPMENT.md) for the underlying boundaries and full regression process.
