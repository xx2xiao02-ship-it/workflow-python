# 选题中心 → 文案创作治理方案（V2）

## 目标

将“主素材采集”和“文案创作”绑定到同一个 `source_content_id`，禁止搜索摘要、空正文或失败采集结果进入文案生成。

## 唯一业务链

```text
TrendRadar 主题
  → TikHub 相关内容筛选（按需调用）
  → 用户指定 primary_content_id / reference_content_ids
  → 采集主素材正文或字幕
  → CONTENT_READY
  → /writing-governance?source_content_id=...
  → 文案草案 REVIEW_PENDING
  → 人工确认 APPROVED
  → DIRECTOR_READY
```

## 状态门禁

| 状态 | 允许动作 | 禁止动作 |
|---|---|---|
| `ACCEPTED` | 采集、重试 | 进入文案 |
| `EXTRACTING` | 等待或查看进度 | 进入文案 |
| `CONTENT_MISSING` / `EXTRACTION_FAILED` / `EXTRACTION_BLOCKED` | 查看原因、重试 | 生成文案 |
| `CONTENT_READY` | 带入文案、生成草案 | 直接交付编导 |
| `REVIEW_PENDING` | 人工检查 | 生成 `approved_copy` |
| `APPROVED` | 交付编导 | 跳过审核 |

## 字段规则

- `selection_id` 负责串联选题及状态历史。
- `primary_content_id` 只表示用户选择的主素材。
- `reference_content_ids` 只作为参考，不得替代主素材。
- `source_content_id` 是文案消费的唯一正文输入。
- 正文必须真实采集或人工输入，长度至少 80 字符；失败结果正文为空。

## 当前实现

- 选题页仅在 `CONTENT_READY + source_content_id` 同时满足时显示“进入文案”。
- 入口携带 `source_content_id` 和 `selection_id`，文案页自动选中对应素材。
- 文案页只读取 `/api/topic-center/sources` 和 `/api/topic-center/selections`。
- 文案提交只发送 `source_content_id`，后端再次执行 `get_source_for_copy` 校验。
- TikHub 查询仍是用户点击后按需调用；页面打开和进入文案不会产生付费调用。

## 验收标准

1. 采集失败时没有“进入文案”可点击入口。
2. 采集成功后入口 URL 包含真实 `source_content_id`。
3. 文案页自动带入同一素材，不能回退到旧队列。
4. 未达到 `CONTENT_READY` 时，后端拒绝文案任务。
5. 文案草案先进入 `REVIEW_PENDING`，人工确认后才产生 `approved_copy`。
