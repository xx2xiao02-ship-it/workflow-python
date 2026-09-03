# “AIGC动画*”节点原始 Coze 配置登记

- 原始节点 ID：`174651`
- 中文名称：`AIGC动画*`
- 节点类型：插件
- 插件接口：`add_videos`
- 插件来源：`Hommy-master/capcut-mate`
- 固定源码提交：`f2581bd989190ee61719f2bf50e118ab035dcc7f`
- YAML 来源：`Workflow-Daily_Update_Te-draft-8364/workflow/Daily_Update_Te-draft.yaml`

## 上下游字段

- `draft_url`：原始 YAML 来源为创建草稿（节点 `182422`）的 `draft_url`。
- `video_infos`：原始 YAML 来源为“AIGC动画”（节点 `115723`）的 `infos`。

## Coze 节点输入配置

原始 YAML 直接配置的输入只有：

| 字段 | 类型 | 来源 |
|---|---|---|
| `draft_url` | String | `182422.draft_url` |
| `video_infos` | String | `115723.infos` |

通过插件页面读取到的完整接口 Schema 还声明了以下可选参数：

| 字段 | 类型 | 默认值 |
|---|---|---:|
| `scene_timelines` | Array<Object> 或 null | null |
| `alpha` | Number | 1.0 |
| `scale_x` | Number | 1.0 |
| `scale_y` | Number | 1.0 |
| `transform_x` | Integer | 0 |
| `transform_y` | Integer | 0 |

成功执行记录中实际展开的运行入参只有 `draft_url`、`video_infos`；上述可选字段未出现在本次运行入参中。

## Coze 节点输出配置及顺序

原始 YAML 的输出字段顺序为：

1. `draft_url: String`
2. `segment_ids: Array<String>`
3. `segment_infos: Array<Object>`
4. `track_id: String`
5. `video_ids: Array<String>`

`segment_infos` 对象包含 `end: Integer`、`id: String`、`start: Integer`；插件 Schema 文档将时间单位明确为微秒。

## 错误处理配置

- 超时时间：`180000ms`
- 重试次数：`0`
- 异常处理：中断流程
