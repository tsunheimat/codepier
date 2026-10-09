---
name: review-and-verify
description: 检查 CodePier 的 Hub、Agent、面板与协议改动，记录真实执行证据并回归验证。
---

这是项目检查清单，不授予权限，也不会自动执行命令。

先解析项目并确认 execution_info。用 project_context 取得入口索引，再按需读取文件；索引不代表全仓扫描。若宿主提供对话标识，CodePier 自动关联调用；也可使用 conversations 读取获准资源与原操作关联。缺少元数据时正常调用工具，不虚构对话身份。工作流和归档功能已移除。

修改前确认现有改动。此目录可能不是 Git 仓库；没有 Git 时建立源码检查点，不声称已提交。保留并发修改，不覆盖本机配置、密钥或已有用户代码。

本项目重点验证：操作幂等与断网续跑、授权和项目映射边界、Hub/Agent 协议、SQLite 事务、文件路径与备份、真实退出码、浏览器旧响应不覆盖新会话。取消执行使用原操作回执；进程控制和恢复独立于对话关联。

执行 `.venv/bin/python -m pytest -q`。新增功能先跑对应测试，再跑全量。前端还需 `node --check web/app.js` 和 `node --check web/product.js`，以及 tests/test_identity_navigation.py、tests/test_product_browser.py 中的真实 Chromium/WebKit 场景。不要把退出码非零说成网络错误；保存原操作号并读取输出。

在当前聊天客户端报告观察结论和真实 operation_id。Conversations 仅保存资源 / 操作引用，不保存任务目标、进度或生成的摘要；不要调用已移除的 workflow 工具或归档读取。报告真实修改、执行结果和未验证范围，不用旧版本测试或部署记录代替本轮证明。
