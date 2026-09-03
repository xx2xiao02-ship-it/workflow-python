# “批处理”节点及 video_generate 配置登记

来源：`Workflow-Daily_Update_Te-draft-1256.zip` 内 `workflow/Daily_Update_Te-draft.yaml`。

## 批处理容器

- 中文名称：批处理
- 节点 ID：`110697`
- 批量数量：100
- 并发数量：8
- 输入：`video_prompt`、`int_duration`、`ref_image_f`、`ref_image_e`，均来自“提示词生成”（`113743`）同名输出。
- 输出：`public_video_url_list`，来源为内部“video_query”（`106157`）的 `public_video_url`。
- 内部顺序：`video_generate`（`139653`）→ `video_query`（`106157`）。
- 外层批处理节点本身在原始 YAML 中没有单独的 `settingOnError` 配置。

## video_generate

- 插件中文职责：创建 Seedance 视频任务。
- 输入：`prompt`、`duration`、`first_frame_url`、`generate_audio`、`last_frame_url`、`ratio`、`resolution`、`watermark`。
- 输入映射：`video_prompt`→`prompt`，`int_duration`→`duration`，`ref_image_f`→`first_frame_url`，`ref_image_e`→`last_frame_url`。
- 固定值：`generate_audio=false`、`ratio=9:16`、`resolution=480p`、`watermark=false`。
- 声明输出：`msg`、`success`、`task_id`、`tip`。
- 错误设置：重试 3 次，超时 180000ms。

用户附件中的插件源码已做 SHA-256 登记并生成脱敏副本；API Key、TOS AK/SK 和 Authorization 值没有写入项目。

## video_query

- 插件中文职责：按 `task_id` 轮询 Seedance 任务，下载完成视频，并按配置返回临时地址或 TOS 地址。
- 输入：`task_id`；内部默认 `max_wait=175`、`check_interval=10`，支持 `enable_tos_upload` 和 `tos_path_prefix`。
- 输出：`errorBody`、`file_size_mb`、`isSuccess`、`msg`、`public_video_url`、`status`、`success`、`tip`。
- 错误设置：开启错误数据回退（`processType=2`、`switch=true`），重试 3 次，超时 180000ms。
- 原始回退内容：`public_video_url=""`、`status=""`、`success=false`、`tip=""`、`file_size_mb=0`、`msg=""`；`errorBody` 和 `isSuccess` 不在回退 JSON 中，由平台/插件输出契约决定。
- 直接输出 `public_video_url` 回到外层“批处理”的 `public_video_url_list` 聚合。
