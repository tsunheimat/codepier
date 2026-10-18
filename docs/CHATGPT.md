# Connect an MCP client

Use the Hub's HTTPS `/mcp` endpoint. Complete OAuth by selecting a workspace first,
then a Profile in it. Review fixed/live permissions before confirmation. The MCP
scope is `codepier.workspace`; URL parameters cannot choose an identity.

The client lists normal Projects and VPS entries exposed by its Connection.
Ordinary commands omit `execution_device_id`; do not query execution choices as a
preflight. Only ask `get_execution_options` when the user requests location details
or a different machine. Keep original operation/session IDs after uncertain outcomes.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.
