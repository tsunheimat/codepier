# Security policy

当前发布分支为 **2.0.x**。The 2.0.0 workspace refactor is currently development
source, not a published/supported production release. Report security issues
privately to the repository maintainer; do not include credentials in public issues.

## Authority boundaries

MCP credentials authenticate one immutable Connection, workspace and Profile.
Members hold direct paired resource/action policies. Effective calls are bounded
by current member policy, Profile policy, connection consent and Runner-local gates.
Resource IDs, path labels, Projects and chat metadata cannot select another Space
or confer access. A shared Profile does not share private connection histories.

Workspace folder bindings bound structured file paths; full shell is **not an OS
sandbox**. Shell and SSH use their OS accounts' authority. Limit Runner accounts
and saved SSH targets appropriately. The Hub does not upload SSH private keys or
accept arbitrary model-supplied credentials, config files or host options.

Map only mounts known to expose the same storage. Abstract mapping is not mounting
or file synchronization. Shared-source coordination applies to managed operations;
it is not a distributed lock against arbitrary local programs or another Hub.

Persistent shell/PTY input cannot be constrained by a static command inspector.
Unsupported combinations fail explicitly. SSH cancellation is not proof that all
remote side effects stopped. Use original receipts and do not replay uncertainty.

Use HTTPS, retain CSRF/Origin checks, protect the database and its matching key,
and do not expose configuration, credentials or state directories as Project roots.
The fresh workspace schema deliberately refuses older databases without migration
or deletion. Backups and an explicit separate data directory remain deployment
administrator responsibilities. Never run older binaries against the new schema.

See [workspace contracts and current verification limits](docs/WORKSPACES.md).
