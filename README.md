# 独立选题中心迁移

本项目是第一批 F0/F1 迁移的独立运行目录，包含公共基础、control-plane API 配置和选题中心。它不导入旧工程、不读取旧工程内部存储，也不共享旧项目的可写运行目录。

## 启动

在 PowerShell 中从项目根目录执行：

```powershell
Set-Location -LiteralPath 'E:\codex project'
New-Item -ItemType Directory -Force -Path "$env:TEMP\topic-center-runtime" | Out-Null
\.venv\Scripts\python.exe -m topic_migration.server --host 127.0.0.1 --port 8790 --runtime-root "$env:TEMP\topic-center-runtime"
```

打开 `http://127.0.0.1:8790/topic-center`。API 配置页面是 `http://127.0.0.1:8790/api-management`；正文治理支撑页面是 `http://127.0.0.1:8790/topic-writing-governance`。

如果需要读取本地 TrendRadar 快照，额外传入独立的 `--trendradar-root`；不传时页面会明确显示等待快照，不会自动采集。运行时配置和凭据通过 `--runtime-root` 或 `TOPIC_CENTER_CONFIG_ROOT` 放在源码仓库之外。

## 验证

```powershell
\.venv\Scripts\python.exe -m pytest -q tests --basetemp "$env:TEMP\codex-project-pytest"
\.venv\Scripts\python.exe -m compileall -q topic_migration tools
\.venv\Scripts\lint-imports.exe --no-cache --no-logo
\.venv\Scripts\ruff.exe check .
\.venv\Scripts\python.exe tools\governance_check.py
```

`governance/migration_file_register.json` 是逐文件迁移登记；`governance/migration_manifest.json` 记录迁移批次和源项目工作树 hash。`governance/acceptance_record.json` 区分已实现、离线测试、浏览器验证、真实供应商验证和用户验收，未经用户确认不会标记为生产可用。

## 范围边界

- 当前实际迁移的配置供应商只有 TikHub；其他供应商显示“尚未迁移”，不会伪造“已连接”。
- 保存配置不会发送外部请求；连接测试在无法确认免费性时保持 `deferred`。
- 只有 `CONTENT_READY` 正文可以进入文案交接，并保留 `selection_id`、`source_content_id`、正文、hash 和来源证据。
- 测试中的 TrendRadar、凭据和正文均为 `synthetic`，不代表真实供应商或生产业务已验收。
