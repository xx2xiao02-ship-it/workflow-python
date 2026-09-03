# “AIGC动画”节点配置登记

来源：`Workflow-Daily_Update_Te-draft-1256.zip` 内 `workflow/Daily_Update_Te-draft.yaml`。

## 节点身份

- 中文名称：AIGC动画
- 节点 ID：`115723`
- 节点类型：插件
- 插件中文名称：剪映小助手数据生成器
- 接口名称：`video_infos`
- 下游节点：解说*（`135313`）

## 工作流实际传入

| 字段 | 类型 | 来源 | 工作流值 |
|---|---|---|---|
| `timelines` | `Array<Object>` | 时间线（`116616`）的 `timelines` | 动态传入 |
| `video_urls` | `Array<String>` | 批处理（`110697`）的 `public_video_url_list` | 动态传入 |
| `transition` | `String` | 本节点固定值 | `漫画撕纸` |
| `transition_duration` | `Integer` | 本节点固定值 | `1000000` 微秒 |
| `volume` | `Float` | 本节点固定值 | `0` |

原始 YAML 没有给该节点显式传入 `height`、`width`、`mask`；插件自身 Schema 将它们定义为可选字段。执行详情页可见这些 Schema 字段，但不能把未展开的页面默认值误写成工作流固定值。

## 输出

- `infos: String`
- 字符串内容是按数组索引生成的视频信息 JSON 数组。
- 每一项固定先写 `video_url`、`start`、`end`、`duration`，随后按 `width`、`height`、`mask`、`transition`、`transition_duration`、`volume` 的顺序追加非空可选字段。

## 原始行为要点

1. `video_urls` 与 `timelines` 数量不一致时，两个数组都截断到较短长度，不抛出长度错误。
2. `duration = end - start`，时间单位沿用输入的微秒，不做换算或舍入。
3. 时间线缺少 `start` 或 `end` 时，原函数直接抛出 KeyError；不自行修复。
4. 输出使用 `json.dumps(..., ensure_ascii=False)`，保留对象字段插入顺序。

## 真实 Coze 页面证据

执行记录 `7668196429877870619` 的页面显示该节点运行成功，耗时约 `0.071s`，输入字段包含 `timelines`、`video_urls`、`height`、`mask`、`transition`、`transition_duration`、`volume`、`width`，输出字段为 `infos`。本项目只保存字段结构和状态，不保存真实视频 URL 或完整输出字符串。
