# 1256“video_generate”插件代码级审计报告

## 结论

在不调用外部模型、Ark API 或 TOS 的前提下，脱敏原始插件源码与新 Python 适配实现的离线代码级对照通过。

- 节点中文职责：创建 Seedance 视频任务。
- 原始插件节点：`video_generate`（工作流内部节点 `139653`）。
- 所属批处理：中文节点“批处理”（`110697`），批量 100、并发 8。
- 目标测试：`7 passed`。
- 逐字段差异：0。
- 网络调用：0；TOS 上传：0。

## 源码与安全处理

用户附件原始 SHA-256：

`5574762e3344befb5bcba79056e7e8f027c2d0a49737ce503a21be65d54ea68c`

项目只保存脱敏副本：

- [`audit/source/video_generate_attachment_sanitized.py`](../audit/source/video_generate_attachment_sanitized.py)
- [`audit/source/video_plugins_source_metadata.json`](../audit/source/video_plugins_source_metadata.json)

脱敏副本保留代码结构、请求组装、参数清洗、账户轮换、重试和错误分支；所有 API Key、TOS AK/SK、Authorization 值均被替换为占位符。没有把原始凭据写入报告、日志或 Python 实现。

## 输入输出契约

Schema：[`schemas/video_generate.schema.json`](../schemas/video_generate.schema.json)

| 输入字段 | 类型 | 工作流来源 |
|---|---|---|
| `prompt` | string | “提示词生成”→`video_prompt` |
| `duration` | integer | “提示词生成”→`int_duration` |
| `first_frame_url` | string | “提示词生成”→`ref_image_f` |
| `generate_audio` | boolean | 固定 `false` |
| `last_frame_url` | string | “提示词生成”→`ref_image_e` |
| `ratio` | string | 固定 `9:16` |
| `resolution` | string | 固定 `480p` |
| `watermark` | boolean | 固定 `false` |

声明输出字段及顺序：`msg`、`success`、`task_id`、`tip`。插件源码还会产生扩展字段，但本地 `to_coze_node_output` 只投影 YAML 声明字段，不擅自扩大下游契约。

## 对照覆盖

- 缺失 prompt 和首帧输入：原始失败结果与新实现一致。
- 正常创建任务：核对 HTTP 方法、接口 URL、请求字段、时长、音频开关、水印、比例和分辨率。
- TOS 自有首尾帧 URL：核对 content 中首帧/尾帧 role，以及有图片时不发送 ratio/resolution。
- 400 参数错误：核对错误返回。
- 两次超时重试：核对重试后失败结果。
- JSON 字符串、空字段和默认值：核对异常输入行为。
- 未注入传输层时：新实现主动抛出 `VideoGenerateTransportRequired`，保证本地测试不会意外访问外网。

## 尚待真实验证

1. 真实 Coze 运行中每个 `task_id`、`msg`、`success`、`tip` 的完整值和平台字段包装。
2. Ark/Seedance 真实请求的鉴权可用性、模型端点、账户轮换、预查询和服务端重试行为。
3. 非 TOS 图片的下载、转存、签名 URL 有效期和 TOS 上传失败分支。
4. 下游 `video_query` 的真实轮询、视频下载、TOS 转存与 `public_video_url_list` 聚合。

因此，本报告的“通过”只覆盖脱敏源码在离线固定 HTTP 事件下的代码行为，不代表真实视频生成或全链路验收已经完成。
