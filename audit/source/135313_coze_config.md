# “解说*”节点配置登记

来源：`Workflow-Daily_Update_Te-draft-1256.zip` 内 `workflow/Daily_Update_Te-draft.yaml`。

- 中文名称：解说*
- 节点 ID：`135313`
- 节点类型：插件
- 插件中文名称：视频合成_剪映小助手
- 接口名称：`add_audios`
- 下游：背景音乐*（`110576`）
- 超时：`180000ms`
- 重试次数：`0`

## 输入

| 字段 | 类型 | 来源 |
|---|---|---|
| `audio_infos` | `String` | 解说（`191683`）的 `infos` |
| `draft_url` | `String` | 创建草稿（`182422`）的 `draft_url` |

`audio_infos` 是音频信息 JSON 字符串，不是嵌套数组。插件 Schema 要求解析后为数组，并要求每一项的 `audio_url` 以 `http://` 或 `https://` 开头；服务层还校验 `audio_url`、`start`、`end`，并按源码设置 `duration`、`volume`、`audio_effect` 默认值。

## 输出

原始 YAML 的输出顺序为：

1. `audio_ids: Array<String>`
2. `draft_url: String`
3. `track_id: String`

服务接口实际会读取草稿、下载音频、创建音频轨道、写入音频片段并保存草稿；这些动作依赖外部草稿服务和音频资源，本阶段本地实现默认不调用。
