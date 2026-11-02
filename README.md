# CodePier · 码头

[![Version](https://img.shields.io/badge/version-2.1.0-2563eb)](RELEASE.json)

CodePier is a self-hosted MCP bridge. ChatGPT and other clients work directly on
explicitly authorized Projects, VPS resources and external MCP services. The Hub
manages identities/resources and dispatches work; your Runner accesses files,
launches local tools and opens SSH connections.

## Workspace abstraction — 2.0 hard cutoff

**The workspace model rejects pre-workspace databases.** For that old model,
initialize a separate empty `HUB_DATA_DIR` and create new workspace bindings,
Profiles and Connections. Existing `workspace-2` installations instead receive
the automatic, VPS-only 2.1 data cutoff described below; no new data directory or
manual migration is required for that known upgrade. Unknown/mixed workspace
models are refused rather than deleting the complete Hub database.
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

Approved Runner SSH aliases automatically become VPS resources. Spaces select
them in one inline list as whole resources. Display them at the top level,
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

当前仓库的 `RELEASE.json` 标记为 **2.1.0 / development**。此分支未发布，也未部署。

- **Version:** 2.1.0

Source version metadata is not a published release, a passing full CI run, or a
promise that an existing installation can be upgraded in place.

## Runner 设置与 VPS cutoff

完整 Runner 配置位于设备的「完整管理 → Runner 设置」。远程写入默认关闭，必须由 Runner 所有者在本机明确启用；未授权时仍可查看、预览和导出补丁。参见 [Runner 设置](docs/RUNNER_SETTINGS.md)。

VPS 由 Runner 已授权的 SSH Alias 自动建立，名称默认沿用 Alias；无需在资源页重复新增。Space 的 VPS / Devices 使用同页列表勾选及别名管理。VPS 选择即代表 Shell 权限，Profile 可明确选择全部当前及未来 VPS。SSH 发现与检查只在 Devices，其他层直接继承资源与状态。

2.1 使用 `runner-inventory-2` VPS 模型：已知 `workspace-2` VPS 数据在启动时自动转换或按旧版本执行 VPS-only reset，不需要新 env 或手动迁移。旧只读 VPS 授权不会自动升级为 Shell；具体策略见 [VPS 与 cutoff](docs/VPS.md)。未知或其他旧 workspace 数据库仍拒绝使用，不会删除整个 Hub 数据。
