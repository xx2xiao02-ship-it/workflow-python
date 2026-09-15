# E:\\codex project 治理规则

本文件是目标项目的根治理入口，适用于当前目录及其子目录。最终功能和版本验收由用户负责；代码治理检查通过不等于业务验收通过。

## 固定工作流

所有模块化迁移、开发、测试和交付任务必须按以下顺序执行：

1. **确认模块范围**：先查 `governance/module_registry.json`，确认生产文件归属、公开接口、API、契约、允许依赖、Transport 和测试命令。
2. **查询关系**：先读当前源码、调用关系、测试配置和 `governance/graphify_scope.json`。图谱缺失、源码 hash 不一致或超过新鲜度期限时，必须标记图谱过期，以当前源码为准。
3. **修改**：只改已登记范围；新增生产文件、路由、公开接口、契约、依赖或 Transport 前，先更新登记设计。禁止导入旧工程、读取其他模块内部存储或创建隐藏兼容入口。
4. **测试**：先跑相关契约/业务 pytest，再跑统一治理入口；动态导入、存储访问、配置引用、路由和文件归属按适用专项测试验证。禁止使用 Ruff `--fix` 批量改写。
5. **更新登记**：写入 `governance/migration_manifest.json`、`governance/migration_file_register.json`、`governance/change_log.jsonl` 和必要的已知问题/图谱/报告记录，记录实际修改、关系变化、测试结果、未验证项和源码版本。所有生产文件必须在逐文件登记中有唯一目标路径、源路径、源 hash、目标 hash、模块和迁移理由。
6. **交付用户验收**：交付文件清单、可运行命令、测试结果、未覆盖风险和用户验收状态。未经用户明确确认，不得把 `user_acceptance_status` 改为 `accepted`。

## 不可破坏边界

- 不改变现有生产业务、API 地址、字段名、契约语义、数组顺序、ID 对应关系或时间单位。
- 不删除历史代码、页面、运行数据或资产。确认废弃的内容只登记为“历史实现”“废弃链路”“仅供参考”，删除前必须取得用户确认。
- 不调用付费服务，不自动提交、推送、合并或切换 Git 分支。
- `.venv` 只用于开发检查，不属于生产运行依赖；凭据、缓存、运行目录和测试产物不得进入源码登记。
- 生产代码修改前必须确认当前迁移批次已结束、服务维护锁已释放；不得与其他任务同时修改公共文件。
- Graphify 仅用于关系和影响分析，不得依据无边、无引用或旧图谱自动删除代码。

## 当前项目范围

- 生产 Python 包：`topic_migration/`
- 生产静态页面：`static/*.html`
- 治理元数据：`governance/`
- 治理脚本：`tools/`
- 测试：`tests/`
- 当前生产图谱只扫描 `topic_migration/**/*.py`，排除测试、缓存、运行数据、历史实现和治理产物。

## 统一命令

在 PowerShell 中从项目根目录执行：

```powershell
Set-Location -LiteralPath 'E:\\codex project'
.\.venv\Scripts\python.exe tools\governance_check.py
.\.venv\Scripts\python.exe tools\governance_check.py --module topic_migration.topic_service
.\.venv\Scripts\python.exe -m pytest -q tests --basetemp "$env:TEMP\\codex-project-pytest"
.\.venv\Scripts\python.exe -m compileall -q topic_migration tools
```

刷新独立生产图谱时执行：

```powershell
.\.venv\Scripts\python.exe tools\build_production_graph.py
.\.venv\Scripts\python.exe tools\governance_check.py
```

治理入口固定调用：

```text
.\.venv\Scripts\lint-imports.exe
.\.venv\Scripts\ruff.exe check .
```

治理入口失败时，先处理报告中的真实文件、关系或工具错误；不得扩大豁免、修改测试断言来掩盖已有违规。已有问题必须登记在 `governance/known_issues.json`，并与当前源码版本绑定。

## 状态定义

- `implemented`：治理代码和登记文件已写入。
- `tested`：相关命令已实际执行并记录退出状态。
- `externally_validated`：真实浏览器、外部服务或最终产物已按需求验证；本项目治理门禁不自动产生该状态。
- `user_acceptance_status`：只有用户明确验收后才可为 `accepted`；必须绑定具体源码版本 hash。
