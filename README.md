# CodePier · 码头

[![Version](https://img.shields.io/badge/version-2.0.0-2563eb)](RELEASE.json)

CodePier is a self-hosted MCP bridge. ChatGPT and other clients work directly on
explicitly authorized Projects, VPS resources and external MCP services. The Hub
manages identities/resources and dispatches work; your Runner accesses files,
launches local tools and opens SSH connections.

## Workspace abstraction — development branch

**This is a breaking development branch, not a released upgrade.** It requires a
separate fresh `workspace-1` database. It refuses older databases without modifying
or deleting them. Do not replace a running Hub's database or deploy over its data
volume. Shared folders and VPS files are independent of Hub database state.

Resources → Workspace bindings → Projects / Profiles → MCP Connections.

A workspace contains assigned resources, mappings, members and direct policies.
There are no Roles or mutable “Current Space”. A Project uses one workspace Folder
and a root/subdirectory. Its execution devices come only from that folder's
provided source. Shared Storage maps existing shared bytes on multiple Devices;
ordinary commands use the configured default without a discovery preflight or
silent fallback. Files and shell can use different mapped devices.

A Connection immutably binds one workspace and one Profile. OAuth selects the
workspace first, then its Profile. Token renewal preserves the Connection's
identity; two connections sharing a Profile keep separate private histories.
Profiles describe exact resource/action pairs and cannot expand member rights.

VPS resources enter a workspace as whole resources. Display them at the top level,
under selected Projects, or both. SSH uses a saved Runner-local OpenSSH reference;
it does not require a dummy Project. One-shot commands and original persistent
sessions/PTYs use the same target model. Unsupported platforms and uncertain remote
outcomes are reported explicitly.

## Setup and development

Read [Workspace setup and contracts](docs/WORKSPACES.md) before initializing this
branch. Register Devices and Storage, assign resources to a workspace, create
Folders/Projects, configure direct member policies and Profiles, then authorize
an MCP Connection. Filesystem mappings must already exist on the Devices.

```bash
# A separate, new directory; never point this build at an existing deployment DB.
python -m hub --data-dir /path/to/new-codepier-workspace-data init
python -m hub --data-dir /path/to/new-codepier-workspace-data run
```

Configure `HUB_PUBLIC_URL`/`MCP_PUBLIC_URL` for the actual HTTPS deployment endpoint.
The environment/deployment templates are inputs, not a production deployment done
by this branch. Keep SSH private keys and credentials on the Runner.

Focused development checks:

```bash
python -m pytest -q tests/test_workspace_model.py tests/test_workspace_policy.py \
  tests/test_workspace_http.py tests/test_workspace_integration.py \
  tests/test_execution_workspace.py tests/test_workspace_panel_contracts.py
python scripts/build_web_assets.py --check
python scripts/check_release.py
```

The full older suite still needs its deleted Role/schema fixtures ported. Template
checks are not browser acceptance. New execution-session Windows ConPTY support,
production NFS/VPS and live-IdP acceptance are not certified by this checkpoint.

[Resources](docs/RESOURCE_MODEL.md) · [Profiles and connections](docs/ACCESS_PROFILES.md)
· [Storage and execution](docs/PROJECT_ROUTING.md) · [SSH / VPS](docs/VPS.md)
· [Security](SECURITY.md) · [Development](docs/DEVELOPMENT.md)

## Version metadata

当前仓库的 `RELEASE.json` 标记为 **2.0.0 / development**。此分支未发布，也未部署。

- **Version:** 2.0.0

Source version metadata is not a published release, a passing full CI run, or a
promise that an existing installation can be upgraded in place.
