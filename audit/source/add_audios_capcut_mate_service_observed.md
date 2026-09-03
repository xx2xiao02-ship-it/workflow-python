# add_audios 服务源码读取登记

来源：公开仓库 `Hommy-master/capcut-mate` 的 `main` 分支：
`src/service/add_audios.py`。

已读取的确定性服务逻辑包括：

- `add_audios(draft_url, audio_infos)` 调用内部草稿写入流程；
- 每个音频项必须是对象，并要求 `audio_url`、`start`、`end`；
- 归一化字段顺序为 `audio_url`、`duration`、`start`、`end`、`volume`、`audio_effect`；
- `volume` 默认 `1.0`，越界时回退到 `1.0`；
- `start/end` 要求数值且 `end > start`，随后转为整数微秒并再次检查；
- `duration` 非空且小于等于 0 时抛出错误；
- 之后创建音频轨道、添加片段、保存草稿并返回 `(draft_url, track_id, audio_ids)`。

该服务源码还依赖剪映草稿运行时、下载器、草稿缓存和锁管理器。本项目没有把这些外部依赖伪造为已可运行，也没有调用真实服务；本地新实现只覆盖可独立验证的输入归一化和输出契约。
