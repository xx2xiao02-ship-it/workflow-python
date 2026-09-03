# 1256 TTS 与 BGM 代码级接入审计报告

更新时间：2026-08-01

## 结论

- TTS 代码接入：通过。
- BGM 任务组装与生成代码接入：通过。
- BGM 融合器 transport：阻断，未宣称等价。
- 本机 CapCut Mate 可达性：已由就绪检查探测到；真实音频写入仍需带真实媒体 URL 的验收。
- 火山方舟真实调用：本轮未执行，不能宣称真实音色、真实 BGM URL 或真实时长验收通过。

## 已核对的原始链路

TTS：`178142 -> 159953 -> 165901 -> 105880 -> 191683 -> 135313`，字幕支路为 `105880 -> 111882 -> 179989`。

BGM：`165901/102833 -> 192311 -> 141288/186546 -> 165818 -> 116592 -> 110576`。

原始字段和单位保持不变：音频时间线 `start/end` 为微秒；TTS `data.duration` 为秒；BGM `Duration` 为整数秒；URL 和数组按输入顺序传递。

## 本次实现

- `ark_audio_transport.py`：火山官方音频生成 HTTP transport，支持新版 `X-Api-Key` 和旧版双鉴权环境变量；不保存凭据。
- `bgm_generation.py`：逐任务调用 `gen_bgm`，校验 `data.SongDetail.AudioUrl`，保持 `AudioUrl_list` 顺序。
- `merge_bgm_timeline.py`：只实现 `165818` 输入输出契约和显式 transport 注入；没有伪造下载、裁剪、淡入淡出或混音结果。
- `DirectorContext`：增加 BGM 输入、生成结果和融合结果的受控挂载点。
- `orchestrator.py`：接入 TTS 生成、时间线、字幕/解说信息、BGM 任务/生成/融合、背景音乐信息和音轨写入；写入之间沿用最新 `draft_url`。

## 测试证据

- TTS、BGM transport、BGM 融合契约、编排器 synthetic 闭环专项测试通过。
- synthetic 闭环验证了：TTS 与 BGM 数组顺序、微秒时间线、融合器缺失时先阻断再写轨道、最新 `draft_url` 逐步传递。
- 真实 Ark 请求未执行；测试中的 URL 是 synthetic，不是生产素材。

## 尚待真实验证

1. 在本机安全配置 Ark 鉴权后，执行一次中文 TTS 和一次 BGM 生成，确认音色、真实 URL、真实 duration 和服务错误语义。
2. 提供 `165818 merge_bgm_timeline` 的源码或官方接口后，完成真实音频下载、裁剪、淡入淡出/交叉淡化验收。
3. 使用真实可访问 MP3 通过 CapCut Mate 写入同一个草稿，读取草稿确认解说轨、BGM 轨和字幕轨的数量、顺序、时间线及最新草稿地址。

在上述证据齐全前，状态是“代码接入完成，真实链路待验证”，不是全链路完成。
