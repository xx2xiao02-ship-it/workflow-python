# 具体画面导演（`directors_v2`）原始实现证据

- 原始压缩包：`C:\Users\Administrator\Downloads\Workflow-Daily_Update_Te-draft-1256.zip`
- 节点 ID：`129109`
- 节点中文名称：具体画面导演
- 节点接口名称：`directors_v2`
- 插件 ID：`7648963559153500203`
- API ID：`7656620535404593192`

## 只读检查结论

原始工作流压缩包本身只有 `MANIFEST.yml` 和 `workflow/Daily_Update_Te-draft.yaml`；
用户随后提供了该节点的源码附件。附件原始文件只读证据如下：

- 原始附件：`C:\Users\Administrator\.codex\attachments\7762b458-7f30-4e28-b294-c93f9571cc4b\pasted-text.txt`
- 原始文件行数：1254
- 原始文件 SHA-256：`F65F26BD1C50FEC6952CD771AECE3123011C30FDD56FE7E30EDDDC651B449662`
- 脱敏副本：[129109_coze_original_sanitized.py](129109_coze_original_sanitized.py)

脱敏副本只清空 `ARK_API_KEY` 和 `GPT_API_KEY` 的赋值，不修改原始附件；
整个审计和测试不发送网络请求、不调用外部模型，并且不保存或输出鉴权值。

源码确认的真实逻辑：先调用 Ark 文本模型做导演路由，再调用 GPT Chat
Completions 文本模型生成 `d` 导演策略和 `seg` Unit 分组，之后进行字段清洗、
Unit 补全、路由清洗、8–12 段数量检查和原文完整覆盖检查，最后返回四个业务字段。
