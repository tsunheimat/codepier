# Whole-workspace VPS and saved OpenSSH targets

VPS metadata is inventory. Assign the whole resource to a workspace with its
Runner and registered `connection_ref`. Top-level execution needs no Project or
launcher-side folder. The SSH account, not the UI category, defines OS authority.

`display_mode` is `top_level`, `project_only` or `both`. Project modes require at
least one same-workspace Project. Both entries reference the same binding and
permissions; placement is not separate authorization or a project-directory sandbox.
`vps` discovers top-level entries; `vps(project=...)` discovers Project entries.

The Runner uses system OpenSSH Host aliases and optional absolute local config
files. IdentityFile, ssh-agent, ProxyJump and known_hosts remain with OpenSSH.
There is no password/sshpass adapter. `exec`/`process` perform one-shot work;
`session` owns persistent shells/PTYs and original operation receipts. Do not infer
remote termination from local SSH client cancellation. See the full guide for
configuration, current platform support and uncertainty semantics.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.
