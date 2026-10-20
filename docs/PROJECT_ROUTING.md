# Workspace folders, shared storage and execution

Shared Storage inventory maps a logical base to existing per-Device mount paths.
A workspace imports selected mappings with required default shell and files devices.
Folders use direct Device directories or a workspace storage source/subpath.
A Project has one Folder and `.` or a bounded descendant; there is no stored legacy
Device/root alternative. Device-side realpath/symlink validation remains required.

Default shell inherits Project → workspace → this Project's source. Allowed
options always come from that folder's provided workspace mappings. Unselected
additional restrictions mean all those eligible mappings, not all global machines.
Changing a default never retargets accepted work and offline defaults do not fail over.
Structured cwd/file paths are mapped; arbitrary shell strings/stdout are not rewritten.

Configured test recipes and before-dispatch RCH hooks remain separate from the
chosen launcher. A normal shell string is not guessed to be a test by a regex.
Shared-source holds use source identity/path across workspaces, not mount spelling.
They do not lock external writers or imply identical clones were synchronized.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.
