# 关键帧信息（节点 151394）原始配置登记

来源：`C:\Users\Administrator\Downloads\Workflow-Daily_Update_Te-draft-1256.zip`

原始 YAML：`Workflow-Daily_Update_Te-draft-1256/workflow/Daily_Update_Te-draft.yaml`

- 中文节点名：关键帧信息
- Coze 标题：`keyframes_infos`
- 节点类型：plugin
- 插件名：剪映小助手数据生成器
- API：`keyframes_infos`
- 输入：
  - `ctype: String`，固定值 `UNIFORM_SCALE`
  - `offsets: String`，固定值 `0|100`
  - `segment_infos: Array<Object>`，来源 `116930.segment_infos`
  - `values: String`，固定值 `1|1.15`
- 页面声明但本次未传入：`height`、`width`
- 输出：`keyframes_infos: String`
- 下游：节点 115137“添加关键帧”的 `keyframes`
- 错误配置：超时 `180000ms`，重试 `0`，异常时中断流程

注意：节点 1904923“关键帧信息（第二处）”使用同一 API，但不在本阶段处理。
