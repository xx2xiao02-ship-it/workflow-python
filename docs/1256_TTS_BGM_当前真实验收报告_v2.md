# 1256 工作流 TTS 与 BGM 当前真实验收报告

更新时间：2026-08-01  
范围：仅处理 TTS、BGM、BGM 融合及其本地 CapCut Mate 写入链路；未迁移或改写其他工作流节点。

## 1. 结论

| 链路 | 代码接入 | 真实接口 | 本地 CapCut Mate 写入 | 当前结论 |
|---|---:|---:|---:|---|
| TTS 语音合成 | 通过 | 通过 | 通过 | 已完成本链路验收 |
| BGM 生成 | 通过 | 通过 | 通过 | 已完成本链路验收 |
| BGM 融合 | 通过 | 本地 FFmpeg 重写 transport 已通过 | 通过 | 已完成本地链路验收 |
| 1256 全工作流 | 部分 | 未完成 | 未完成 | 不能宣称全流程完成 |

这里的“通过”只表示本报告对应的链路取得了实际证据，不等于后续编导、素材、数字人、视频生成和完整剪辑链路已经验收。

## 2. 保留的节点契约

### TTS：`speech_synthesis`

输入字段保持：

- `text`
- `speed_ratio`
- `voice_id`

输出字段保持：

- `data.duration`
- `data.link`
- `log_id`
- `msg`

TTS 使用火山方舟官方 HTTP 音频接口适配。代码支持分片 JSON 拼接、成功码校验、大小写不敏感读取 `X-Tt-Logid`、音频时长探测和 TOS 发布。原 Coze 的数字 `voice_id` 不会直接冒充方舟 `speaker`；方舟 speaker 缺失时会明确阻断。

### BGM：`gen_bgm`

输入字段保持：

- `Duration`：整数秒
- `Text`
- `Genre`
- `Instrument`
- `Mood`

输出重点保持：

- `data.SongDetail.AudioUrl`
- `data.SongDetail.Duration`
- 原有 `AudioUrl` 数组顺序

BGM 采用整段任务生成，不再把一个 BGM 任务人为拆成多个不必要的分段生成任务；小镜头的节奏和时间线仍由上游数据负责，BGM 生成只负责按照任务描述产生音频。

### BGM 融合：`merge_bgm_timeline`

输入字段保持：

- `audio_urls`
- `timelines`
- `transition_schemes`

其中时间线继续按微秒处理，不能混用秒或毫秒。输出字段保持：

- `audio_url`
- `audio_url_list`
- `duration`

由于原融合插件源码没有在当前项目或已提供材料中取得，未宣称与该插件代码级等价。本项目新增本地 FFmpeg 融合 transport，按上述原节点输入输出契约工作，并保留融合后 URL 列表顺序。

### CapCut Mate 写入

“背景音乐”及“背景音乐*”写入适配保持 `audio_infos`、音频轨道、音频 ID 和草稿 URL 的原有数据方向，执行流程为：创建或取得草稿 → 写入音频信息 → `add_audios` → 保存草稿 → 读取草稿确认文件清单。

## 3. 真实证据

### 3.1 Coze 原工作流基线

已读取真实成功执行记录：

- 执行记录：`7668196429877870619`
- BGM 批处理数量：3
- `gen_bgm` 第一个批次：`code=0`、`isSuccess=true`、`msg=success`
- Coze 汇总中的 BGM 结果：`new_segments=61`、`new_timelines=61`、`duration=9`、`BGM_duration=112`
- 背景音乐时间线末端：`111672000` 微秒

这证明了原工作流真实 BGM 节点存在成功输出，但不直接证明本地 Python 实现与原插件逐行等价。

### 3.2 TTS 真实链路

已用真实方舟鉴权完成：

1. 调用官方 TTS 接口；
2. 取得真实音频结果和真实 `log_id`；
3. 探测音频时长为约 `3.216` 秒；
4. 发布为可访问音频 URL；
5. 通过 CapCut Mate 写入本地草稿；
6. 保存并读取草稿，确认音频、字幕和草稿文件清单可读。

本次证据中不记录密钥、签名 URL 或完整鉴权值。

### 3.3 BGM 真实链路

已用真实方舟接口完成 BGM 生成：

- 接口返回成功码 `0`；
- 真实生成音频时长分别验证过约 `30.0` 秒和 `25.97995` 秒的结果；
- 兼容接口直接返回 URL 和 Base64 音频两种结果，Base64 会先解码再发布，不能把 Base64 内容误当作 URL。

已完成真实 BGM → 本地 FFmpeg 融合 → TOS 发布 → CapCut Mate 写入：

- 生成结果码：`0`
- 融合结果时长：`30.0` 秒
- 融合 URL 协议：`https`
- 融合 URL 列表数量：1
- CapCut 写入音频 ID 数量：1
- 音频轨道存在：是
- 保存后的草稿文件清单可读：是

另一次真实 BGM 直接写入验证结果：

- BGM 时长约 `25.97995` 秒
- `audio_infos` 数量：1
- `audio_ids` 数量：1
- 音频轨道存在：是
- 保存后的草稿文件清单数量：7

## 4. 代码与测试文件

本轮关键实现：

- `src/workflow_1256/ark_audio_transport.py`
  - 方舟 BGM transport；支持双鉴权环境变量映射、成功码、URL/Base64 音频结果和发布器注入。
- `src/workflow_1256/ffmpeg_bgm_merge_transport.py`
  - 缺失融合插件的本地 FFmpeg 重写；支持时间线裁剪、交叉淡化、间隔淡入淡出、混音和 TOS 发布。
- `src/workflow_1256/orchestrator.py`
  - 自动接入 BGM transport；缺配置时明确阻断，不伪造成功结果。
- `tests/test_ark_audio_transport.py`
  - BGM URL、Base64、错误码和契约测试。
- `tests/test_ffmpeg_bgm_merge_transport.py`
  - FFmpeg 合并、缺少发布器、非法 transition 测试。

原始 Coze 压缩包未修改，原插件源码未被覆盖。

## 5. 测试结果

已执行全量回归测试：

```text
352 passed, 4 skipped, 2 warnings in 26.80s
```

警告为依赖弃用提示和 pytest 缓存目录警告，不是业务测试失败。真实接口和真实 CapCut Mate smoke 也已单独执行并取得上述结果。

## 6. 尚待验证与明确边界

1. `merge_bgm_timeline` 原始插件源码尚未取得，因此只能确认本地 FFmpeg 重写满足当前契约和真实 smoke，不能宣称原插件代码级等价。
2. 1256 工作流后续完整链路尚未验收，尤其是其他素材生产、数字人、视频生成、字幕布局和最终剪辑节点。
3. 尚未用同一份完整真实 Coze 输出逐字段对照 Python 融合器和 CapCut 最终时间轴；当前真实融合验证使用了真实远程音频 URL 和可执行时间线样本。
4. “本地草稿可被剪映客户端完整打开并播放”的最终客户端界面验收仍需在目标机器上人工确认；服务端保存和草稿文件可读不等于客户端打开验收。
5. TOS 签名 URL 会过期；保存草稿时必须保证媒体 URL 在客户端实际读取期间仍有效，或使用项目约定的持久可访问地址。

## 7. 当前状态

TTS 与 BGM 的代码接入、真实 transport、真实本地写入和专项/全量测试均已取得证据；当前报告不把这部分扩大表述为 1256 全工作流完成。下一阶段应继续按三层数据收口方案验收其他节点，而不是回头修改已验证的 TTS/BGM 契约。
