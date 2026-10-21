# CodePier · 码头

[![Version](https://img.shields.io/badge/version-2.0.0-2563eb)](RELEASE.json)

CodePier is a self-hosted MCP bridge. ChatGPT and other clients work directly on
explicitly authorized Projects, VPS resources and external MCP services. The Hub
manages identities/resources and dispatches work; your Runner accesses files,
launches local tools and opens SSH connections.

## Workspace abstraction — 2.0 hard cutoff

**CodePier 2.0 uses a fresh-start model, not an in-place upgrade.** Initialize a
separate empty `HUB_DATA_DIR` with the `workspace-2` schema and create new workspace
bindings, Profiles and Connections. Older or mixed-model databases are refused,
never converted or deleted. Do not point the new Hub at a previous data volume.
Shared folders and VPS files are independent of Hub database state and remain
untouched. A source merge does not deploy, publish a release, or replace a running
Hub; deployment is a separate explicit operation.

Resources → Workspace bindings → Projects / Profiles → MCP Connections.

A workspace contains assigned resources, mappings, members and direct policies.
There are no Roles or mutable “Current Space”. A Project uses one workspace source (Folder or Storage Pool)
and a root/subdirectory. Its execution devices come only from that source. Shared Storage maps existing shared bytes on multiple Devices;
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
branch. Register Devices and Folder/Storage Pool resources, import sources into a workspace, create
Projects, configure direct member policies and Profiles, then authorize
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

Use the complete regression workflow for source acceptance; template checks alone
are not browser acceptance. Native Windows, production NFS/VPS and live identity
provider acceptance require their own platform and deployment checks.

[Resources](docs/RESOURCE_MODEL.md) · [Profiles and connections](docs/ACCESS_PROFILES.md)
· [Storage and execution](docs/PROJECT_ROUTING.md) · [SSH / VPS](docs/VPS.md)
· [Security](SECURITY.md) · [Development](docs/DEVELOPMENT.md)

## Version metadata

当前仓库的 `RELEASE.json` 标记为 **2.0.0 / development**。此分支未发布，也未部署。

- **Version:** 2.0.0

Source version metadata is not a published release, a passing full CI run, or a
promise that an existing installation can be upgraded in place.

## Runner 设置与 VPS cutoff

完整 Runner 配置位于设备的「完整管理 → Runner 设置」。远程写入默认关闭，必须由 Runner 所有者在本机明确启用；未授权时仍可查看、预览和导出补丁。参见 [Runner 设置](docs/RUNNER_SETTINGS.md)。

VPS 使用资源级 Runner / SSH Alias，工作空间仅管理显示信息。旧 VPS 数据不保留兼容路径；已有 `workspace-2` 数据库须停机执行明确的 VPS-only cutoff，重新登记 VPS，不需要删除其他项目或存储数据。升级前阅读 [VPS 与 cutoff 操作](docs/VPS.md)。
