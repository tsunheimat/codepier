# 角色与明确动态同意

本功能是未发布的 Hub 源码变更，不代表线上部署或 ChatGPT 宿主已经验收。Role 只控制 CodePier 已实现的资源 / 操作组合；它不自动授予资源管理、授权管理或操作系统沙箱能力。当前产品设置流程见 [Resources / Access](RESOURCE_MODEL.md)。

## 身份、角色和连接

- **Profile** 是稳定身份，例如「开发客户端」「审阅客户端」。ID、显示名称与 Token 分开。
- **Role** 是可共享的实时政策，例如 `worker`。多个人工创建的 Profile 可以关联同一 Role。
- **Grant** 是某条已确认连接。新角色型 grant 使用 `authorization_mode=role`，固定绑定 Profile 和 Role ID，明确同意角色以后增减能力及资源。
- **Token** 只证明这条连接；每次授权读取现行角色规则。首次授权的能力/资源快照只写入审计，不是永久上限。

两个连接身份可以共用角色，但不会共用 grant 私有操作、交付物或浏览器/桌面租约。调整角色不更换 grant 或 Profile，因此日常加项目不打断身份和原操作归属。重新连接产生的新 grant 仍不自动继承旧 grant 的记录。

## 面板操作

1. 在 **Resources** 建立项目、MCP Service 或 VPS。VPS 执行另需明确的项目 / Agent 路线。
2. 在 **Access → Roles** 新建 / 编辑通用角色，为具体 Project、VPS、MCP 帐户/binding 与工具配置独立规则。项目关联不授予其他资源访问。
3. 在 **Access → Client Connections** 创建连接或复制角色 OAuth 地址：`https://YOUR-HUB/mcp?authorization=role`。OAuth 同意窗口可以自动建立稳定身份；不必先访问单独 Profile 页面。
4. Bearer 连接默认冻结当前资源 / 操作。要跟随未来角色变化，须选择动态方式并明确勾选同意；外部 MCP 委派另需明确勾选。
5. 核对 Client Connections 的实际资源权限、期限和状态。动态角色编辑对已经明确同意的连接生效；固定连接仅在首次同意上限内收缩/恢复，不因角色增加资源或能力而扩展。
6. 高级稳定身份与原固定 grant 设置在 **Access → 高级身份与兼容接入**。同一 Profile 不合并私有历史。

`authorization=role` 只选择认证提示和工具目录，不是授权秘密；在任何地址使用固定 Token 都不会变成角色 Token。正式身份只能从经过认证的 Token 解析。`profile=coding` 仍只是工具显示模式，与访问身份完全不同。

实际 ChatGPT 是否在已有连接刷新目录后正确发起新的粗粒度 scope、是否显示多个连接标签，需在目标宿主验收。不能把源码/SDK 测试当作 ChatGPT 端的验收。

## 项目规则：操作必须和资源配对

每条规则有 `actions`（read/write/execute/computer）与资源选择器。选择器可组合：

- `projects`：精确的现有 Project ID，日后可随时增减。
- `all_projects: true`：全部现有及未来项目。
- `created_projects: true`：通过该 Role 的明确创建委派建立的项目。
- `excluded_projects`：仅排除**这一条规则**的项目，不是跨规则的全局 deny。其他规则仍可能独立允许它；撤权时检查全部匹配规则。

每条项目规则包含 read，其他动作不隐含。规则按目标项目分别合并，不能先合并所有动作再套用所有资源：

```json
{
  "label": "worker",
  "project_rules": [
    {"actions": ["read"], "all_projects": true},
    {"actions": ["read", "write", "execute"], "projects": ["PROJECT_A_ID"]}
  ],
  "device_rules": [],
  "idempotency_key": "owner-create-worker-001"
}
```

这允许所有项目读取，但只有 A 可写及执行。向角色添加 B 不要求 Token 更换。Hub 的项目 mode/allow_tasks 和 Agent 的本机能力继续限制操作；角色不能越过本机否决。

`get_access_context().scopes` 是能力汇总，不代表每个项目都拥有其中全部能力。**以 `project_permissions` 的每个项目 actions 为准**。界面和 Agent 工作区元数据也按具体项目计算。

## 明确的项目创建委派

这是独立的 `projects.create` 管理能力，不是角色管理权。设备规则包含精确设备 ID，可额外限制根目录前缀、新映射只读/可写上限及是否允许启用任务：

```json
{
  "label": "worker",
  "project_rules": [{"actions": ["read", "write"], "created_projects": true}],
  "device_rules": [{
    "actions": ["devices.read", "projects.create"],
    "devices": ["DEVICE_ID"],
    "root_prefixes": ["/srv/projects"],
    "max_project_mode": "write",
    "allow_tasks": false
  }],
  "idempotency_key": "owner-create-delegation-001"
}
```

本机目录必须已存在且在 Agent `allowed_roots` 内。`root_prefixes` 留空仍要求 Agent 本机批准，不代表整个磁盘已获授权。每条设备规则的设备、路径、模式和任务条件一起匹配，不能从不同规则拼凑更宽权限。

角色连接可调用：

```text
 devices_list()
 projects_create(alias="new-work", device_id="DEVICE_ID",
                 root="/srv/projects/new-work", mode="write", allow_tasks=false,
                 idempotency_key="create-new-work-001")
```

创建复用原有 `system_validate`、持久 operation 和幂等保存回执。返回验证仍在执行时，用原请求/原幂等键继续查询，不创建新请求。派送前及真正保存前重验当前角色，且用 Agent 返回的规范化真实路径再验路径上限。重复的已完成请求返回原映射，不重复创建。

委派不允许修改既有映射、不创建磁盘目录、不改 Role/Agent 配置。不允许把现有项目、它的父目录或子目录用新别名再映射，以避免重新命名绕过受限项目。确需重叠映射由面板 owner 明确处理。

新项目是否能被此角色的连接使用，取决于 Role 是否有 `created_projects`、`all_projects` 或后来明确添加的项目规则；**创建权本身不自动赋予所有数据操作权**。

## OAuth/PAT 语义与迁移

`codepier.role_access` 是 CodePier 定义的新粗粒度 scope，不是 OAuth 标准内置 scope。此范围明确代表「按指定身份和角色的现行及未来政策工作」。Token refresh 不扩大 OAuth scope，角色规则变化也不修改 Token scope。

传统固定模式继续使用 read/write/execute/computer。绑定固定 Profile 的有效权限仍是原 grant 与 Profile 上限的交集。旧 grant **不会因为关联角色或升级服务而静默转换**；需要一次新的明确 OAuth 同意或新建角色 PAT。之后的日常加项目、调能力才不再需要重复 OAuth。

PAT 创建 `/api/grants`：

```json
{
  "label": "Worker connection",
  "authorization_mode": "role",
  "scopes": ["codepier.role_access"],
  "profile_id": "PROFILE_ID",
  "profile_version": 1,
  "role_version": 1,
  "confirm_dynamic_role": true,
  "days": 30
}
```

OAuth 确认 `/api/oauth/requests/{id}/decide` 同样提交以上身份/版本/确认字段和 `allow=true`，不提交固定的项目快照。版本检查在创建 grant 的事务内完成；过期确认页、跨 owner Role、未关联 Role 或未明确同意会被拒绝。精确政策、版本和未来变更的同意写入审计。Token 值不写入该审计。

Profile 改绑其他 Role 会使旧角色 grant 失效，不能悄悄换身份。普通 Role 的项目/能力编辑则保持绑定和身份不变。

## 撤权、等待和结果

- **Role 暂停**：身份识别、访问上下文和 Token refresh 继续可用；资源操作返回政策拒绝，不要求反复重新登录。恢复 Role 后原有效连接继续工作。
- **Profile 停用 / grant 撤销 / Token 过期**：按原认证失效机制处理。恢复 Profile 不能复活已撤销的 grant。
- **收回项目或能力**：后续呼叫、排队派送、创建保存及原操作结果读取重新验证。等待完成后也再验权，不能凭旧快照返回内容。
- **已经被接受的操作**：收权不回滚已发生的修改，也不保证终止外部命令。使用 owner 面板的原操作及停止入口核对。错误保留原 operation ID，避免不确定结果被新请求重放。

管理 API `/api/access-roles` 的读取按当前 Space / 角色分配检查；写入仍要求当前 Space 管理员会话和 CSRF/Origin。创建使用 idempotency_key，更新使用 expected_version；显示受影响 Profiles/未撤销角色 grant 数量，记录前后政策。MCP 不提供改 Role、签发凭据或修改自身授权的工具。

早期角色功能曾从 schema 5/6 增量迁移到 7。当前 IAM schema 与新增资源模型标记分别管理，见 RESOURCE_MODEL.md。旧 grants 的 authorization_mode 默认 fixed，role_id 为空；新增 access_roles、role_created_projects 和 Profile 的 role_id。保留主密钥、旧 Token、授权和历史。降级使用配套旧数据库备份，不直接让旧代码接新 schema。

## 当前范围和边界

当前 Role 涵盖项目内四类能力，及设备查看/新项目映射创建。VPS 执行、现有浏览器/桌面工具沿用其原有项目能力与本机限制。此改动没有新增任意外部 MCP/Kiln 接入、角色管理委派、角色继承、多 Role 合并、企业多用户邀请或独立租户隔离。

授权 Role 为操作资格，不规定客户端必须亲自长时间执行。角色可以用于短诊断或不同工作分工。完整 Shell 仍使用 Agent 系统账号权限；项目和角色名称不是 OS 沙箱。ChatGPT 的聊天/Project 也不是可信的角色身份输入或硬隔离边界。

## 验证

```bash
python -m pytest -q tests/test_roles.py tests/test_roles_integration.py
python -m pytest -q tests/test_roles_ui.py tests/test_access_profiles_ui.py
```

单元/HTTP 测试包括不换 Token 增加项目与能力、未来项目、规则配对、撤销与恢复、明确 OAuth/PAT 同意、迁移、跨身份隐私、派送/等待后撤权及委派创建的本机校验。真实 Hub/Agent 测试只用临时目录和测试账号。UI 矩阵分别为 Chromium/WebKit × 桌面/手机；未运行的平台不能算通过。

官方规范依据：OAuth scopes/refresh <https://www.rfc-editor.org/rfc/rfc6749.html>；OpenAI 凭据验证/工具 securitySchemes/Profile 契约 <https://developers.openai.com/plugins/build/auth>。

## VPS 与 MCP 独立规则

`vps_rules` 使用 `{ "actions": ["read", "execute"], "vps": ["明确 VPS ID"] }`；read 只查看元数据，execute 另行授权并检查明确执行路线及项目 execute。`connector_rules` 使用固定 `binding_id` 与明确批准的原始工具名，绑定获许可账号；不共享其他人的私有凭据。新 VPS / MCP 工具须明确加入政策，关联不会授予访问。
