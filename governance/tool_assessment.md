# Import Linter 与 Ruff 评估

## 环境和版本

- Python：`3.14.6`
- Import Linter：`2.15`
- Ruff：`0.16.7`
- 安装位置：项目 `.venv`，不加入生产运行依赖。
- 依赖清单：`requirements-dev.txt`。当前目标目录没有现成 Python 依赖管理或锁文件，因此使用精确版本清单。

## 角色划分

Import Linter 只负责 Python 导入图的层级约束；`.importlinter` 将 HTTP 表面、业务编排、采集、Transport、存储/配置、契约和错误层按当前真实关系分层。模块精确白名单、文件归属、公开/内部符号、旧工程导入、重复路由、动态导入、存储和配置边界由 `tools/governance_check.py` 负责。

Ruff 只做问题报告和退出状态门禁，配置在 `ruff.toml`，当前没有使用 `--fix`。现有问题不通过扩大 `ignore` 隐藏；真实基线写入 `governance/known_issues.json`。

## 已执行安装验证

```powershell
.\.venv\Scripts\python.exe -m pip show import-linter ruff
.\.venv\Scripts\lint-imports.exe --help
.\.venv\Scripts\ruff.exe --version
```

结果：两个包已安装，`lint-imports` 帮助可读，Ruff 输出 `ruff 0.16.7`。当前未调用任何付费服务。

## 当前实际门禁结果

2026-09-15 实际执行：

```powershell
.\.venv\Scripts\lint-imports.exe --no-cache --no-logo
.\.venv\Scripts\ruff.exe check .
```

- Import Linter：退出码 `0`，`topic_migration dependency layers KEPT`。
- Ruff：退出码 `0`，`All checks passed!`。通过手工整理导入和移除未使用导入解决，未使用 `--fix`；历史 46 项基线仍保留在 `governance/known_issues.json`，但不作为当前阻断。
- Graphify：退出码 `0`，当前生产图谱 `242 nodes / 634 edges`，源码 hash 为 `45ef435072ca20ed645a759c428e00428218c9ed56eae76ee86de076a9ab81bd`，诊断无悬空端点、自环或端点折叠。
- 相关 pytest：`20 passed`；编译：`passed`；逐文件迁移登记边界测试：`11 passed`。

## 官方资料

- Import Linter 文档：<https://import-linter.readthedocs.io/>
- Import Linter 源码与发布：<https://github.com/seddonym/import-linter>
- Ruff 配置文档：<https://docs.astral.sh/ruff/configuration/>
- Ruff 源码与发布：<https://github.com/astral-sh/ruff>

## 兼容性结论

本次实际安装在 Python `3.14.6` 上成功，并且两个 CLI 入口实际运行。Import Linter 的架构层检查不能替代 API 路由、动态调用、存储访问和配置引用专项检查；Ruff 的通过也不能替代 pytest、浏览器、外部服务或业务验收。
