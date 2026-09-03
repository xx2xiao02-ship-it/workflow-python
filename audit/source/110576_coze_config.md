# “背景音乐*”节点配置登记

来源：`Workflow-Daily_Update_Te-draft-1256.zip` 内 `workflow/Daily_Update_Te-draft.yaml`。

- 中文名称：背景音乐*
- 原始节点 ID：`110576`
- 节点类型：插件
- 插件中文名称：视频合成_剪映小助手
- 接口名称：`add_audios`
- 上游输入：
  - “背景音乐”（`116592`）的 `infos` → `audio_infos`
  - “解说*”（`135313`）的 `draft_url` → `draft_url`
- 下游：AIGC动画*（`174651`）
- 超时：`180000ms`
- 重试次数：`0`

## 输入

| 字段 | 类型 | 来源 |
|---|---|---|
| `audio_infos` | `String` | 背景音乐（`116592`）的 `infos` |
| `draft_url` | `String` | 解说*（`135313`）的 `draft_url` |

`audio_infos` 是音频信息 JSON 字符串，不是嵌套数组。它继续使用 capcut-mate `add_audios` 的 HTTP URL、时间范围、音量和可选效果字段规则；本节点真实样本中为 1 项、时间范围 `0 → 111672000` 微秒、音量 `0.4`。

## 输出

原始 YAML 输出顺序：

1. `audio_ids: Array<String>`
2. `draft_url: String`
3. `track_id: String`

本地实现复用已审计的 `add_audios` 输入归一化和输出包装，但执行器通过显式注入提供，以便后续接入本地草稿运行时；默认不产生外部副作用。
