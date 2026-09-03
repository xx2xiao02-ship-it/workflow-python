# 选题中心旧链路删除预案（待确认）

生成时间：2026-08-27

## 当前闸门

- 迁移报告：`reports/topic-center-migration-dry-run.json`
- V2 选题：8 条
- V2 来源：7 条
- 历史库唯一选题：8 条
- 缺失：0 条；重复冲突：0 条
- 哈希差异：1 条：`topic-trendradar-e1a4e7048e33e2ca4f46`
- 差异字段：`platform` 为旧值 `article`，V2 当前值为 `toutiao`；其余投影字段一致
- `safe_to_delete=false`，在差异处理和真实 TikHub/端到端验证完成前不得删除

## 精确删除清单（用户确认后执行）

### 文件和目录

- `tools/horizon_topic_bridge.py`
- `tools/mainland_hot_adapter.py`
- `tools/douyin_hot_adapter.py`
- `start_horizon_topic_bridge.cmd`
- `tmp/Horizon/`
- `outputs/horizon-bridge-8791.err.log`
- `outputs/horizon-bridge-8791.out.log`
- `outputs/horizon_test/`
- `ui-drafts/topic-center-new.html`
- `ui-drafts/tikhub-data-preview.html`
- `ui-drafts/topic-writing-governance.html`
- `ui-drafts/topic-intelligence-center-preview.html`
- `tests/test_douyin_hot_adapter.py`
- `tests/test_mainland_hot_adapter.py`

### 代码入口和旧数据

- `tools/video_production_console.py` 中旧 Horizon 内联页面、旧页面路由、旧 `/api/topic-writing-governance/*`、旧候选/queue 兼容接口及 queue/archive/decision 双写逻辑
- `outputs/topic_center/selection_archive.json`
- `outputs/topic_center/collection_queue.json`
- `outputs/topic_center/collection_results.json`
- `outputs/topic_center/topic_decisions.json`
- 仅在 `topic_selection_contract.py` 的剩余生产调用迁移并通过回归后删除该文件及对应废弃测试

## 删除影响

- 旧页面和旧接口返回404，不再保留别名、跳转或隐藏回退。
- 保留 `/topic-center`、`/writing-governance`、V2 `governance.json`、`source_content_id` 契约、TrendRadar、编导、TTS、视频生产和剪映链路。
- 不删除 `external/TrendRadar` 或其他无关历史代码。

## 恢复方式

1. 删除前将以上文件、目录和旧JSON复制到带时间戳的本地备份目录，并记录SHA256。
2. 删除后执行专项测试、全量测试和服务启动核验。
3. 若验收失败，停止服务，从备份目录恢复原路径，重新启动 `video_production_console`；V2 数据不回滚，除非发现V2写入损坏。

## 尚需用户确认

1. 允许使用已配置的 `TIKHUB_API_TOKEN` 发起首轮真实查询（预计4次；提供公众号 `ghid` 时最多5次，费用以TikHub账户实际计费为准）。
2. 接受或修正 `article` → `toutiao` 的单字段迁移差异。
3. 在上述条件完成后，明确确认执行本清单的不可逆删除。
