# Start with the workspace development build

Use a separate empty Hub data directory and initialize it explicitly. The new
schema refuses older databases without converting or deleting them. Do not point
this development branch at a live data volume. Register/pair Devices, then create
a workspace and assign its resources, folder mappings and shell defaults.

Create Projects from those Folders. Configure direct member policy and a workspace
Profile, then create/authorize a Connection through workspace-first selection.
Read the full guide before deployment; this branch is not a released upgrade.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.
