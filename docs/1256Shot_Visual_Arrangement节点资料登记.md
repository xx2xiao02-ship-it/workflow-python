# 1256“Shot_Visual_Arrangement”节点资料登记

## 当前状态

状态：已完成源码脱敏、Python 实现、离线原始代码适配和代码级审计；真实模型请求等价待验证。

本登记保存原始 Coze YAML 配置、用户补充源码、上下游基线和验证边界；不把离线固定模型响应当作真实 Coze 模型验收。

## 原始节点配置

- 中文节点名称：Shot_Visual_Arrangement
- 原始节点 ID：`116616`
- 节点类型：plugin
- API 名称：`Shot_Visual_Arrangement`
- 插件名称：全局视觉序列编排器
- 插件版本：`0`
- API ID：`7657483805024059427`
- 插件 ID：`7657483375015772212`
- 节点错误处理：不重试，超时 `180000ms`
- 原始配置来源：`Workflow-Daily_Update_Te-draft-1256/workflow/Daily_Update_Te-draft.yaml`

## 输入契约

输入字段顺序：

1. `Code_list`：数组，来自“镜头精细化”，每项包含：
   - `clip_duration`：数字数组；
   - `int_duration`：整数数组；
   - `shots`：对象数组，每项包含 `clip_duration`、`clip_role`、`source_text`、`story_beat`；
   - `timelines`：对象数组，每项包含整数 `end`、`start`，单位为微秒。
2. `ref_image`：图片数组，来源“开始”的 `ref_images`；
3. `segments`：字符串数组，来源“具体画面导演”的 `segments`；
4. `debug`：布尔值，原始配置默认值为 `false`。

## 输出契约

原始 YAML 的输出字段顺序：

1. `debug`：对象或空值，包含：
   `assemble_ms`、`build_ms`、`gpt_group_count`、`gpt_input_chars`、`gpt_ms`、
   `gpt_output_chars`、`gpt_shot_count`、`gpt_system_chars`、`mini_batches`、
   `mini_fallback_batches`、`mini_ms`、`mini_parallel_workers`、
   `mini_success_batches`、`motion_seed_count`、`plan_validate_ms`、
   `prompt_count`、`total_ms`。
2. `error`：字符串；
3. `int_duration`：整数数组；
4. `motion_seed`：字符串数组；
5. `prompt`：字符串数组；
6. `ref_image`：对象数组，每项包含字符串数组 `ref_image`；
7. `timelines`：对象数组，每项包含整数 `end`、`start`，单位为微秒。

## 源码检索结果

- 原始 `1256.zip` 中只有工作流 YAML 和清单，没有该插件的 Python 源码。
- 用户补充源码已保存为脱敏只读副本：`audit/source/116616_coze_original_sanitized.py`。
- 源码 SHA-256、行数和鉴权脱敏状态登记在：`audit/source/116616_source_metadata.json`。
- 源码包含 GPT 全局规划、mini 首帧资料补全、并发、重试、结构化 JSON 校验、失败回退和 debug 统计逻辑。
- 已在用户此前提供的 `Hommy-master/capcut-mate` 仓库中只读检索：没有找到 `Shot_Visual_Arrangement`、`motion_seed` 或 `Code_list` 对应实现。
- 仓库中命中的通用时间线/视频信息服务不具备该节点的完整输入输出契约，不能作为本节点替代实现。

## 已建立的实现与审计材料

- Python 实现：`src/workflow_1256/shot_visual_arrangement.py`。
- 原始代码离线适配器：`audit/coze_adapter_shot_visual_arrangement.py`。
- 输出逐字段对照：`audit/equivalence_shot_visual_arrangement.py`。
- Schema：`schemas/shot_visual_arrangement.schema.json`。
- synthetic 样本：`samples/synthetic/shot_visual_arrangement_*.json`。
- 真实上游输入 fixture：`samples/real/run-7668196429877870619-116616-shot-visual-arrangement-input.json`。
- 真实 Coze 页面结构观察：`samples/real/run-7668196429877870619-116616-coze-output-observation.json`。
- 审计报告：`docs/1256全局视觉序列编排器节点代码级审计报告.md`。

## 继续条件

本节点代码级审计已通过；进入真实等价验收前仍需要：

1. 同一次 Coze 成功运行的完整输入、完整输出和模型请求/响应记录；
2. 真实失败样本，覆盖超时、HTTP 错误、结构化输出失败和 mini 回退；
3. 真实 Coze 环境中确认模型版本、并发和重试行为。

鉴权值可以继续删除或打码；本项目不会读取、保存或调用鉴权信息。
