# 1256“video_query”插件代码级审计报告

## 结论

在不调用外部 Ark/Seedance 服务或 TOS 的前提下，脱敏原始插件源码与新 Python 适配实现的离线代码级对照通过。

- 节点中文职责：查询视频生成任务并返回可用视频地址。
- 原始插件节点：`video_query`（工作流内部节点 `106157`）。
- 所属批处理：中文节点“批处理”（`110697`），内部顺序为 `video_generate`→`video_query`。
- 目标测试：`7 passed`。
- 逐字段差异：0；轮询 sleep 时间表差异：0。
- 网络调用：0；TOS 上传：0。

## 源码与安全处理

用户附件原始 SHA-256：

`495b84b9e875c30e5f5a24d8816790a31a6404458969d79a104e3aa114694d16`

项目只保存脱敏副本：

- [`audit/source/video_query_attachment_sanitized.py`](../audit/source/video_query_attachment_sanitized.py)
- [`audit/source/video_plugins_source_metadata.json`](../audit/source/video_plugins_source_metadata.json)

API Key、TOS AK/SK 和 Authorization 值没有写入项目。新实现默认没有传输层时抛出 `VideoQueryTransportRequired`，避免本地测试意外访问网络。

## 输入输出契约

Schema：[`schemas/video_query.schema.json`](../schemas/video_query.schema.json)

| 输入字段 | 类型 | 来源/用途 |
|---|---|---|
| `task_id` | string | `video_generate`→`task_id` |
| `max_wait` | integer | 默认 175 秒 |
| `check_interval` | integer | 默认 10 秒 |
| `enable_tos_upload` | boolean | 默认开启；synthetic 测试关闭 |
| `tos_path_prefix` | string | 默认 `seedance-videos` |

声明输出字段顺序：`errorBody`、`file_size_mb`、`isSuccess`、`msg`、`public_video_url`、`status`、`success`、`tip`。源码内部的 `temp_video_url`、`waited_seconds`、`raw_error` 不直接扩展 Coze 声明契约。

## 对照覆盖

- 缺少 `task_id`、JSON 字符串和异常顶层输入。
- 处理中→完成→下载成功，核对轮询次数、视频 URL、文件大小和 sleep 时间。
- 完成但任务 URL 位于嵌套 `data`。
- 任务失败、双账号鉴权失败。
- 下载文件过小、最大等待时间为 0 的超时分支。
- 输出投影字段名称、类型和顺序。

## 尚待真实验证

1. 真实 Coze 页面中 106157 每个任务的完整输入输出、状态变化、轮询耗时和平台错误包装。
2. Ark 真实响应中的视频 URL 层级、视频下载大小和临时 URL 有效期。
3. TOS 上传成功/失败、公开 URL 可访问性和权限配置。
4. 外层“批处理”在单项失败、超时和部分成功时是否保留原数组索引；成功执行已确认 `public_video_url_list` 为 17 项、索引 0–16 连续且元素为非空字符串。

所以，本报告只证明脱敏源码在固定离线事件下与新实现等价，不能替代真实 Coze 或真实视频结果验收。
