# 版本治理方案

本方案用于防止“本地工作区、执行环境、远程仓库和交付产物”之间发生版本漂移。它约束版本如何产生、如何验证、如何进入 GitHub，以及如何回滚；不改变生产 API、字段和业务契约。

## 1. 版本定义

- 项目版本遵循 SemVer：`MAJOR.MINOR.PATCH`。
- 当前版本记录在根目录 `VERSION`，当前为 `0.1.0`。
- `0.x.y` 表示迁移和治理阶段，尚不代表生产业务已完成验收。
- `MAJOR`：不兼容的公开接口或数据契约变化。
- `MINOR`：向后兼容的功能、模块或页面能力增加。
- `PATCH`：向后兼容的修复、治理登记修正、文档和测试修正。
- 正式版本使用 Git annotated tag：`vX.Y.Z`；禁止移动已发布 tag。

## 2. 分支和远程基线

- `main`：稳定基线，只接受 Pull Request，不直接推送，不强制覆盖，不删除。
- `codex/<topic>-<date>`：开发、迁移和治理工作分支。
- `codex/local-baseline-*`：只保存明确确认过的本地提交基线，不代表已经合并到 `main`。
- 每个 Pull Request 必须说明：目标版本、源 commit、修改范围、测试结果、未验证项和用户验收状态。
- 远程 `main` 与本地历史不一致时，先报告 `diverged`，不得自动合并、强推或重写远程历史。

## 3. 提交和合并门禁

合并到 `main` 前必须满足：

1. Pull Request 指向正确的目标分支。
2. GitHub Actions 从干净 checkout 执行。
3. `tools/version_guard.py --require-clean --expected-sha $GITHUB_SHA` 通过。
4. pytest、compileall、Import Linter、Ruff 和 `tools/governance_check.py` 通过。
5. 没有凭据、运行目录、缓存、测试临时文件或未登记生产文件进入提交。
6. `user_acceptance_status` 只有在用户明确验收后才能改为 `accepted`。

GitHub 分支保护需要在仓库 Settings 中启用：要求 PR、要求上述状态检查、禁止 force push 和删除分支，并要求分支在合并前保持最新。

## 4. 执行期防漂移

每次长任务或生产验证都必须绑定一个 commit SHA：

1. 执行前确认工作树干净并记录 `git rev-parse HEAD`。
2. 运行数据、配置、凭据和缓存写入源码仓库之外的 `runtime-root`。
3. 产物记录 `version`、`commit_sha`、关键输入 hash 和生成时间。
4. 执行后再次检查 commit SHA；源码发生变化时，任务标记为 `revision_drift`，不得直接发布产物。
5. 同一任务使用唯一锁或 GitHub Actions `concurrency` 组，禁止旧任务和新任务同时写同一运行目录。

本地执行前检查：

```powershell
.\.venv\Scripts\python.exe tools\version_guard.py --require-clean
```

开发工作区允许先不加 `--require-clean` 查看当前版本和 commit；提交或 CI 门禁不得跳过清洁检查。

## 5. 发布和回滚

发布包必须同时记录：

- `VERSION` 和 Git tag；
- 发布 commit SHA；
- `governance/graphify_scope.json` 的源码 hash；
- 测试、治理检查和外部验证结果；
- 未验证项和用户验收状态。

回滚只能选择上一个已验证 tag 或 commit 重新部署，不使用 `git reset --hard`、force push 或移动远程 tag 作为回滚手段。

## 6. 责任边界

- `tools/version_guard.py`：检查版本格式、当前 commit、工作树和期望 SHA。
- `.github/workflows/governance.yml`：在 Pull Request 和受控分支上执行自动门禁，并取消同一分支的过期运行。
- `governance/`：保存规则、登记、测试证据和验收状态。
- GitHub 仓库设置：负责分支保护、必需检查和合并权限；工作流文件本身不能替代仓库保护规则。

治理门禁通过只代表版本和代码治理条件满足，不等于真实供应商、真实生产 E2E 或用户业务验收通过。
