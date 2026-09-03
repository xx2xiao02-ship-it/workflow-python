# TTS 音色治理清理执行记录（2026-09-01）

## 执行结论

- 普通 TTS 使用 TTS 主 API Key；主请求失败时才按现有策略跌落到 TTS 副 API Key。
- 新版 V3 `voice_clone`、`get_voice` 和训练结果试听固定使用 TTS 主 API Key，绝不使用副 Key。
- TikHub 继续保持独立 Token 卡片。

## 已清理

1. 删除 `src/workflow_1256/voice_clone_admin_transport.py`：该模块只实现旧 AK/SK + ProjectName 的槽位查询/下单控制面，生产入口无调用。
2. 删除 `tests/test_voice_clone_admin_transport.py`：对应旧控制面协议测试已不再适用。
3. 从 `tools/video_production_console.py` 移除旧资源设置、资源 AK/SK 保存、旧 transport 构造、槽位解析/查询/下单函数及相关配置分支。
4. `/api/voices/resource-status` 与 `/api/voices/resource-order` 保留轻量 410 封口，仅返回迁移提示，不访问供应商、不构造旧 transport。
5. 新版声音复刻配置仍拒绝 AppID、AK/SK、ProjectName、槽位控制面和 `resource_management` 字段；编导弹窗入口与 TTS 主 Key 复用保持不变。

## 可恢复归档

归档目录：`archive/tts-voice-governance-cleanup-20260901/`

- `voice_clone_admin_transport.py`
- `test_voice_clone_admin_transport.py`
- `video_production_console.py.before-cleanup`
- `test_tts_governance_console.py.before-cleanup`

## 尚未完成

供应商返回的新版 V3 `HTTP 403 resource not granted` 仍需在火山控制台开通/匹配项目权限后才能完成真实音频、STT 和时间轴闭环。本次清理未重试外部请求，也未切换副 Key。
