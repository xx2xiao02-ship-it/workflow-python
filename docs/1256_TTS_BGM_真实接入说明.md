# 1256 TTS 与 BGM 真实接入说明

> 状态修正（2026-08-01）：本文早期内容中的 `/api/v3/tts/create`、
> `seed-audio-1.0-multilingual` 和旧版双鉴权不能作为当前官方 TTS 实装依据。
> 当前官方 TTS 以 `docs/1256_TTS_语音支持清单.md` 和
> `src/workflow_1256/ark_tts_transport.py` 为准；BGM 仍缺独立真实接口/源码。

更新时间：2026-08-01

## 当前结论

- TTS `159953 speech_synthesis`：已按火山引擎官方“音频生成 HTTP”接口实现可注入 transport。
- BGM `186546 gen_bgm`：已按同一官方音频生成接口实现逐任务 transport，保留 `Duration/Text/Genre/Instrument/Mood` 和 `AudioUrl_list` 顺序。
- BGM `165818 merge_bgm_timeline`：仍是工作流内独立融合插件，不能用 Ark 的音频生成接口或 CapCut `audio_timelines` 冒充；当前仍需该插件源码/官方接口，或明确采用本地 FFmpeg 融合方案。
- 当前没有调用真实 Ark，也没有读取或写入鉴权值；专项测试使用的是 synthetic HTTP 响应。

## 官方依据

官方文档：[音频生成 HTTP--豆包语音-火山引擎](https://docs.volcengine.com/docs/6561/2550782?lang=zh)

官方接口：`POST https://openspeech.bytedance.com/api/v3/tts/create`

官方鉴权规则：新版使用 `X-Api-Key`；旧版支持 `X-Api-App-Id + X-Api-Access-Key`。代码两种方式均支持，优先使用新版单头鉴权。

## 本地实现

- `src/workflow_1256/ark_audio_transport.py`
  - 官方 URL、请求头、`X-Api-Request-Id`、JSON 请求和响应错误处理；
  - TTS：把官方 `url/duration/code/X-Tt-Logid` 映射回 Coze `data.link/data.duration/code/log_id/msg`；
  - BGM：把官方音频 URL 映射为原 `data.SongDetail.AudioUrl`；
  - 官方只返回 Base64 `audio` 时明确阻断，不把 Base64 当成可访问素材地址。
- `src/workflow_1256/bgm_generation.py`
  - 对应原 `141288 -> 186546`；
  - 保持 BGM 任务、生成结果与 URL 数组一一对应、顺序不变。
- `src/workflow_1256/orchestrator.py`
  - 未显式注入 `159953` runner 时，若本机存在 Ark 音频鉴权，会自动使用 `ArkAudioHTTPTransport`；没有配置则保留结构化阻断。

## 本机配置（只在本机 PowerShell 输入）

新版单头鉴权：

```powershell
$env:ARK_AUDIO_API_KEY='在本机填写，不要发到聊天'
```

旧版双头鉴权：

```powershell
$env:ARK_AUDIO_APP_ID='在本机填写，不要发到聊天'
$env:ARK_AUDIO_ACCESS_KEY='在本机填写，不要发到聊天'
```

可选配置：

```powershell
$env:ARK_TTS_MODEL='seed-audio-1.0-multilingual'
$env:ARK_BGM_MODEL='seed-audio-1.0-multilingual'
$env:ARK_TTS_SPEAKER_ID='可选：火山语音官方音色ID；不填时沿用工作流 voice_id'
$env:ARK_AUDIO_SAMPLE_RATE='48000'
```

## 验收状态

专项测试覆盖：

- 官方 URL 与 `X-Api-Key` 单头；
- 旧版双头鉴权的字段映射；
- TTS `speed_ratio=1.1` 到官方 `speech_rate=10` 的适配规则；
- TTS 输出 `code/data/log_id/msg`；
- BGM 任务数量、URL 数量和数组顺序；
- 缺少 transport、缺少 URL、缺少鉴权时的阻断。

专项测试使用 synthetic HTTP 响应通过，不等于真实 Ark 计费调用成功。真实验收还必须用本机鉴权和实际中文文案运行一次，确认：音色 ID 可用、音频 URL 可访问、TTS 实际时长与时间线一致、BGM 实际时长满足任务范围，并最终通过 CapCut Mate 写入剪映草稿。
