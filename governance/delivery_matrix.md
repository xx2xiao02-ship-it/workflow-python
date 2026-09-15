# F0/F1 迁移交付矩阵

记录依据：当前目标工作树生产 aggregate SHA-256 91b320d9d1f5288e38fcc23dde883a9024a03c2e2561880ecb3ef0140c627b1a。源文件路径和源 hash 见 migration_file_register.json；源项目工作树为 dirty，hash 来自当前文件内容而不是只取 Git HEAD。

## 页面与 API

| 前端能力 | 页面/API | 目标业务模块 | Transport/存储 | 当前验证 |
| --- | --- | --- | --- | --- |
| 读取主题 | GET /api/topic-center/topics | topic_service | TrendRadar 只读 SQLite 快照 | 离线测试通过；真实快照待浏览器验证 |
| 预览来源 | GET /api/topic-center/topics/{candidate_id}/preview | topic_service | 公开 HTML opener；知乎详情按需 deferred | 离线拒绝外部请求测试通过 |
| 查询相关内容 | POST /api/topic-center/topics/{candidate_id}/enrich | topic_service | TikHubTransport.search_related | synthetic Transport 合同通过；真实供应商未执行 |
| 读取缓存 | GET /api/topic-center/topics/{candidate_id}/contents | topic_service | 外置运行时缓存 | 读取不触发请求测试通过 |
| 建立选题包 | POST /api/topic-center/selections | governance_store | 外置 JSON 存储 | ID 对应测试通过 |
| 采集正文 | POST /api/topic-center/selections/{selection_id}/collect | topic_service | topic_collection + 注册 Transport | synthetic 正文和闸门测试通过 |
| 专业媒体 | POST /api/topic-center/professional-media | professional_media | RSS/HTTPS 文章请求 | URL 校验和标题非正文测试通过；真实媒体未验证 |
| 删除选题 | POST /api/topic-center/selections/{selection_id}/delete | governance_store | 保留历史/资产的外置存储 | 缺确认拒绝和删除 HTTP 测试通过 |
| 归档素材 | POST /api/topic-center/sources/{source_content_id}/archive | governance_store | 外置 JSON 存储 | 业务契约测试通过 |
| 进入文案 | GET /api/topic-center/writing-input | governance_store -> contracts | TopicContentManifest | 仅 CONTENT_READY 可交接，hash/ID/provenance 测试通过 |
| API 配置读取 | GET /api/api-management | config_service | 外置 api_management.json + 外置凭据文件 | 重启读取和脱敏测试通过 |
| 保存配置 | POST /api/api-management/config | config_service | control-plane 配置边界 | 不触发外部请求；非法值拒绝测试通过 |
| 测试连接 | POST /api/api-management/test-connection / auth-verify | config_service | 独立 deferred 结果 | executed=false，未确认免费性所以未发请求 |
| 健康与维护锁 | GET /api/executor/health /healthz | service_governance | 独立运行目录锁文件 | 标准库服务启动测试通过 |

## 配置项到 Transport

| 配置项 | 使用模块 | Transport | 验证结果 |
| --- | --- | --- | --- |
| selected_provider=tikhub、group_id=tikhub、channel_id=tikhub | config_service -> topic_service | TikHubTransport | 读取、保存和非法供应商拒绝通过 |
| settings.endpoint | topic_service 的按需扩展 | TikHubTransport.search_related | synthetic 请求 URL 使用保存值通过 |
| settings.timeout_seconds | topic_service 的按需扩展 | TikHubTransport HTTP opener | synthetic 请求 timeout 使用保存值通过；单位为秒 |
| settings.max_retries | topic_service 的按需扩展 | TikHubTransport | 只允许 0/1，非法值拒绝通过 |
| credential_ref=tikhub.primary_api_key | config_service | TikHubTransport Authorization | 凭据仅外置存储；响应和快照不泄露；真实请求未执行 |
| connection_status | config_service / API 配置页 | 无，连接测试独立 | 保存后重置为 not_tested；不得伪造已连接 |

## 依赖和边界

- control-plane 配置由 ConfigService 唯一提供；选题中心不读取配置模块内部文件。
- TrendRadar 只读快照；运行时配置、凭据、缓存和正文存储均由外部 runtime-root 注入。
- 目标生产包只依赖 Python 标准库和登记的 topic_migration 模块；不导入 workflow_1256、旧控制台、src 或动态模块。
- /writing 和 /writing-governance 明确显示“尚未迁移”；/api/topic-center/douyin 返回 501 not_migrated。
- synthetic 测试只证明字段、ID、hash、边界和 Transport 选择，不证明真实供应商连接或生产业务 E2E。

## 当前验收状态

- implemented：F0 公共基础、control-plane API 配置、F1 选题中心和交接契约已写入。
- tested：业务 pytest、治理 pytest、compileall、Import Linter、Ruff、Graphify 和登记门禁已通过。
- externally_validated：真实浏览器验证在最终联合验收阶段记录；真实 TikHub、专业媒体和生产 E2E 未执行。
- user_acceptance：pending_user_acceptance，未经用户确认不标记为生产可用。
