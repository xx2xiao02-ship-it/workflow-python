---
name: copywriting-orchestrator
description: 以项目固定顺序编排知识源、Obsidian 资产、文案初稿、human-writing 收口和人工审核；用于本项目文案生产，不允许模型自行猜测调用顺序。
---

# 项目文案编排 Skill

这是本项目唯一的文案执行顺序事实源。生产入口是 `/writing`，资产治理和人工审核入口是 `/obsidian`。`/writing-mock` 和 `/obsidian-mock` 只作为界面参考，不能作为生产状态来源。

## 固定执行链

```text
知识源自动识别
→ 写入 Obsidian 原始资料区
→ 人工确认（无法识别时必须停在待人工确认）
→ 仓颉适配器生成知识方法包
→ 女娲适配器从 Obsidian 作品卡生成思维包
→ Writing-DNA 生成语言风格包
→ 人工审核并锁定版本
→ 文案编排 Skill 生成初稿
→ human-writing 收口
→ 人工终审
```

## 生产约束

1. 每个输入必须产生稳定 `source_id`，并保存原始 URL/文件、平台、资料类型、作者、用途、状态、Vault 路径、创建时间和来源版本。
2. 只有已写入 Obsidian、状态可追踪的作品卡才能交给女娲适配器；不能把未登记链接或模型记忆当作账号样本。
3. Writing-DNA 少于 30 篇完整作品只能标记“实验版”；80—100 篇是推荐量。实验版不得出现在“正式可用”资产列表。
4. 思维包、风格包必须使用已审核版本。`REVIEW_PENDING`、`BLOCKED`、`FAILED` 资产不能进入生成任务。
5. human-writing 只在初稿之后运行。它可以做事实核验提示、重复解释清理、中文节奏和模型腔检查，但不得改变事实、数字、引语、核心观点和已审核账号规则。
6. 第三方仓库源码保持只读。项目改造只能放在 `src/workflow_1256/copywriting_adapters/`，并在 `docs/skill-governance.json` 记录上游地址、版本、许可证和适配状态。上游未读取时必须标记 `not_verified`。
7. 任何阶段失败都要写入任务快照和来源状态，不能用前端提示冒充成功；未完成真实模型、登录或外部平台验证时，报告必须明确“代码修改已完成，但尚未完成实际验证，因此当前状态不是最终完成。”

## 输入与输出

- 输入：主题/热点、目标平台、受众、已审核思维包、已审核语言风格包、一个或多个已登记知识源。
- 初稿输出：`review_status=REVIEW_PENDING`，同时包含 `source_id`、`source_version`、`style_profile_id`、`style_profile_version` 和编排链状态。
- 收口输出：保留初稿和收口报告；不存在事实可追踪或安全校验失败时，状态为 `BLOCKED`。
- 终审输出：只有人工确认后才生成 `approved_copy`，并允许进入 `/director`。

## 当前项目映射

- 知识源登记与状态：`src/workflow_1256/knowledge_source_registry.py`
- Obsidian 文件边界：`src/workflow_1256/account_knowledge/obsidian_repository.py`
- 账号作品卡与 Writing-DNA：`src/workflow_1256/account_knowledge/production_bridge.py`
- 文案初稿/审核：`src/workflow_1256/style_package.py`
- 生产路由与任务快照：`tools/video_production_console.py`
- 第三方适配边界：`src/workflow_1256/copywriting_adapters/`
