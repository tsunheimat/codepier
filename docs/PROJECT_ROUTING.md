# Workspace folders, shared storage and execution

Folder inventory identifies one Device directory. Storage Pool inventory maps a
logical base to existing per-Device mounts and owns required file/shell defaults.
A workspace imports either as one source with a path scope, capabilities and
selected mappings. A Project has one source and `.` or a bounded descendant;
a Pool does not require a wrapper Folder. Runner realpath/symlink checks remain.

Shell inherits Project → source → Pool. Files inherit source → Pool independently.
Overrides can only narrow the selected source and Pool shell limits; excluding a
required inherited default demands a valid override. Direct Folders only use their
own Device. Empty extra allowlists mean all eligible provided mappings. Separate
workspace Device bindings grant independent Device operations and are unnecessary
for source routing or VPS launchers. Defaults never retarget accepted work or fail
over. Structured cwd/file paths are mapped; arbitrary shell strings are not rewritten.

Configured test recipes and before-dispatch RCH hooks remain separate from the
chosen launcher. A normal shell string is not guessed to be a test by a regex.
Shared-source holds use source identity/path across workspaces, not mount spelling.
They do not lock external writers or imply identical clones were synchronized.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.
