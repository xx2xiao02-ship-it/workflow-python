---
name: daily-update-te
description: 执行和审计 Daily_Update_Te 视频内容规划工作流，包含纯 Python 节点、剪映 CapCut Mate HTTP transport、节点映射、字段契约和分支处理。Use when the user asks to run, migrate, validate, explain, or troubleshoot the current Daily_Update_Te 8364 workflow, its legacy 1256 baseline, Coze export, CapCut Mate integration, Python node implementations, or related JSON fixtures.
---

# Daily Update Te

## 目标

按导出的 Coze 工作流执行逻辑处理 `Daily_Update_Te`，保持节点职责、字段来源、数组一一对应关系、输出顺序、时间单位、批处理/循环语义和错误分支。先读取 [workflow-map.md](references/workflow-map.md)；需要字段级细节时再读取 [node-contracts.md](references/node-contracts.md)；需要判断该向用户索要源码、模型配置还是凭据时读取 [external-dependencies.md](references/external-dependencies.md)。

## 使用边界

- 将该 Skill 视为工作流编排和验证入口，不把它描述成已经连通 Coze、模型或外部插件的生产执行器。
- 仅当调用方提供相应 transport/API 适配层和运行凭据时，才执行模型、插件、视频生成、任务查询或草稿写入；凭据必须来自环境变量或安全配置，禁止写入代码、日志、样例、Skill 或报告。
- 离线契约测试、synthetic fixture、真实 Coze 页面证据和 Python 生产端到端运行是三类不同证据，不得混称。
- 不根据节点标题猜测字段含义。字段来源以工作流输入配置中的 `ref_node + path` 为准；修改前先核对 YAML、Schema 和现有测试。

## 执行流程

### 1. 确认输入和模式

- 接收工作流起始输入时，核对 `text`、`host_image`、`ref_images` 的类型和是否为空；最终草稿结果来自 `draft_url`。
- 用户若只要求迁移/审计，保持“读取原始 YAML → 建立执行基线 → 单节点实现 → 单节点验证”的顺序，不直接重构整条链。
- 用户若要求运行，先判断是否属于纯 Python 节点；外部节点没有 transport 时，明确返回“需要 transport”，不要伪造成功结果。

### 2. 按主线路和分支处理

- 主线从 `create_draft`、`directors_v2` 开始；随后分为镜头细化、时间线/字幕、Host 数字人、BGM、视频生成、关键帧/特效和草稿写入分支。完整节点和边见 `references/workflow-map.md`。
- 批处理必须保持输入项与输出项的索引对应；循环节点必须保持迭代顺序；不要把批处理结果压平成无序字典。
- 异步任务必须遵循“创建 → 等待/轮询 → 查询 → 成功或失败分支”。任何超时、HTTP 错误、任务失败或部分失败都要保留原节点的错误字段和下游分支语义。
- 写入剪映草稿时，沿用最新的 `draft_url`；不要使用早期草稿地址覆盖后续写入。

### 3. 执行纯 Python 节点

在项目根目录执行以下命令。输入 JSON 必须是节点输入对象，而不是包裹了 `input` 的 Schema 文件：

```powershell
python skills\daily-update-te\scripts\run_node_contract.py --list-nodes
python skills\daily-update-te\scripts\run_node_contract.py --node timeline_planning --input samples\synthetic\timeline_planning_input.json --expected samples\synthetic\timeline_planning_expected.json
```

从全局 Skill 路径直接调用脚本时，先切换到项目根目录；无法切换时可设置 `$env:DAILY_UPDATE_TE_ROOT='C:\你的项目路径'`。脚本不会把全局 Skill 目录误当成项目源码目录。

当前脚本支持已具备离线实现的节点：`timeline_planning`、`end_frame_extension`、`prompt_generation`、`video_infos`、`scene_type_recognition`、`host_task_assembly`、`bgm_task_assembly`、`video_data_aggregation`、`keyframes_infos`。传入 `--expected` 时逐字段比较并在有差异时返回非零退出码；不传时只输出实际 JSON。字幕不再使用旧的 `subtitle_data` 纯 Python 节点，而是固定走 TTS -> CapCut STT -> Mini -> 镜头坑位链路。

在开始真实运行前，先检查本地与外部依赖就绪度：

```powershell
python skills\daily-update-te\scripts\check_readiness.py
python skills\daily-update-te\scripts\check_readiness.py --probe-capcut
```

第一条只检查本地文件和离线节点；第二条在存在 `CAPCUT_MATE_BASE_URL` 时额外探测 `/openapi.json`。输出中的“仍需 transport”是阻断清单，不代表这些节点已经执行成功。

`check_readiness.py` 会校验用户提供的 8364 归档，并输出 61 个可执行节点的状态矩阵（52 个顶层节点 + 9 个批处理/循环内部节点）：`offline_verified` 表示离线契约证据，`transport_mapped` 表示字段和路由已接入，`verified_live`/`verified_live_route` 表示已有真实服务证据，`transport_pending` 和 `source_placeholder` 表示不可越过的阻断。
输出中的“当前仍需你协助”是动态清单：它会区分本机环境配置、缺失源码/确认项、可访问媒体样例和外部 transport；当前版本的 `directors_v2` 源码已经接入，不要重复索要该源码。

工作流编排入口当前执行已接入前缀并在缺 transport 处停止：

```powershell
python skills\daily-update-te\scripts\run_workflow.py
python skills\daily-update-te\scripts\run_workflow.py --execute --input samples\synthetic\workflow_start_input.json --base-url http://127.0.0.1:30000
```

不带 `--execute` 不产生副作用；带 `--execute` 会创建新的测试草稿，若未配置 `DIRECTORS_V2_API_KEY`，结果会在 `directors_v2` 节点明确返回 `blocked`/失败原因。
Python 调用方可以向 `run_daily_update_te(..., node_runners={"102833": ..., "159953": ..., "165901": ..., "197742": ..., "116616": ..., "139488": ...})` 注入单节点 transport；编排器会按真实字段契约执行内部 `197742/127095`、Host 前缀 `116616/156199/139488/117861`，推进到第一个未接入插件，并在 `completed_nodes`、`outputs`、`blocked_nodes` 中保留证据。

### 4. 调用 CapCut Mate 剪映插件层

`capcut-mate` 是剪映编辑插件的真实实现来源。其 `main` 分支提交为 `f2581bd989190ee61719f2bf50e118ab035dcc7f`，接口文档和节点映射见 [capcut-mate.md](references/capcut-mate.md)。Skill 不自动调用公网服务，也不自动启动 CapCut Mate；调用前必须由用户提供已启动的服务地址。

```powershell
$env:CAPCUT_MATE_BASE_URL='http://localhost:30000'
python skills\daily-update-te\scripts\capcut_mate_client.py --endpoint create_draft --input samples\synthetic\create_draft_input.json
python skills\daily-update-te\scripts\smoke_capcut_mate.py --base-url http://127.0.0.1:30000
# 可直接传入 CapCut Mate 能访问的媒体 URL，脚本默认各写入前 1 秒：
python skills\daily-update-te\scripts\smoke_capcut_mate.py --base-url http://127.0.0.1:30000 --video-url http://host.docker.internal:30123/sample.mp4 --audio-url http://host.docker.internal:30123/sample.mp3
# 在视频写入后，继续按 8364 常量验证关键帧和两种特效：
python skills\daily-update-te\scripts\smoke_capcut_mate.py --base-url http://127.0.0.1:30000 --video-url http://host.docker.internal:30123/sample.mp4 --with-keyframes-effects
# 只有拿到 CapCut Mate 可访问的真实素材 JSON 后才追加：
python skills\daily-update-te\scripts\smoke_capcut_mate.py --base-url http://127.0.0.1:30000 --video-infos-file path\to\video_infos.json --audio-infos-file path\to\audio_infos.json
```

客户端保持请求 JSON 原样传给 `/openapi/capcut-mate/v1`，保留服务端返回结构；`--dry-run` 只打印请求，不产生草稿。成功返回不等于整条工作流成功，必须继续核对下一个节点传入的最新 `draft_url`。

当前附件版 `directors_v2` 使用以下本机配置，不要把密钥写进 JSON 或聊天：

```powershell
$env:DIRECTORS_V2_API_KEY='在本机填入'
$env:DIRECTORS_V2_SYSTEM_PROMPT='必须输出包含 director_plan、ok、segment_beats、segments 的 JSON'
$env:AISHUCH_API_KEY='在本机填入；不要写入 JSON、日志或聊天'
```

该插件原始输出字段是 `output_5_5`；如果返回普通文本或不符合 8364 四字段结构，适配器会阻断并报告契约不匹配。

Seedance `video_generate/video_query` 的脱敏源码通过 `workflow_1256.seedance_transport.SeedanceHTTPTransport` 接入；它只从 `SEEDANCE_PRIMARY_*`、可选备用/第三/第四账号、模型覆盖变量和 `SEEDANCE_TOS_*` 环境变量读取配置。自动模式先调用 1.5 Pro，额度/权限/接入点级失败后再调用 1.0 Pro；未配置主 Key 时必须返回阻断。

### 5. 验收和报告

- 修改后先运行目标节点测试，再运行项目回归：`$env:PYTHONPATH='src'; python -m pytest -q`。
- 报告必须区分：代码级等价、synthetic 测试、真实 Coze 节点证据、Python 外部服务端到端证据。
- 涉及真实 API、视频、图片或草稿时，检查实际返回结构、数组数量和顺序、URL/任务 ID、时间单位及最终生成文件；未执行的外部链路写明阻断原因。
- 发现差异时优先修正 Python 实现或适配层，不通过改变原始 Coze 语义来“合理化”差异。

## 安全和失败处理

- 如果缺少输入字段，按原节点契约报告字段路径，不用默认值掩盖输入错误。
- 如果缺少 transport、API Key、网络或外部服务，报告为“外部依赖未验证”，不要输出伪造的 `task_id`、URL、草稿地址或成功状态。
- 如果发现工作流版本、节点数量、节点 ID 或字段来源与参考文件不一致，暂停执行并先重新审计导出的 YAML。
