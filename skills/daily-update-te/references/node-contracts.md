# Daily_Update_Te 节点契约与当前实现

## 纯 Python 节点

| 工作流节点 | Python 入口 | 输入 | 输出 | 当前证据 |
|---|---|---|---|---|
| `105880` TTS-STT 字幕与坑位规划 | `workflow_1256.capcut_stt_subtitle_pipeline.run_capcut_stt_subtitle_pipeline` + `plan_stt_caption_shot_slots` | `segment_text`, `group_timelines`, `all_timelines`, `audio_sources` | `captions`, `BGM_duration`, `duration`, `new_segments`, `new_timelines`, `shot_slot_plan` | STT 时间戳统一为微秒；Mini 仅校正文案；2.5 秒以下合并、5 秒以上分割；真实外部服务待验收。 |
| 内部批处理代码 `127095` 时长计算时间线规划 | `workflow_1256.timeline_planning.run_timeline_planning` | `segments`, `shots`, `duration`, `timelines` | `shots`, `clip_duration`, `int_duration`, `timelines` | synthetic 和真实 9 项 fixture 对照；时间线 `start/end` 为微秒，`duration` 为秒。 |
| `170263` 首尾帧顺延 | `workflow_1256.end_frame_extension.run_end_frame_extension` | `LLM_list`, `motion_seed`, `items`, `image_url_list`, `Code_list` | `items`, `motion_seed`, `clip_duration`, `ref_image`, `ref_image_f`, `ref_image_e`, `error` | 代码级等价和真实页面结构核对；完整 URL/长文本未全部导出。 |
| `113743` 提示词生成 | `workflow_1256.prompt_generation.run_prompt_generation` | `plan_out_list`, `ref_image`, `clip_duration` | `video_prompt`, `int_duration`, `ref_image_f`, `ref_image_e`, `camera_fixed`, `error` | synthetic 边界测试；需继续核对真实模型响应和失败行为。 |
| `156199` 画面类型识别 | `workflow_1256.scene_type_recognition.run_scene_type_recognition` | `director_plan`, `Code_list`, `new_segments`, `new_timelines`, `link_list`, `timelines` | `host_llm_input`, `host_map`, `error` | 8364 导出 Python 代码移植；已做最小结构和时间线测试，仍需真实工作流样本对照。 |
| `117861` Host 任务组装 | `workflow_1256.host_task_assembly.run_host_task_assembly` | `host_map`, `host_idxs` | `audio_url`, `audio_time`, `new_timelines`, `error` | 8364 导出 Python 代码移植；已做索引顺序和字段测试，仍需真实 Host 分支对照。 |
| `192311` BGM 任务组装 | `workflow_1256.bgm_task_assembly.run_bgm_task_assembly` | `timelines`, `music_cues` | `bgm_tasks`, `bgm_timelines`, `transition_schemes` | 8364 导出 Python 代码移植；已做任务数量和时间线结构测试，仍需真实 BGM 样本对照。 |
| `167309` 视频数据汇总 | `workflow_1256.video_data_aggregation.run_video_data_aggregation` | `url_list`, `new_timelines`, `public_video_url_list`, `timelines` | `super_timelines`, `super_url_list`, `super_segments` | 8364 导出 JavaScript 逻辑的 Python 移植；已做覆盖、排序、合并和输入校验测试，仍需真实数组对照。 |
| `151394/1904923` 关键帧信息 | `workflow_1256.keyframes_infos.run_keyframes_infos` | `ctype`, `offsets`, `values`, `segment_infos` | `keyframes_infos` | 13 项代码级等价通过；输出保持 segment 与 offset 顺序、相对微秒偏移；8364 单项真实路由与写入已验证。 |

## 需要 transport 或外部服务的节点

8364 的 52 个顶层节点还包含 9 个批处理/循环内部节点；它们同样属于执行链：

- `197742 镜头精细化导演`：内部 LLM，需保持每个镜头项的顺序。
- `127095 时长计算时间线规划`：Python 代码节点，已接入 `timeline_planning.py`，时间线使用微秒、时长使用秒。
- `142186 create_image_task`、`106538 get_task_result`：aishuch 异步生图创建/轮询，分别注入 transport，不能把创建成功当成图片成功。
- `102833 全文视觉意图导演`：`workflow_1256.visual_intent.run_visual_intent` 已实现 `segments/director_plan -> items/music_cues/reasoning_content` 契约；需要真实 LLM transport。
- `159953 speech_synthesis`：`workflow_1256.speech_synthesis.run_speech_synthesis` 已实现 8364 当前 YAML 的 `text/speed_ratio/voice_id -> data/log_id/msg` 契约；编排器固定接入 `TTS -> audio_timelines -> CapCut STT -> Mini -> 镜头坑位`，并可继续生成 `191683/135313`；需要真实 TTS/STT transport。不要混用其他版本插件中额外的 emotion/language 字段。
- `181639 图生视频提示词导演`：需要模型 transport，输出必须保持批处理项一一对应。
- `139488 host 镜头识别`：`workflow_1256.host_shot_recognition.run_host_shot_recognition` 已实现 `host_llm_input -> host_idxs/reasoning_content` 契约，并校验所选 idx 来自输入 candidates、去重且升序；需要豆包 1.8 深度思考或等价 Host LLM transport。
- `186546 gen_bgm`：BGM 生成插件，需保留任务创建和失败字段；`bgm_generation.py` 已保持 `AudioUrl_list` 与任务顺序一一对应，并接入编排器。
- `139653 video_generate`、`106157 video_query`：Seedance 创建/查询，使用 `workflow_1256.seedance_transport.SeedanceHTTPTransport` 注入请求；自动模式 1.5 Pro 优先，额度/权限/接入点级失败降级 1.0 Pro，最多轮换四套 Seedance 鉴权；仍需 `SEEDANCE_PRIMARY_*`、必要时 `SEEDANCE_TOS_*` 凭据和端到端验收。
- `191914 audio_infos`：8364 YAML 未声明输入，而当前 CapCut Mate 接口要求 `mp3_urls/timelines`；在拿到 `179757 转场音效` 真实代码和字段映射前必须阻断，不能把空请求标记为已接入。
- `118959 time_sleep`：`workflow_1256.time_sleep.run_time_sleep` 保持 `seconds -> seconds` 的整数秒契约；8364 固定值为 175。测试通过注入 sleeper 避免真实等待，生产默认调用 `time.sleep`。
- `165818 merge_bgm_timeline`：独立插件 `BGM 时间轴融合器`，输入为 `audio_urls/timelines/transition_schemes`；`merge_bgm_timeline.py` 只建立严格契约和注入点，不实现猜测的混音逻辑。它不是 CapCut Mate `audio_timelines(links)`，当前缺真实源码或接口。
- `900001 结束`：`workflow_1256.workflow_end.run_workflow_end` 只接受 `143635 save_draft` 返回的非空 `draft_url`，按 YAML 的 `{{draft_url}}` 渲染为最终 `content`；在保存草稿前不得提前执行。
- `115137/1962357 add_keyframes`：`workflow_1256.add_keyframes.run_add_keyframes` 保持 `draft_url/keyframes -> draft_url` 契约，解析器 12 个场景与固定 CapCut Mate 源码语义一致；没有 executor 时明确阻断。本轮另有 8364 单视频 2 关键帧真实写入证据。

以下实现只在注入 transport/API 客户端后才允许调用，不得把默认占位值当成真实成功：

剪映编辑类节点可以通过 [capcut-mate.md](capcut-mate.md) 中的 HTTP transport 接入；该服务必须由用户自行部署或提供地址。模型、素材生成、Seedance 视频任务和 Coze 其他插件不因接入 CapCut Mate 而自动可用。

- `129109 directors_v2`：当前用户附件版本是单一 Chat Completions 多模态插件，输入为 `system_prompt/prompt/image_urls`、输出为 `output_5_5`；8364 适配器把 `text -> prompt`，并且只有在 `output_5_5` 能解析为 `director_plan/ok/segment_beats/segments` 时才继续。旧 Ark→GPT 逻辑保留在 `directors_v2_transport.py`，不能当作当前附件版本的真实 transport。
- `103964 镜头精细化`：批处理中的模型和代码适配；保持输入项与输出项索引对应。
- `116616 Shot_Visual_Arrangement`：全局规划模型和首帧资料模型；缺 transport 时只能做契约测试。
- `184922 首帧生成`、内部 `142186 create_image_task`、内部 `106538 get_task_result`：创建任务、轮询/查询和失败包装必须分开。
- `110697 批处理`、内部 `video_generate` / `video_query`：每项都遵循 create → query，保留部分失败结果。
- `173596/137386` InfiniteTalk 分支：包含 API key、任务创建、等待、查询、错误分支；API key 只从安全配置读取。
- `182422/143635` 草稿创建和保存，以及其他剪映写入插件：需要真实服务端 transport；未注入时不得返回伪造 `draft_url`。

## 字段和时间单位规则

- `clip_duration` 通常为秒，可带 1 位小数；`int_duration` 为向上取整后的秒数。
- 时间线 `start/end` 使用微秒；不要把微秒直接当成秒参与计算。
- 任何批处理数组、镜头数组、图片数组、任务 ID 数组和 URL 数组都保持输入顺序和一一对应关系。
- 保留原始字段顺序和错误字段。尤其不要把 `msg`、`message`、`errorBody`、`isSuccess`、`status` 擅自合并或改名。
- 外部节点请求中的 `api_key` 只使用环境变量/秘密管理器，不进入 JSON fixture、日志、Skill 文件或 Git。

## 参考项目文件

- 原始节点代码快照：`audit/source/`
- Python 节点实现：`src/workflow_1256/`
- 输入输出 Schema：`schemas/`
- synthetic fixture：`samples/synthetic/`
- 真实脱敏 fixture：`samples/real/`
- 节点级审计报告：`docs/`
- 外部源码/模型/凭据分类：[external-dependencies.md](external-dependencies.md)
