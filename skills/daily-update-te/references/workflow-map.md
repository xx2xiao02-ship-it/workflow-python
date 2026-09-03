# Daily_Update_Te 工作流地图

## 来源

- 导出文件：`Workflow-Daily_Update_Te-draft-8364.zip`
- YAML：`Daily_Update_Te-draft.yaml`
- 工作流 ID：`7667938827822825515`
- 解析结果：52 个顶层节点、56 条顶层边；递归包含 9 个批处理/循环内部节点，共 61 个可执行节点
- 入口：`100001 开始`
- 终点：`900001 结束`，返回最新 `draft_url`

## 主线路

```text
开始
  -> create_draft
  -> directors_v2
  -> 镜头精细化(batch)
  -> Shot_Visual_Arrangement
  -> 首帧生成(batch)
  -> 首尾帧顺延
  -> 生成视频提示词撰写(batch)
  -> 提示词生成
  -> 批处理(video_generate -> video_query)
  -> 关键帧 / 特效 / 转场音效
  -> audio_infos
  -> save_draft
  -> 结束
```

## 批处理/循环内部节点

这些节点不出现在顶层画布列表中，但属于 8364 的真实执行链，状态检查和验收不能遗漏：

| 父节点 | 内部节点 ID | 节点 | 当前实现/证据 |
|---:|---:|---|---|
| 103964 | `197742` | 镜头精细化导演 | 需要 LLM transport |
| 103964 | `127095` | 时长计算时间线规划 | Python 代码级与真实 9 项 fixture 对照 |
| 184922 | `142186` | create_image_task | aishuch 创建任务 transport 已实现，待凭据和真实验收 |
| 184922 | `106538` | get_task_result | aishuch 查询任务 transport 已实现，待凭据和真实轮询验收 |
| 178142 | `159953` | speech_synthesis | 需要真实 TTS transport |
| 195692 | `181639` | 图生视频提示词导演 | 需要模型 transport |
| 141288 | `186546` | gen_bgm | 需要 BGM 生成插件 transport |
| 110697 | `139653` | video_generate | 可注入 requests transport，待 Seedance 配置和真实验收 |
| 110697 | `106157` | video_query | 可注入 requests transport，待 Seedance/TOS 配置和真实验收 |

## 并行和条件分支

| 分支 | 线路 | 关键约束 |
|---|---|---|
| 时间线与字幕 | `directors_v2 -> 循环(TTS) -> 时间线 -> CapCut STT -> Mini 校对 -> 2.5–5 秒镜头坑位 -> 解说 -> 字幕` | 新旧路径互斥；STT 产生唯一字幕时间戳，Mini 只校正文案；循环顺序和微秒时间线不可打乱。 |
| BGM | `镜头精细化 -> BGM 任务组装 -> 批处理_2 -> merge_bgm_timeline -> 背景音乐 -> 背景音乐*` | `bgm_tasks` 与 `AudioUrl_list` 逐项对应，写入草稿时沿用最新 `draft_url`。 |
| Host 数字人 | `Shot_Visual_Arrangement -> 画面类型识别 -> host 镜头识别 -> Host 任务组装 -> host_audio_split -> merge_audio_urls -> Create_infinitetalk_Task -> query_infinitetalk_task` | 创建任务、轮询/查询和异常分支保持独立；不能把待查询状态当作成功 URL。 |
| 视频批处理 | `提示词生成 -> 批处理`，内部为 `video_generate -> video_query` | 创建任务后必须查询；每个索引的成功/失败结果保留在原位置。 |
| 关键帧和特效 | `关键帧 -> add_keyframes -> 关键帧+ -> add_effects/effect_infos -> 转场音效` | 关键帧字段和时间线按原顺序传递，不能只保留最后一项。 |

## 节点清单

| ID | 类型 | 节点 | 主要输入 | 主要输出 |
|---:|---|---|---|---|
| 100001 | start | 开始 | — | `host_image`, `ref_images`, `text` |
| 182422 | plugin | create_draft | `height`, `width` | `draft_url`, `tip_url` |
| 129109 | plugin | directors_v2 | `text` | `director_plan`, `ok`, `segment_beats`, `segments` |
| 103964 | batch | 镜头精细化 | `duration`, `items`, `segments`, `timelines` | `Code_list`, `LLM_list` |
| 102833 | llm | 全文视觉意图导演 | `segments`, `director_plan` | `items`, `music_cues`, `reasoning_content` |
| 184922 | batch | 首帧生成 | `prompt`, `ref_image` | `image_url_list` |
| 178142 | loop | 循环 | `segments` | `link_list` |
| 165901 | plugin | 时间线 | `links` | `all_timelines`, `timelines` |
| 105880 | pipeline | TTS-STT 字幕与坑位规划 | `segment_text`, `group_timelines`, `all_timelines`, `audio_sources` | `captions`, `BGM_duration`, `duration`, `new_segments`, `new_timelines`, `shot_slot_plan` |
| 111882 | plugin | 字幕 | `texts`, `timelines` | `infos` |
| 116616 | plugin | Shot_Visual_Arrangement | `Code_list`, `ref_image`, `segments`, `debug` | `debug`, `error`, `int_duration`, `motion_seed`, `prompt`, `ref_image`, `timelines` |
| 195692 | batch | 生成视频提示词撰写 | `items`, `motion_seed`, `ref_image`, `clip_duration` | `plan_out_list` |
| 170263 | code | 首尾帧顺延 | `LLM_list`, `motion_seed`, `items`, `image_url_list`, `Code_list` | `clip_duration`, `error`, `items`, `motion_seed`, `ref_image`, `ref_image_e`, `ref_image_f` |
| 115723 | plugin | AIGC动画 | `timelines`, `video_urls`, `transition`, `transition_duration`, `volume` | `infos` |
| 156199 | code | 画面类型识别 | `director_plan`, `Code_list`, `new_segments`, `new_timelines`, `link_list`, `timelines` | `error`, `host_llm_input`, `host_map` |
| 139488 | llm | host 镜头识别 | `host_llm_input` | `host_idxs`, `reasoning_content` |
| 117861 | code | Host 任务组装 | `host_map`, `host_idxs` | `audio_time`, `audio_url`, `error`, `new_timelines` |
| 175652 | plugin | host_audio_split | `host_audio_timeline`, `links` | `failed_segments`, `msg`, `segments`, `status`, `url_list` |
| 143380 | plugin | merge_audio_urls | `audio_urls` | `duration`, `failed_count`, `format`, `msg`, `status`, `success`, `timeline`, `total`, `url` |
| 173596 | plugin | Create_infinitetalk_Task | `api_key`, `audio`, `image`, `prompt`, `Max_side_length`, `Scale`, `intensity` | `msg`, `status`, `task_ID` |
| 118959 | plugin | time_sleep | `seconds` | `seconds` |
| 121815 | variable_merge | 变量聚合 | — | `output_url` |
| 137386 | plugin | query_infinitetalk_task | `api_key`, `taskId` | `cost_coins`, `cost_time`, `errorBody`, `isSuccess`, `msg`, `output_url`, `status`, `task_ID` |
| 1178381 | plugin | query_infinitetalk_task_1 | `api_key`, `taskId` | `cost_coins`, `cost_time`, `errorBody`, `isSuccess`, `msg`, `output_url`, `status`, `task_ID` |
| 160040 | plugin | video_split_by_timeline_2 | `timeline`, `video_url` | `failed_count`, `failed_segments`, `ffmpeg_path`, `msg`, `segments`, `status`, `success_count`, `total`, `url_list` |
| 152553 | plugin | HOST数字人 | `timelines`, `video_urls`, `transition`, `transition_duration`, `volume` | `infos` |
| 192311 | code | BGM 任务组装 | `timelines`, `music_cues` | `bgm_tasks`, `bgm_timelines`, `transition_schemes` |
| 141288 | batch | 批处理_2 | `bgm_tasks` | `AudioUrl_list` |
| 165818 | plugin | merge_bgm_timeline | `audio_urls`, `timelines`, `transition_schemes` | `audio_url`, `audio_url_list`, `duration` |
| 116592 | plugin | 背景音乐 | `mp3_urls`, `timelines`, `volume` | `infos` |
| 191683 | plugin | 解说 | `mp3_urls`, `timelines`, `audio_effect`, `volume` | `infos` |
| 135313 | plugin | 解说* | `audio_infos`, `draft_url` | `audio_ids`, `draft_url`, `track_id` |
| 110576 | plugin | 背景音乐* | `audio_infos`, `draft_url` | `audio_ids`, `draft_url`, `track_id` |
| 174651 | plugin | AIGC动画* | `draft_url`, `video_infos` | `draft_url`, `segment_ids`, `segment_infos`, `track_id`, `video_ids` |
| 116930 | plugin | 数字人* | `draft_url`, `video_infos` | `draft_url`, `segment_ids`, `segment_infos`, `track_id`, `video_ids` |
| 179989 | plugin | 字幕* | `captions`, `draft_url`, `font`, `font_size`, `has_shadow`, `transform_y` | `draft_url`, `segment_ids`, `segment_infos`, `text_ids`, `track_id` |
| 143635 | plugin | save_draft | `draft_url` | `draft_url`, `message` |
| 113743 | code | 提示词生成 | `plan_out_list`, `ref_image`, `clip_duration` | `camera_fixed`, `error`, `int_duration`, `ref_image_e`, `ref_image_f`, `video_prompt` |
| 110697 | batch | 批处理 | `video_prompt`, `int_duration`, `ref_image_f`, `ref_image_e` | `public_video_url_list` |
| 151394 | plugin | keyframes_infos | `ctype`, `offsets`, `segment_infos`, `values` | `keyframes_infos` |
| 1693143 | plugin | query_infinitetalk_task_2 | `api_key`, `taskId` | `cost_coins`, `cost_time`, `errorBody`, `isSuccess`, `msg`, `output_url`, `status`, `task_ID` |
| 115137 | plugin | add_keyframes | `draft_url`, `keyframes` | `draft_url` |
| 1904923 | plugin | 关键帧 | `ctype`, `offsets`, `segment_infos`, `values` | `keyframes_infos` |
| 1962357 | plugin | 关键帧+ | `draft_url`, `keyframes` | `draft_url` |
| 187358 | plugin | add_effects | `draft_url`, `effect_infos` | `draft_url`, `effect_ids`, `segment_ids`, `track_id` |
| 197721 | plugin | effect_infos | `effects`, `timelines` | `infos` |
| 1922689 | plugin | add_effects_1 | `draft_url`, `effect_infos` | `draft_url`, `effect_ids`, `segment_ids`, `track_id` |
| 1765956 | plugin | effect_infos_2 | `effects`, `timelines` | `infos` |
| 167309 | code | 视频数据汇总 | `url_list`, `new_timelines`, `public_video_url_list`, `timelines` | `super_segments`, `super_timelines`, `super_url_list` |
| 179757 | code | 转场音效 | `super_timelines` | `key0`, `key1`, `key2` |
| 191914 | plugin | audio_infos | — | `infos` |
| 900001 | end | 结束 | `draft_url` | 工作流答案 |

## 关键字段来源

- `start.text -> directors_v2.text`；`directors_v2.director_plan -> 全文视觉意图导演.director_plan`。
- `时间线.timelines` 与循环 TTS 音频共同进入 `CapCut STT -> Mini -> 镜头坑位规划`；`循环.link_list` 同时参与时间线和画面类型识别。
- `镜头精细化.Code_list/LLM_list`、`Shot_Visual_Arrangement.ref_image/motion_seed` 和 `首帧生成.image_url_list` 共同进入 `首尾帧顺延`。
- `首尾帧顺延.clip_duration/ref_image/ref_image_f/ref_image_e -> 生成视频提示词撰写 -> 提示词生成 -> 批处理`。
- 草稿写入链必须持续传递最新 `draft_url`，最终只从 `save_draft.draft_url` 返回。
