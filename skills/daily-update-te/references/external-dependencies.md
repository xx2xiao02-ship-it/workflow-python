# 8364 外部依赖与源码状态

本表来自用户提供的 `Workflow-Daily_Update_Te-draft-8364.zip`。父级 batch/loop 节点不需要单独索要“源码”；它们是否可执行取决于内部节点 transport。

## 已获得实现，只缺本机配置或真实验收

| 节点 | 来源 | 当前缺口 |
|---|---|---|
| `129109 directors_v2` | 用户提供的 253 行插件源码 | `DIRECTORS_V2_API_KEY`、真实 system prompt、成功 `output_5_5` 验收 |
| `116616 Shot_Visual_Arrangement` | 用户提供并已脱敏登记的源码 | 全局规划模型和首帧资料模型 transport |
| `142186/106538` | 插件 `（Image-2）生图增加重试`，ID `7643809500675211274` | `AISHUCH_API_KEY` 与真实创建/轮询验收 |
| `139653/106157` | 插件 `Seedance 1.5 Pro`，自动降级 `Seedance 1.0 Pro` | `SEEDANCE_PRIMARY_*`、最多三套附加 Seedance 鉴权、可选 `SEEDANCE_TOS_*` 与真实创建/查询验收 |
| `174651/116930` | CapCut Mate `add_videos` | 真实 MP4 单项写入和落盘已通过；待 8364 上游同输入等价 |
| `135313/110576` | CapCut Mate `add_audios` | 真实 MP3 单项写入和落盘已通过；待 8364 上游同输入等价 |

## 有 YAML 契约、缺模型 transport

| 节点 | 8364 模型 | 依赖它的父节点 |
|---|---|---|
| `197742 镜头精细化导演` | `豆包·2.0·pro` | `103964 镜头精细化` |
| `102833 全文视觉意图导演` | `豆包·2.0·pro` | 主视觉分支 |
| `181639 图生视频提示词导演` | `豆包·2.0·lite` | `195692 生成视频提示词撰写` |
| `139488 host 镜头识别` | `豆包·1.8·深度思考` | Host 数字人分支 |
| `116616` 内部模型 | 源码中声明的全局规划/首帧资料模型 | `116616 Shot_Visual_Arrangement` |

这些节点不需要猜测插件源码；需要的是可用模型端点、鉴权与同输入真实输出。模型替换不能同时改变 prompt、schema 或节点职责。

## 缺真实插件源码或官方接口

### 本轮已获得并接入的 InfiniteTalk 源码

用户本轮提供的 RunningHub InfiniteTalk 创建/查询源码已用于实现
`src/workflow_1256/infinitetalk_transport.py`：

- `173596` 创建任务：上传 image/audio、固定 App ID 和 nodeInfoList、创建一次、健康检查。
- `137386/1178381/1693143` 查询任务：taskId 轮询、成功/失败/取消/超时和 output_url 提取。
- 密钥只从节点输入 `api_key` 或 `INFINITETALK_API_KEY` 读取；附件中的硬编码凭据未复制。
- 当前状态：离线契约已验证；真实 RunningHub 任务仍需本机 API Key 和可用媒体 URL。

| 节点 | Coze 插件/API | 插件 ID |
|---|---|---:|
| `159953` | `语音合成 / speech_synthesis` | `7426655854067351562` |
| `175652` | `Infinitetalk数字人 / host_audio_split` | `7644407435695898666` |
| `143380` | `Infinitetalk数字人 / merge_audio_urls` | `7644407435695898666` |
| `173596` | `Infinitetalk数字人 / Create_infinitetalk_Task` | `7644407435695898666` |
| `137386/1178381/1693143` | `Infinitetalk数字人 / query_infinitetalk_task` | `7644407435695898666` |
| `160040` | `Infinitetalk数字人 / video_split_by_timeline` | `7644407435695898666` |
| `165818` | `BGM 时间轴融合器 / merge_bgm_timeline` | `7658130621830692927` |
| `186546` | `Doubao-音乐生成 / gen_bgm` | `7516841765194743843` |

## 代码缺口

- `179757 转场音效`：8364 导出中的代码仍是默认示例，占位状态。需要真实代码，或用户明确确认该节点无需执行。
- `191914 audio_infos`：8364 YAML 没有 `node_inputs`，但当前 CapCut Mate `audio_infos` 强制要求 `mp3_urls` 和 `timelines`。需要随 179757 真实代码一起明确字段映射，或确认两节点均为占位。

## 本轮官方接口核对（覆盖上方旧状态）

2026-08-01 已通过火山引擎官方文档核对音频生成 HTTP 接口：

- `159953 speech_synthesis`：已实现 `src/workflow_1256/ark_audio_transport.py`。官方请求为 `POST https://openspeech.bytedance.com/api/v3/tts/create`，输出 URL、duration、code 和 `X-Tt-Logid` 已映射回 Coze 契约；真实请求仍需本机 `ARK_AUDIO_API_KEY` 或旧版双鉴权环境变量。
- `186546 gen_bgm`：已实现同一官方音频生成 transport；`Duration/Text/Genre/Instrument/Mood` 被组合为官方 `text_prompt`，生成 URL 映射到原 `data.SongDetail.AudioUrl`，批处理顺序由 `bgm_generation.py` 保持。
- `165818 merge_bgm_timeline`：已建立 `src/workflow_1256/merge_bgm_timeline.py` 和 Schema，但仍未实现 transport。它是独立的音频下载、裁剪、摆放、淡入淡出/交叉淡化融合插件，不等同于 Ark 音频生成，也不等同于 CapCut `audio_timelines`。
