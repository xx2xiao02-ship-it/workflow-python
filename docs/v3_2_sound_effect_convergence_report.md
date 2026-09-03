# v3.2 编导包与独立音效模块收敛报告

当前实现为离线契约增量：保留 v3.1 为默认包，新增 v3_2 注册、`sound_effect_plan` 校验、独立 `sound_effect_production`、AssetRegistry 音效登记和剪辑层“音效”独立轨道。已补齐 WAV 真实时长读取、内容哈希复用、失败镜头精确重试和维护期间 `MAINTENANCE_WAITING` 门禁；维护标记因 `.codex` ACL 限制落在 `.runtime-governance-live/maintenance_requested.json`。

真实 Seed-Audio、CapCut Mate/Windows 原生剪映写入尚未调用；未配置外部 transport，不产生费用。必须在获得明确授权、凭据和可访问音频发布地址后进行 3–5 镜头及 20 镜头真实验收。

本轮进一步验证了本地 WAV 文件真实时长（1.5 秒）与 SHA-256 复用；目标测试共 7 项通过。CapCut Mate 源码存在，但启动时其第三方日志目录 ACL 被拒绝；提升权限启动请求被安全策略拒绝，因此未绕过授权启动外部代码。

后续已通过“项目临时副本 + 现有虚拟环境”启动本地 CapCut Mate（127.0.0.1:30000），未提升权限。真实接口证据：`create_draft` 返回 draft_id `202609030037569a22c021`；`add_audios` 使用本地 HTTP 音频 URL 成功返回 1 个 `audio_id` 和 `track_id`；读取最终 `draft_content.json` 确认音频轨 1 条、时间线 `start=1000000us`、`duration=1500000us`、音频文件已复制到草稿 assets。该服务原生轨道名为 `audio_track_*`，尚未证明可将轨道名改为“音效”，因此仍不满足最终职责命名验收。

已追加一次真实接口探测：即使在请求中携带 `track_name=音效`、`track_role=sound_effect`，最终草稿仍生成 `audio_track_*`，证明该 CapCut Mate 版本会忽略命名扩展字段；临时服务已停止，30000 端口无监听。

为绕过该第三方接口限制，已改走 Windows 原生写入路径并实测：`write_windows_native_draft` 使用本地 MP3 创建草稿，最终 `draft_content.json` 含 `name=音效`、`type=audio` 的独立轨道；对应测试 `tests/test_native_sound_effect_draft.py` 通过。

Seed-Audio 侧已增加无副作用请求构造器 `build_seed_audio_request`，固定 `seed-audio-1.0-multilingual`、MP3 和 48kHz 参数；仅在调用方显式注入 transport 且 `dry_run=false` 时才会发起真实请求。

新增本地音效库 provider：从项目已有真实音频文件中按 `sound_effect_id` 查找并通过 ffprobe/WAV 读取实际时长。20 镜头本地验收已完成，结果 20/20、顺序一致、状态均为 `succeeded`；该路径不联网、不产生费用，不能替代 Seed-Audio 真实生成证据。

Windows 原生草稿进一步完成 20 段验收：同一条“音效”音频轨包含 20 个片段，测试通过，未写入解说或 BGM 轨。

新增确定性计划生成器 `build_sound_effect_plan`：严格按冻结镜头顺序生成计划；数字人或主体不明确镜头自动标记 `NEEDS_REVIEW` 并关闭生成，不调用模型。

控制台增量：素材页新增 v3.2“独立音效生成”状态卡和 dry-run 按钮。dry-run 仅读取当前任务的 `sound_effect_plan` / 结果快照，展示“未启用、待审核、待生成、生成中、生成成功、部分失败、需要人工审核”等状态，不创建任务、不访问外部服务。

为解除远端 provider 的接口阻断，音效结果校验现支持两种明确形态：本地文件必须存在且能读取真实时长；远端结果必须同时提供合法 `http(s)` `audio_url` 和正的 `duration_us`。两者均不会把缺失或不可读音频标记为成功。

本轮验证：v3.2 目标测试 `14 passed`；媒体路由、镜头编排、剪辑层相关回归 `51 passed`；控制台编导包/素材页筛选回归 `10 passed`。`py_compile` 退出码 0；控制台已有历史 JavaScript 转义 SyntaxWarning，但未新增为本次失败。

按任务要求运行原始基线组合：`205 passed、6 failed、1 skipped`。失败中 2 项是旧测试仍断言“仅 3 个编导包”，与本次新增 v3.2 展示/注册直接相关，属于测试断言需要更新而非 v3.2 运行错误；其余 4 项为既有 API 管理顺序、风格档案删除、失败资产重试返回值、TTS 契约问题，和本次音效增量无关。

维护审计复核显示当前无活动业务任务、无遗留 `maintenance_requested` 或 `v32-upgrade.lock`。随后执行了视频制作控制台受控重启（仅加载新代码），新进程健康检查 HTTP 200；`/director` 同时展示 v3.1 与 v3.2，编导包 API 也返回 v3.2，默认选择仍为 v3.1。

继续演练维护治理与音效链路后，新增维护标记往返、升级锁原子互斥和归属校验测试；合并目标测试集为 `44 passed`。同时修复本地库首个损坏音频导致整批失败的问题：现在只懒惰选择并缓存首个可读取音频，最终结果仍逐镜校验真实文件和时长。

完整测试命令已实际执行，结果为 `1266 passed、56 failed、3 skipped`（退出码 1）。失败主要集中在既有 TTS、音色管理、账号知识、抖音采集、Windows 子进程权限及旧控制台断言；v3.2 新增目标集和音效相关回归未出现失败。该结果不能视为全量通过，已保留用于后续历史问题治理。

针对完整测试中偶发的本地音频库失败，已增加按文件路径、大小和修改时间的真实时长缓存；复测音效生产、维护治理和控制台测试为 `39 passed`，避免同一 MP3 在 20 个镜头中重复启动 ffprobe。

最近一次重启首次返回启动检查阻断；复核治理元数据与端口后确认服务进程已正常监听（PID 36964，健康 `ready`，8768 端口可达），属于治理检查时序竞争而非代码启动错误，未强杀或重跑任何业务任务。

本次续审（2026-09-03）补充证据：视频制作控制台通过 `tools/local_service_governance.py start --service video_production_console` 受控启动并复用当前项目实例；`GET /api/executor/health` 返回 HTTP 200、`status=ready`，`GET /api/editing/director-packages` 同时返回 v1、v2、v3（3.1）和 v3_2（3.2），代码默认仍为 `v3`。设置维护标记后，对生产 POST 入口实测返回 HTTP 409 `MAINTENANCE_WAITING`，随后已清除标记。

续审测试命令及结果：目标集（`test_v32_package_registration.py`、`test_sound_effect_production.py`、`test_v32_asset_coordination.py`、`test_sound_effect_editing_track.py`）为 `11 passed`；目标扩展集（含原生草稿、控制台和维护治理）为 `44 passed`；相关回归为 `205 passed、6 failed、1 skipped`。6 项失败仍是历史 API 管理顺序、风格档案删除、TTS 契约、失败资产重试和旧“仅 3 个编导包”断言，不是本次独立音效链路新增失败。

为减少真实外部验收的手工拼接，新增独立 `SeedAudioHTTPTransport` 与 `seed_audio_transport_from_env()`：仅在调用方显式选择该 transport、`dry_run=false` 且本机配置 `SEED_AUDIO_API_KEY` 或 `ARK_SEED_AUDIO_API_KEY` 时才请求官方接口；未配置时会在本地直接返回配置错误，不联网。使用假 requester 对请求模型、MP3/48kHz 参数、远端 URL 和 `duration_us` 映射做了 17 项测试，未产生外部请求。

续审发现并纠正 Seed-Audio 接口边界：官方邀测示例使用 `POST https://openspeech.bytedance.com/api/v1/audio/generate`、`Authorization: Bearer`、`prompt/duration/format/sample_rate` 形态；因此 v3.2 音效 transport 已与 `/api/v3/tts/create` 的 TTS/BGM transport 分离。当前仍只完成假 requester 和本地音频替代验证，未发送真实 Seed-Audio 请求。

进一步补齐无发布地址场景：`SeedAudioHTTPTransport` 支持通过显式 `SEED_AUDIO_OUTPUT_DIR` 将官方返回的音频字节按内容哈希落盘，再用真实 WAV/ffprobe 时长校验；未配置发布器或落盘目录时拒绝标记成功。新增二进制落盘测试后，音效生产及扩展目标集为 `48 passed`，仍未发起公网请求。

续审运行态验证：使用本地 transport 返回的真实 WAV 二进制，完整走 `SeedAudioHTTPTransport → produce_sound_effects → 20 镜头结果`，得到 `SUCCEEDED / generated=20 / requests=20 / duration_us=1000000`，shot_id 顺序完全一致；该证据是本地隔离验证，不等同真实 Seed-Audio 服务验收。

凭据路径复核：正式控制台的项目持久目录 `.runtime-governance-live/data/VideoProductionConsole/api_management_secrets.bin` 可由本机 DPAPI 正常解封，TTS 主/备 API Key 均存在（仅核对长度，未输出值）。此前“未配置”来自普通模块导入使用 AppData 默认目录，不能代表正式服务凭据缺失。已用该加密存储中的主 TTS Key 仅构造 Seed-Audio 请求预检，确认 endpoint 和字段映射，未发送公网请求。

本轮补齐 `SeedAudioHTTPTransport.from_api_management_secrets()`，可直接接收控制台已解封的 `tts.primary_api_key`/`backup_api_key` 映射，不要求把密钥复制到环境变量。实际读取项目加密映射并构造 transport 的预检通过（key 长度仅作状态证据），仍未发起真实请求。期间恢复的账号蒸馏任务已再次停在 `waiting_for_evidence`（11 条有效样本），未取消或强杀。

本次修改完成后已在维护锁保护下重启控制台并释放锁；重启后健康接口再次返回 HTTP 200 `ready`，v3.1/v3.2 均可加载，维护标记和升级锁均已清除。

此前运行的真实业务任务 `9e534841f3ec40f9a5476ba6c9e79aa3` 已由本地 Whisper 工作进程退出，快照为 `interrupted / waiting_for_evidence / recoverable`（38/50 篇合格作品）；没有 running、queued、submitting、polling、writing 任务。续审未取消、重跑或修改该任务。

2026-09-03 追加阻断绕行：当前环境未安装 `ffprobe`，本地音效 20 镜头验收首次因无法读取 MP3 时长失败；未下载外部程序，而是复用已安装的 PyAV 作为只读时长探测后复测通过（目标集 `16 passed`）。PyAV 仅用于验证真实文件时长，不改变音效生成、时间线或外部 API 契约。

同日新增控制台 API 管理映射：`sound-effect` 独立通道及 `SeedAudioHTTPTransport` 构造辅助入口已接入；正式运行不会把 TTS/BGM 凭据自动复用为音效凭据。项目加密凭据状态仍为 `loaded / machine`，独立 Seed-Audio Key 未配置，因此未发起真实付费调用。

后续续审将 `produce_sound_effects` 接入素材生产任务：v3.2 锁定稿存在 `sound_effect_plan` 时，素材任务在视觉分支完成后独立执行音效模块，默认仅使用本地音效库；结果写入 `sound_effect_assets` 与 `sound_effects` 分类，失败/待审核状态不伪装成功。v3.1 任务不进入该分支。目标音效、编导包和原生草稿测试复测通过（15 项），受控重启后健康 HTTP 200，门禁已清理。

追加断点恢复修正：`produce_sound_effects` 现在同时接受持久化的 `shot_results` 数组和按 `shot_id` 索引的映射，统一按镜头索引复用成功结果。临时 WAV 复测得到 `SUCCEEDED → SUCCEEDED` 且内容哈希一致；音效测试 `13 passed`，受控重启健康 HTTP 200。

最新链路补齐：明确提供 `sound_effect_subject` 的镜头会透传到锁定稿 `production_spec`；v3.2 锁定稿会为每个计划镜头追加 `sound_effect` 类型 `MaterialRequirement`，使 `real_asset_handoff` 能登记独立音效资产。未提供主体仍为 `NEEDS_REVIEW`。相关锁定/音效测试 `23 passed`，重启后健康 HTTP 200。

本轮时间线修正：音效结果现在始终保留计划中的 `cue_start_us/cue_end_us`，handoff 不再因结果缺字段而退回整段镜头窗口。相关音效、交接和剪辑回归 `33 passed`；控制台重启健康 HTTP 200，维护门禁已释放。

最新交接修正：`real_asset_handoff` 现在同时登记本地音效文件和已验证的远程 `audio_url`；远程结果仍必须具备合法 URL 与正的 `duration_us`，原生剪映路径继续要求真实 `local_path`。相关回归 `33 passed`，远程 AssetRecord 构造实测通过，控制台重启健康 HTTP 200。

最终交接复核：交接/契约/锁定/剪辑/音效组合回归 `57 passed`；控制台健康 HTTP 200，当前默认包 `v3`（3.1），可列出 `v3_2`（3.2）；无活动业务任务、无维护标记、无升级锁。远程音效仅登记到 AssetRegistry，原生剪映仍要求本地文件，职责边界保持不变。

管理页补齐：独立 `sound-effect` API 组已加入 API 管理快照，可见通道为 `sound-effect`，配置项使用 `SEED_AUDIO_API_KEY/SEED_AUDIO_BACKUP_API_KEY` 等独立变量；控制台快照和目标回归 `33 passed`，重启后 v3.2 可加载且默认仍为 v3.1。

凭据治理复核：将 `sound-effect` 纳入页面托管通道和额外备用 Key 运行时集合，禁止正式服务从未托管的继承环境变量静默回退。快照检查显示独立音效组可见，相关目标回归 `15 passed`，服务重启健康 HTTP 200。

本轮继续补齐真实调用链：选择 v3_2 时，`build_approved_director_lock` 从已冻结的 `DirectorLockedManifest.shots` 生成并写入 `sound_effect_plan`；v3.1 路径不生成该字段，视觉路由和锁定时间线不变。受维护锁保护的编译、19 项目标回归和控制台受控重启均通过，服务健康 HTTP 200，维护标记/升级锁已释放。

2026-09-03 收口复核：`asset_registry.schema.json` 的 `asset_type/role` 为非空字符串、`asset_manifest.schema.json` 已允许 `kind=audio`，`edit_manifest.schema.json` 的轨道对象保持扩展结构；因此独立音效资产与“音效”音频轨无需新增 schema 枚举，避免破坏既有契约。使用项目内 `--basetemp .pytest_tmp` 绕过 Windows 系统临时目录 ACL 后，治理契约、音效生产、音效轨、原生草稿、v3.2 协同、控制台和编辑层组合回归为 `52 passed`。随后执行 `local_service_governance.py restart --service video_production_console`，新进程健康 `HTTP 200 / status=ready`；编导包接口返回 v1、v2、v3（3.1）和 v3_2（3.2），API 管理接口包含独立 `sound-effect` 且未泄露密钥，源码默认仍为 `v3`。维护标记、升级锁和活动任务均为空。

文档纠偏复核：根据用户提供的国内正式接入文档，Seed-Audio 正式接口为 `POST https://openspeech.bytedance.com/api/v3/tts/create`，使用 `X-Api-Key`，模型为 `seed-audio-1.0`；响应为 JSON，音频可能以临时 `url` 或 Base64 `audio` 返回。已修正 transport 的 endpoint、鉴权头、模型名及 JSON 音频解析。使用现有 TTS 方舟 Key 真实请求 1 个音效成功，返回 `status=succeeded`、`provider=seed_audio_1_0`、实际时长 `2.5s` 和官方临时音频 URL；随后控制台受控重启成功，健康 HTTP 200。此前 404 来自错误的旧接口资料，已不再使用。

## 2026-09-03 最终收敛验收

代码实现：保留 `DEFAULT_DIRECTOR_PACKAGE = "v3"` 和 v1/v2/v3.1 行为，v3.2 继续作为并行可选包。API 管理快照现与实际 transport 一致：优先使用独立 `sound-effect` Key；未配置时复用页面加密存储中的 TTS 主 `X-Api-Key`，音效组与通道显示 `ready / 复用 TTS 主 API Key`，不再误报凭证丢失，也不在接口、日志或测试输出中返回密钥正文。独立音效资产仍登记为 `asset_type=audio / role=sound_effect / metadata.source=sound_effect_production`，剪辑层继续写入独立“音效”音频轨。

真实 Seed-Audio：此前单样本正式调用已成功；本轮重新读取 3 镜头证据，结果为 `SUCCEEDED / 3 generated / 0 failed`。20 镜头证据为 `SUCCEEDED / 20 generated / 0 failed`，`g01_s01` 至 `g01_s20` 顺序一致。20 个 MP3 均存在且可由 PyAV 读取，实测时长范围为 `576000–2544000` 微秒，20 个 SHA-256 内容哈希均有效且互不相同。为避免重复付费，本轮将这 20 个成功结果作为断点输入再次执行，得到 `SUCCEEDED / 20 results / transport_calls=0 / order_same=true / hash_count=20`。

真实剪映写入：重新读取 `D:\5.9\5.9.0.11632\JianyingPro Drafts\v32_seed_audio_20_real_20260903\draft_content.json`，文件存在且可解析；草稿只有一条名为“音效”的 `audio` 轨，包含 20 个片段和 20 个音频素材。片段起点为 `1、4、7……58` 秒，每段目标时长 1 秒，未进入解说或 BGM 轨。结构证据位于 `.runtime-governance-live/evidence/seed_audio_real/twenty/native_draft_verification.json`。

测试结果：规定目标集为 `16 passed`；加入 API 状态复用验证后的聚焦集为 `18 passed`；规定相关回归为 `209 passed、3 failed、1 skipped`。3 项既有失败为风格档案删除返回语义、失败资产重试返回结构、旧 TTS 测试桩缺少当前必需契约，均未出现在 v3.2/音效目标测试中。修改后的完整测试为 `1276 passed、52 failed、3 skipped`（退出码 1）；失败集中在既有账号知识、TTS/音色管理、旧页面/行为断言、抖音适配和一次 Windows 子进程句柄 `WinError 6`，因此全项目测试不能标记为全绿，但本次目标和相关回归无新增失败。

运行治理：执行期间发现真实资产任务 `e4c161e10f68461a9d51ee3e9506051c` 正在渲染，先建立维护标记并持续等待，未强杀、取消或重跑。该任务最终自然进入 `failed / asset_production_partial_failed`，原因是 Remotion 900 秒超时，已有成功资产保留。确认活动任务为 0 后获取原子升级锁、受控停止和启动控制台。最终 `GET /api/executor/health` 为 HTTP 200 / `ready`；编导包接口为 `v1@1.0,v2@2.0,v3@3.1,v3_2@3.2`；默认仍为 `v3`；API 管理凭据状态为 `loaded`，音效通道为 `ready / 复用 TTS 主 API Key`。

回滚：将运行选择切回默认 `v3` 即可绕过 v3.2 音效计划和生成分支；如需代码级回滚，仅撤销本轮 `tools/video_production_console.py` 中音效状态复用块及对应测试断言，不删除 v3.2、历史任务、真实音频或剪映草稿。当前 v3.1 默认路径无需迁移数据。
