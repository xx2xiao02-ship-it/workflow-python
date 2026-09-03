# 节点 1904923「关键帧信息」原始配置登记

- 节点名称：关键帧信息
- API：`keyframes_infos`
- 输入：`ctype`、`offsets`、`segment_infos`、`values`
- 固定值：`ctype=UNIFORM_SCALE`、`offsets=0|100`、`values=1|1.15`
- `segment_infos` 来源：节点 `174651` 的 `segment_infos`
- 输出：`keyframes_infos: String`
- 下游：节点 `1962357`「添加关键帧」的 `keyframes`
- 超时：`180000ms`
- 重试：`0`
- 异常：中断流程
- 原始 CapCut Mate 源码快照：`audit/source/keyframes_infos_capcut_mate_original.py`

本节点与 `151394` 使用同一 API 和原始实现，但不能共用节点级验收结论；本节点上游为 `174651`，真实结构样本为 17 段。
