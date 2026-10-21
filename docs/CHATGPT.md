# Connect an MCP client

Use the Hub's HTTPS `/mcp` endpoint. Complete OAuth by selecting a workspace first,
then a Profile in it. Review fixed/live permissions before confirmation. The MCP
scope is `codepier.workspace`; URL parameters cannot choose an identity.

The client lists normal Projects and VPS entries exposed by its Connection.
Ordinary commands omit `execution_device_id`; do not query execution choices as a
preflight. Only ask `get_execution_options` when the user requests location details
or a different machine. Keep original operation/session IDs after uncertain outcomes.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.

## Copy the connection address

Workspace → **MCP Connections** displays a read-only **MCP Server URL** and a copy
button even before any Connection exists. It uses the Hub's configured public
MCP/OAuth address, not the current browser origin or a Space/Profile query string.
Use that plain `/mcp` endpoint in the client and select OAuth with dynamic client
registration (DCR), followed by workspace and Profile consent. Do not append
`authorization=role`, `space` or `profile` selectors. After replacing the Hub's
database, create a fresh client registration rather than reconnecting a remembered
client ID from the old database. Copying the address neither creates a Connection
nor discloses a token.
