# workflow-python：独立选题中心迁移与版本治理

本项目是从旧工作流/控制台拆出的独立 Python 运行目录，当前已提交基线为 F0/F1 选题中心与控制面配置迁移。项目不导入旧工程、不读取旧工程内部存储，也不共享旧项目的可写运行目录。

GitHub 仓库：[xx2xiao02-ship-it/workflow-python](https://github.com/xx2xiao02-ship-it/workflow-python)

## 当前版本与交付边界

- 当前版本：`0.1.0`，版本号记录在根目录 `VERSION`。
- 当前远程升级分支先在 `e2f6772` 完成 README 与版本治理，随后在 `36daff2` 完成 API 配置传输边界升级；两次提交都只包含已登记、已验证的交付内容。
- 本地工作区中尚未提交的业务代码、页面、运行数据、测试产物和历史材料不属于本次远程分支。
- `0.x.y` 只表示迁移和治理阶段，不代表真实供应商、生产 E2E 或用户验收已经完成。

## 当前目录

| 目录 | 用途 |
| --- | --- |
| `topic_migration/` | F0/F1 生产 Python 包：选题中心、配置、供应商连接边界、契约和运行治理 |
| `static/` | 当前基线页面：选题中心、API 配置、正文治理和未迁移提示页面 |
| `governance/` | 模块登记、迁移清单、验收记录、已知问题和治理报告 |
| `tests/` | 契约、业务和治理测试 |
| `tools/` | 治理检查、Graphify 构建和版本检查工具 |
| `graphify-out/` | 生产代码关系图和报告，仅用于关系分析，不作为删除代码的依据 |

生产文件归属以 `governance/module_registry.json` 为准；Graphify 扫描范围以 `governance/graphify_scope.json` 为准。

## 快速开始

以下命令在 Windows PowerShell 中从项目根目录执行：

```powershell
Set-Location -LiteralPath 'E:\codex project'

if (-not (Test-Path -LiteralPath '.\.venv\Scripts\python.exe')) {
    py -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 启动 F0/F1 服务

当前基线入口是 `topic_migration.server`，默认端口为 `8787`。运行目录和凭据必须放在源码仓库之外：

```powershell
$runtime = Join-Path $env:TEMP 'topic-center-runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

.\.venv\Scripts\python.exe -m topic_migration.server `
    --host 127.0.0.1 `
    --port 8787 `
    --runtime-root "$runtime"
```

常用页面：

- `http://127.0.0.1:8787/topic-center`：选题中心
- `http://127.0.0.1:8787/api-management`：API 配置
- `http://127.0.0.1:8787/topic-writing-governance`：正文治理
- `http://127.0.0.1:8787/writing`：未迁移提示页

`/` 会重定向到 `/topic-center`；`/topic-center-new` 会重定向到 `/topic-center`。不传 `--trendradar-root` 时不会自动采集外部数据。

## 配置、凭据和外部请求

- 配置和运行数据通过 `--runtime-root` 或 `TOPIC_CENTER_CONFIG_ROOT` 指向仓库外目录。
- 当前已登记的配置供应商为 TikHub；其他供应商保持“尚未迁移”，不会伪造已连接状态。
- 保存配置不会发送外部请求。
- 在无法确认连接测试免费性时，连接测试保持 `deferred`，不自动启动采集或生成。
- 测试中的 TrendRadar、凭据和正文均为 `synthetic`，不代表真实供应商或生产业务验收。

## 测试和治理检查

```powershell
Set-Location -LiteralPath 'E:\codex project'

.\.venv\Scripts\python.exe -m pytest -q tests --basetemp "$env:TEMP\codex-project-pytest"
.\.venv\Scripts\python.exe -m compileall -q topic_migration tools
.\.venv\Scripts\lint-imports.exe --no-cache --no-logo
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe tools\governance_check.py
```

治理检查通过不等于真实供应商验证、生产 E2E 或用户验收通过。

## Graphify 关系分析

生产图谱只扫描 `topic_migration/**/*.py`，排除测试、治理产物、运行数据和 Graphify 输出：

```powershell
.\.venv\Scripts\python.exe tools\build_production_graph.py
.\.venv\Scripts\python.exe tools\governance_check.py
```

图谱用于调用关系和影响范围分析。图谱过期、源码 hash 不一致、没有引用或没有图谱边，都不能直接作为删除代码的理由。

## 版本治理

详细规则见 [`governance/version_governance.md`](governance/version_governance.md)。当前门禁包括：

- `VERSION`：记录 SemVer。
- `tools/version_guard.py`：检查版本格式、当前 commit、期望 SHA 和工作树状态。
- `tests/test_version_guard.py`：覆盖版本格式、当前 revision 和错误 revision 检查。
- `.github/workflows/governance.yml`：在 Pull Request、`main` 和 `codex/*` 推送时运行 Windows 治理门禁，并取消同一分支的过期执行。
- `governance/`：保存迁移登记、源码 hash、测试结果、未验证项和用户验收状态。

本地检查：

```powershell
.\.venv\Scripts\python.exe tools\version_guard.py
```

提交或 CI 门禁使用严格模式：

```powershell
.\.venv\Scripts\python.exe tools\version_guard.py `
    --require-clean `
    --expected-sha (git rev-parse HEAD)
```

GitHub 仓库还需要在 Settings 中启用 `main` 分支保护：要求 Pull Request 和治理工作流通过，禁止直接推送、force push 和删除分支。工作流不能替代仓库保护设置。

## 验收边界

- `implemented`：治理代码和登记文件已写入。
- `tested`：相关命令已实际执行并记录退出状态。
- `externally_validated`：真实浏览器、外部服务或最终产物已按需求验证。
- `user_acceptance_status`：只有用户明确验收后才可标记为 `accepted`。

旧工程和历史实现不因迁移完成而自动删除；任何清理都必须先登记影响并取得确认。
