# “数字人*”节点原始 Coze 配置登记

- 原始节点 ID：`116930`
- 中文名称：`数字人*`
- 节点类型：插件
- 插件接口：`add_videos`
- 插件中文名称：`视频合成_剪映小助手`
- 插件 ID：已从导出配置核对，本登记不重复保存平台标识值
- 插件来源：`Hommy-master/capcut-mate`
- 固定源码提交：`f2581bd989190ee61719f2bf50e118ab035dcc7f`
- 原始压缩包：`Workflow-Daily_Update_Te-draft-1256.zip`
- 压缩包 SHA-256：`117AF15096A07EAE00109B48698D806EA42FF66A927FAB8D27028A2C3B6AB087`
- YAML 内部路径：`Workflow-Daily_Update_Te-draft-1256/workflow/Daily_Update_Te-draft.yaml`

原始压缩包仅以只读方式检查，未解压覆盖、未修改。

## 上下游字段来源

| 本节点输入 | 类型 | 原始 `ref_node + path` | 中文来源 |
|---|---|---|---|
| `draft_url` | String | `182422.draft_url` | 创建草稿 |
| `video_infos` | String | `152553.infos` | HOST数字人 |

- 直接上游画布节点：`AIGC动画*`，但原始 YAML 中 `draft_url` 仍直接引用“创建草稿”的输出。
- 直接下游画布节点：`keyframes_infos`；其 `segment_infos` 来源为本节点 `116930.segment_infos`。

## Coze 节点输入配置

原始 YAML 显式配置的输入只有：

1. `draft_url`
2. `video_infos`

插件页面同时声明以下可选参数，但成功执行记录中没有实际传入：

| 字段 | 类型 | 插件默认值 |
|---|---|---:|
| `scene_timelines` | Array<Object> 或 null | null |
| `alpha` | Number | 1.0 |
| `scale_x` | Number | 1.0 |
| `scale_y` | Number | 1.0 |
| `transform_x` | Integer | 0 |
| `transform_y` | Integer | 0 |

## Coze 节点输出配置及顺序

原始 YAML 的输出字段顺序为：

1. `draft_url: String`
2. `segment_ids: Array<String>`
3. `segment_infos: Array<Object>`
4. `track_id: String`
5. `video_ids: Array<String>`

`segment_infos` 每项按 YAML 定义包含：

1. `end: Integer`
2. `id: String`
3. `start: Integer`

时间单位为微秒。

## 错误处理配置

- 整体执行超时：`180000ms`
- 重试次数：`0`
- 异常处理方式：中断流程

## 真实成功记录

- 执行记录：`7668196429877870619`
- 页面状态：运行成功
- 页面耗时：约 `0.524s`
- 真实 `video_infos`：18 项
- 输出 `segment_infos`、`segment_ids`、`video_ids`：各 18 项
- 真实 URL、草稿地址、轨道 ID、片段 ID、视频 ID：采集时主动脱敏

