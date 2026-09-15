# Graph Report - codex project  (2026-09-15)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 244 nodes · 621 edges · 15 communities (10 shown, 5 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS · INFERRED: 3 edges (avg confidence: 0.95)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9
- Community 10
- Community 11
- Community 12
- Community 13
- Community 14

## God Nodes (most connected - your core abstractions)
1. `ValidationError` - 36 edges
2. `TopicCenterService` - 32 edges
3. `TopicGovernanceStore` - 23 edges
4. `ConfigService` - 18 edges
5. `TopicMigrationHandler` - 13 edges
6. `_clean_text()` - 13 edges
7. `PersistenceError` - 11 edges
8. `utc_now()` - 11 edges
9. `collect_topic_source()` - 11 edges
10. `TrendRadarAdapter` - 10 edges

## Surprising Connections (you probably didn't know these)
- `TikHubTransport` --uses--> `ConfigService`  [INFERRED]
  tikhub_transport.py → config_service.py
- `TopicMigrationHandler` --uses--> `TopicCenterService`  [INFERRED]
  server.py → topic_service.py
- `TopicMigrationHTTPServer` --uses--> `TopicCenterService`  [INFERRED]
  server.py → topic_service.py
- `_non_empty_text()` --calls--> `ValidationError`  [EXTRACTED]
  config_service.py → errors.py
- `_normalize_endpoint()` --calls--> `ValidationError`  [EXTRACTED]
  config_service.py → errors.py

## Import Cycles
- None detected.

## Communities (15 total, 5 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.21
Nodes (17): NotFoundError, ValidationError, _append_state_history(), _clean_text(), _content_items(), _http_url(), _json_safe(), normalize_source_text() (+9 more)

### Community 1 - "Community 1"
Cohesion: 0.13
Nodes (23): _atomic_write_secret(), _atomic_write_text(), ConfigService, default_runtime_root(), _harden_secret_file(), _mask_secret(), _non_empty_text(), _normalize_endpoint() (+15 more)

### Community 3 - "Community 3"
Cohesion: 0.14
Nodes (10): MigrationError, NotMigratedError, ProviderError, RuntimeError, 可安全返回给 HTTP 客户端的业务错误。, _pid_running(), F0 服务治理：单实例维护锁和活动任务状态。, 使用原子创建文件避免两个目标服务共用可写运行目录。 (+2 more)

### Community 4 - "Community 4"
Cohesion: 0.22
Nodes (16): _extract_duration(), filter_short_videos(), _first_nested(), _iter_nested(), _mapping_items(), _normalize_item(), _number(), _parse_duration_seconds() (+8 more)

### Community 5 - "Community 5"
Cohesion: 0.19
Nodes (13): _as_int(), _candidate_id(), _clean(), _normalize_terms(), Any, Path, RuntimeError, Read-only adapter for an external TrendRadar SQLite snapshot. (+5 more)

### Community 6 - "Community 6"
Cohesion: 0.16
Nodes (14): utc_now(), HTMLParser, _base(), _clean_text(), collect_topic_source(), detect_source_platform(), _fetch_html(), Any (+6 more)

### Community 7 - "Community 7"
Cohesion: 0.13
Nodes (13): BaseHTTPRequestHandler, Exception, create_server(), _first(), main(), _page(), Any, Path (+5 more)

### Community 8 - "Community 8"
Cohesion: 0.32
Nodes (8): adapt_topic_content(), _http_url(), _provenance(), Any, 把治理存储中的正文投影为公开交接契约，不回写源记录。, _text(), TopicContentManifest, 第一批独立迁移：公共基础、control-plane 配置与选题中心。

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (12): Any, ConfigService, Path, TikHubTransport, _atomic_json(), _clean_list(), default_runtime_root(), _normalize_limit() (+4 more)

### Community 10 - "Community 10"
Cohesion: 0.39
Nodes (7): _article_url(), professional_media_contents(), ProfessionalMediaError, Any, 专业媒体最小入口。 只保留源码审计确认的三个供应商及其当前能力：人人都是产品经理支持 公开 RSS 近期文章，雪球和 36 氪只支持用户粘贴 HTTPS…, 专业媒体输入或公开 RSS 响应不符合边界。, ValueError

## Knowledge Gaps
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ValidationError` connect `Community 0` to `Community 1`, `Community 3`, `Community 7`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.190) - this node is a cross-community bridge._
- **Why does `TopicCenterService` connect `Community 9` to `Community 8`, `Community 7`?**
  _High betweenness centrality (0.179) - this node is a cross-community bridge._
- **Why does `ConfigService` connect `Community 1` to `Community 8`, `Community 4`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `TopicCenterService` (e.g. with `TopicMigrationHandler` and `TopicMigrationHTTPServer`) actually correct?**
  _`TopicCenterService` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.13174603174603175 - nodes in this community are weakly interconnected._
- **Should `Community 3` be split into smaller, more focused modules?**
  _Cohesion score 0.13725490196078433 - nodes in this community are weakly interconnected._
- **Should `Community 7` be split into smaller, more focused modules?**
  _Cohesion score 0.12615384615384614 - nodes in this community are weakly interconnected._