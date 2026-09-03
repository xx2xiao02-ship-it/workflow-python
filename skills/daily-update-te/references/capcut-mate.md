# CapCut Mate 剪映插件映射

## 真实来源

- 仓库：[Hommy-master/capcut-mate](https://github.com/Hommy-master/capcut-mate)
- 本次核对分支：`main`
- 本次核对提交：`f2581bd989190ee61719f2bf50e118ab035dcc7f`
- API 前缀：`/openapi/capcut-mate/v1`
- 官方项目说明：CapCut Mate 是基于 FastAPI 的剪映草稿自动化服务，可以独立部署或接入 Coze/n8n。

## 工作流节点到接口

以下是按 8364 YAML 的节点输入/输出与 CapCut Mate 路由做出的接入表。路由和请求/响应模型来自仓库源码；“工作流级等价”仍需用同一份真实输入执行验证，不能只凭路由名称宣布通过。

| 工作流节点 | 节点 ID | CapCut Mate 接口 | 主要请求/响应 | 状态 |
|---|---:|---|---|---|
| create_draft | `182422` | `POST /create_draft` | `height,width -> draft_url,tip_url` | 路由和字段已直接核对 |
| save_draft | `143635` | `POST /save_draft` | `draft_url -> draft_url` | 路由和字段已直接核对 |
| AIGC动画* | `174651` | `POST /add_videos` | `draft_url,video_infos -> draft_url,track_id,video_ids,segment_ids,segment_infos` | 真实 MP4 单项路由已验证；8364 同输入待验收 |
| 数字人* | `116930` | `POST /add_videos` | `draft_url,video_infos -> 同上` | 共用真实路由已有证据；Coze 18 项结构已审计；Host 同输入本地写入待验收 |
| 解说* | `135313` | `POST /add_audios` | `draft_url,audio_infos -> draft_url,track_id,audio_ids` | 真实 MP3 单项路由已验证；8364 同输入待验收 |
| 背景音乐* | `110576` | `POST /add_audios` | `draft_url,audio_infos -> draft_url,track_id,audio_ids` | 共用真实路由已验证；BGM 同输入待验收 |
| 字幕* | `179989` | `POST /add_captions` | `draft_url,captions -> draft_url,track_id,text_ids,segment_ids,segment_infos` | 真实单项写入已验证 |
| 关键帧信息 | `151394`,`1904923` | `POST /keyframes_infos` | `ctype,offsets,segment_infos,values -> keyframes_infos` | 8364 常量已真实生成 2 个关键帧 |
| 添加关键帧 | `115137`,`1962357` | `POST /add_keyframes` | `draft_url,keyframes -> draft_url,...` | 真实写入并检查 segment 已通过 |
| 特效信息 | `197721`,`1765956` | `POST /effect_infos` | `effects,timelines -> infos` | 暗角/蓝色丝印各 1 项已验证 |
| 添加特效 | `187358`,`1922689` | `POST /add_effects` | `draft_url,effect_infos -> draft_url,...` | 两种特效真实写入和落盘已通过 |
| 视频信息 | `115723`,`152553` | `POST /video_infos` | `video_urls,timelines,... -> infos` | 漫画撕纸/胶片定格两套参数均已真实验证 |
| 音频信息 | `116592`,`191683` | `POST /audio_infos` | `mp3_urls,timelines,... -> infos` | BGM/解说两套参数均已真实验证 |
| 无输入音频信息 | `191914` | `POST /audio_infos` | YAML 无输入，但接口要求 `mp3_urls,timelines` | 契约不一致，等待 179757 真实代码和映射 |
| 字幕信息 | `111882` | `POST /caption_infos` | `texts,timelines,... -> infos` | 真实单项 infos 已验证 |
| 音频时间线 | `165901` | `POST /audio_timelines` | `links -> timelines,all_timelines` | 真实 MP3 单项已验证；不要误映射到 `/timelines` |

## 服务端实际行为

- `create_draft` 创建草稿并返回 `draft_url`；后续编辑接口都必须使用最新的草稿地址。
- `save_draft`、`add_videos`、`add_audios`、`add_images`、`add_keyframes`、`add_captions`、`add_effects` 等写入接口在路由层调用异步服务，并使用同一草稿的并发锁，默认锁等待时间为 30 秒。
- `video_infos`、`audio_infos`、`caption_infos`、`effect_infos`、`keyframes_infos` 等信息接口返回 `infos` JSON 字符串，不是已经解析好的数组；下游节点必须按原字段契约处理。
- 所有时间线 `start/end`、`duration`、转场时长等时间字段遵循项目接口文档中的微秒约定；不要在 transport 层转换成秒。
- `gen_video` 提交异步渲染任务，`gen_video_status` 查询任务状态；它们不能替代工作流中 Seedance `video_generate/video_query` 插件，除非完成同输入输出对照。

## 运行前提

1. 在本机或用户提供的服务器上启动 CapCut Mate。
2. 设置 `CAPCUT_MATE_BASE_URL`，例如 `http://localhost:30000`；客户端会自动补上 API 前缀。
3. 先用 `--dry-run` 检查 URL 和请求 JSON，再调用真实接口。
4. 真实调用后保存脱敏的完整请求/响应，逐字段核对 `draft_url`、ID 数组、`infos` 内容和错误响应。

## 未覆盖边界

CapCut Mate 只覆盖剪映草稿编辑/渲染服务。它不提供 `directors_v2`、`镜头精细化`、`Shot_Visual_Arrangement`、首帧图像生成、InfiniteTalk、Seedance 视频生成等工作流上游或外部模型插件。对这些节点仍必须使用对应插件源码、API 文档或真实 Coze 输入输出建立独立 transport。

## 真实最小链路证据

本项目已使用本机 `http://127.0.0.1:30000` 实际执行两类新测试草稿：

- 基础链路：`create_draft -> add_captions -> save_draft -> get_draft`，字幕响应包含 1 个 `text_id`、1 个 `segment_id` 和 1 个 `segment_info`，`get_draft` 返回 6 个文件。
- 完整编辑 smoke：`audio_timelines -> video_infos(AIGC/HOST) -> add_videos -> keyframes_infos -> add_keyframes -> effect_infos/add_effects(暗角、蓝色丝印) -> audio_infos(解说/BGM) -> add_audios -> caption_infos -> add_captions -> save_draft -> get_draft`。真实 MP4、MP3 和字幕各写入 1 项；关键帧生成 2 项；两种特效各生成并写入 1 项。落盘草稿含 video/audio/text 各 1 个 segment、effect 轨道 2 条，视频 segment 的 `common_keyframes` 含偏移 0 和 `1,000,000` 微秒、缩放 1.0→1.15；草稿目录共 8 个文件。

这些证据证明对应 CapCut Mate 路由和草稿落盘可用，不代表 8364 上游生成结果的同输入等价，也不代表整条工作流完成。
