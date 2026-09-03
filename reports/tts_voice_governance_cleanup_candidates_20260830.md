# TTS 音色治理：建议清理项（2026-08-30）

## 当前结论

新版生产链路已经切换为 `voice_clone_transport.py` + TTS 主 API Key。以下内容仍在源码中，但当前页面不展示、运行入口不调用；现阶段只登记为候选项，不执行删除。真实 TTS→STT→时间轴→数字人→剪映验收通过，并确认具体清单后再处理。

本轮方向修正：新版 `voice_clone` / `get_voice` 的 endpoint 与 `seed-icl-2.0` 已固定，旧 `ARK_TTS_VOICE_CLONE_API_URL`、旧资源控制面和 ProjectName/AK/SK 不再能够覆盖运行时；8768 页面只保留 TTS 主 API Key 复用和编导音色管理。

## 2026-08-31 实际接口探测

使用 1 字节合成测试数据（未使用用户声音文件）调用新版 `voice_clone`，供应商返回 `HTTP 403 resource not granted`；本地目录仍为 0 个定制音色，未写入半成品记录。该结果表明当前火山项目/ API Key 尚未获得声音复刻资源，或 Key 与开通项目不匹配，暂不能进行真实音频、时间轴和剪映草稿验收。代码已将该错误转换为“新版控制台开通豆包声音复刻模型 2.0 + 后付费音色服务并确认项目一致”的可执行提示。

## 建议清理项

| 文件/模块 | 原用途 | 当前是否调用 | 替代实现 | 删除影响 | 回滚方式 | 建议 |
|---|---|---|---|---|---|---|
| `src/workflow_1256/voice_clone_admin_transport.py` | 旧 `open.volcengineapi.com` 资源查询/下单，AK/SK + `ProjectName` | 仅被 `video_production_console.py` 的旧资源函数引用；当前开关为 `False`，路由返回 410 | `voice_clone_transport.py` 的新版 v3 数据面；新版控制台负责创建音色 | 旧资源查询/下单测试和兼容调用失效 | 从归档恢复文件及测试 | 真实验收后归档并删除 |
| `tools/video_production_console.py` 中 `_voice_clone_resource_*`、资源密钥保存和 `VOICE_CLONE_RESOURCE_*` | 旧槽位管理配置、查询、下单及页面注入 | 当前页面不注入；仅 410 封口和旧单测保留 | 编导弹窗登记真实 `speaker_id`；TTS 主 Key 训练/查询 | 删除旧资源路由和旧配置迁移逻辑 | 从当前版本或归档恢复 | 先替换/归档单测，再删除死代码 |
| `tools/video_production_console.py` 中 `voice_clone_resource_script` 与 `.voice-clone-slot-lookup` 样式 | 旧页面槽位查询下拉框 | 当前未挂载到页面 | 新版控制台链接 + `speaker_id` 输入 | 不影响新版页面 | 恢复该段模板 | 与上一项一起删除 |
| `tests/test_voice_clone_admin_transport.py`、资源控制面单测 | 固化旧 AK/SK/OpenAPI 合约 | 仍执行，但只覆盖已废弃模块 | 新版 v3 transport 与 410 路由测试 | 删除后不再验证旧协议 | 从归档恢复 | 新版真实验收后归档/删除 |
| `/api/voices/resource-status`、`/api/voices/resource-order` 410 封口 | 防止旧客户端继续调用旧资源控制面 | 当前只返回 410，不访问供应商 | 新版控制台创建后登记 `speaker_id` | 旧客户端会得到 404/410 | 保留路由封口或恢复归档版本 | 建议至少保留一个迁移周期，再删除 |

## 明确暂不清理

- `src/workflow_1256/ark_audio_transport.py`：仍包含 BGM 兼容链路，不能按名称删除。
- `src/workflow_1256/ark_tts_transport.py` 中迁移期字段：需先证明旧任务读取链路已退出，再清理。
- 运行时目录中的加密凭据和历史任务快照：不做批量删除，避免影响旧任务预览和回滚。

## 删除前置条件

1. 一个官方音色和一个定制音色完成真实 TTS。
2. 新音频实际完成 STT、字幕/镜头微秒时间轴、数字人和剪映草稿验收。
3. 旧任务读取与新版回归测试通过。
4. 用户确认上述具体清理项后，先归档再删除，并重新执行全量回归。
