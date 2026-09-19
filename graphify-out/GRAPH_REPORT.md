# Graph Report - api-config-delivery-20260919  (2026-09-19)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 291 nodes · 826 edges · 18 communities (10 shown, 8 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 27 edges (avg confidence: 0.95)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e2f67722`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

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
- Community 15
- Community 16
- Community 17

## God Nodes (most connected - your core abstractions)
1. `ValidationError` - 54 edges
2. `TopicCenterService` - 39 edges
3. `ConfigService` - 32 edges
4. `TopicGovernanceStore` - 28 edges
5. `UnifiedApiTransport` - 21 edges
6. `PersistenceError` - 21 edges
7. `utc_now()` - 17 edges
8. `TopicMigrationHandler` - 16 edges
9. `NotFoundError` - 15 edges
10. `_clean_text()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `UnifiedApiTransport` --uses--> `ConfigService`  [INFERRED]
  api_transports.py → config_service.py
- `UnifiedApiTransport` --uses--> `ValidationError`  [INFERRED]
  api_transports.py → errors.py
- `create_server()` --uses--> `UnifiedApiTransport`  [INFERRED]
  server.py → api_transports.py
- `TopicMigrationHTTPServer` --uses--> `UnifiedApiTransport`  [INFERRED]
  server.py → api_transports.py
- `ConfigService` --uses--> `ValidationError`  [INFERRED]
  config_service.py → errors.py

## Import Cycles
- None detected.

## Communities (18 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.12
Nodes (30): _atomic_write_secret(), _atomic_write_text(), _base_channel(), ConfigService, default_runtime_root(), _dpapi(), _effective_verification(), _harden_secret_file() (+22 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (23): BaseHTTPRequestHandler, MigrationError, NotMigratedError, ProviderError, RuntimeError, 可安全返回给 HTTP 客户端的业务错误。, Exception, create_server() (+15 more)

### Community 2 - "Community 2"
Cohesion: 0.21
Nodes (16): NotFoundError, _append_state_history(), _clean_text(), _content_items(), _http_url(), _json_safe(), normalize_source_text(), Any (+8 more)

### Community 3 - "Community 3"
Cohesion: 0.16
Nodes (10): _atomic_json(), _clean_list(), default_runtime_root(), _normalize_limit(), _normalize_platforms(), Any, Path, 选题中心业务编排层。 该层只依赖已登记的 TrendRadar 只读适配器、control-plane ConfigService、… (+2 more)

### Community 4 - "Community 4"
Cohesion: 0.14
Nodes (19): _apimodels_balance_url(), _ark_models_auth_url(), _base_channel(), _bearer_header(), _effective_api_key(), _model(), _origin(), RuntimeError (+11 more)

### Community 5 - "Community 5"
Cohesion: 0.21
Nodes (17): _bearer_header(), _extract_duration(), filter_short_videos(), _first_nested(), _iter_nested(), _mapping_items(), _normalize_item(), _number() (+9 more)

### Community 8 - "Community 8"
Cohesion: 0.19
Nodes (13): _as_int(), _candidate_id(), _clean(), _normalize_terms(), Any, Path, RuntimeError, Read-only adapter for an external TrendRadar SQLite snapshot. (+5 more)

### Community 10 - "Community 10"
Cohesion: 0.18
Nodes (13): utc_now(), HTMLParser, _base(), _clean_text(), collect_topic_source(), detect_source_platform(), _fetch_html(), Any (+5 more)

### Community 11 - "Community 11"
Cohesion: 0.36
Nodes (9): adapt_topic_content(), _http_url(), _provenance(), Any, 把治理存储中的正文投影为公开交接契约，不回写源记录。, _text(), TopicContentManifest, ValidationError (+1 more)

### Community 13 - "Community 13"
Cohesion: 0.39
Nodes (7): _article_url(), professional_media_contents(), ProfessionalMediaError, Any, 专业媒体最小入口。 只保留源码审计确认的三个供应商及其当前能力：人人都是产品经理支持 公开 RSS 近期文章，雪球和 36 氪只支持用户粘贴 HTTPS…, 专业媒体输入或公开 RSS 响应不符合边界。, ValueError

## Knowledge Gaps
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ValidationError` connect `Community 11` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`?**
  _High betweenness centrality (0.262) - this node is a cross-community bridge._
- **Why does `TopicCenterService` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 5`, `Community 8`, `Community 11`?**
  _High betweenness centrality (0.157) - this node is a cross-community bridge._
- **Why does `ConfigService` connect `Community 0` to `Community 3`, `Community 11`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.154) - this node is a cross-community bridge._
- **Are the 6 inferred relationships involving `ValidationError` (e.g. with `UnifiedApiTransport` and `ConfigService`) actually correct?**
  _`ValidationError` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `TopicCenterService` (e.g. with `TopicMigrationHandler` and `TopicMigrationHTTPServer`) actually correct?**
  _`TopicCenterService` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `ConfigService` (e.g. with `UnifiedApiTransport` and `PersistenceError`) actually correct?**
  _`ConfigService` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `TopicGovernanceStore` (e.g. with `NotFoundError` and `PersistenceError`) actually correct?**
  _`TopicGovernanceStore` has 4 INFERRED edges - model-reasoned connections that need verification._