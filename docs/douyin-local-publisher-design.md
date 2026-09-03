# 抖音本地网页自动发布方案

## 1. 目标与边界

目标是让 Windows 本地电脑通过可见浏览器执行抖音创作者中心的视频上传和发布，现有 Web UI 继续作为查询系统和任务管理入口。

第一阶段采用半自动发布：浏览器自动打开、登录态复用、选择视频、填写标题和话题；发布按钮前进入 `waiting_user`，由用户确认后继续。连续验证稳定后，才允许切换为自动点击发布。

本方案不使用抖音内部接口、不上传 Cookie、不把浏览器用户目录提交到 Git，也不改变现有 `videos` 表字段含义。

## 2. 运行结构

```text
现有 FastAPI 查询 UI
    ├─ 创建/取消发布任务
    ├─ 查询队列、日志和结果
    └─ 不直接控制浏览器
             │ SQLite/WAL
             ▼
本地 publisher_service.py
    ├─ 单进程队列锁
    ├─ Playwright 可见浏览器
    ├─ 独立账号 profile
    └─ 状态、错误、截图回写
             │
             ▼
抖音创作者服务中心
```

发布服务和 Web 服务运行在同一台 Windows 电脑上，共享现有 SQLite 数据库。发布服务必须是单消费者，避免同一账号同时打开多个发布页。

## 3. 目录设计

```text
services/douyin-monitor/
├─ publisher_service.py       # 本地队列执行器，第一阶段新增
├─ publisher_browser.py       # Playwright 页面动作和选择器
├─ publisher_tasks.py         # 发布任务 CRUD、状态迁移、幂等控制
├─ publisher_data/            # 运行产物，不提交 Git
│  ├─ screenshots/
│  └─ logs/
└─ douyin_publish_profiles/   # 浏览器登录态，不提交 Git
   └─ account_default/
```

现有 `web/app.py` 只增加发布任务查询和队列 API；采集、评论和登录模块保持原职责。发布登录优先复用现有可见桌面登录能力，但发布 profile 与采集 profile 分开，避免互相锁定或污染。

## 4. 数据契约

新增 `publish_tasks` 表：

| 字段 | 说明 |
|---|---|
| `id` | 本地任务 ID |
| `video_db_id` | 可选，关联现有 `videos.id` |
| `account_key` | 本地账号 profile 名称，不保存账号密码 |
| `video_path` | Windows 本地 MP4 绝对路径 |
| `cover_path` | 可选本地封面路径 |
| `title` | 发布标题，包含或不包含话题由任务创建方决定 |
| `hashtags_json` | JSON 数组，保持输入顺序 |
| `publish_mode` | `manual_confirm` 或 `auto_confirm` |
| `status` | 当前状态机值 |
| `scheduled_at` | 可选，使用本机时区的 ISO 时间；内部统一保存带时区时间 |
| `attempt_count` | 尝试次数 |
| `platform_video_id` | 发布成功后回写 |
| `platform_url` | 发布成功后回写 |
| `last_error` | 脱敏后的失败原因 |
| `screenshot_path` | 失败或等待确认时的本地截图 |
| `created_at/updated_at` | ISO 时间 |

状态只允许按以下路径迁移：

```text
queued → opening → uploading → editing → waiting_user
waiting_user → publishing → success
queued/opening/uploading/editing/publishing → failed
failed → queued       # 仅显式重试
queued → cancelled    # 未开始执行时允许取消
```

不能通过重复点击或重复请求创建同一任务。幂等键使用 `video_path + account_key + scheduled_at` 的规范化组合；同一任务重试只增加 `attempt_count`。

## 5. 本地服务接口

第一版建议使用同机 HTTP，监听 `127.0.0.1`，不暴露局域网：

```text
GET  /api/publish/health
GET  /api/publish/tasks?status=...
GET  /api/publish/tasks/{id}
POST /api/publish/tasks
POST /api/publish/tasks/{id}/retry
POST /api/publish/tasks/{id}/cancel
POST /api/publish/tasks/{id}/confirm
```

`POST /api/publish/tasks` 只接受本地路径，并在入队前检查文件存在、扩展名为 `.mp4`、文件大小大于 0。服务端不接受远程 URL 作为上传文件来源。

`confirm` 只允许 `waiting_user` 状态使用，并校验浏览器仍在目标发布页面。确认后进入 `publishing`，不能由普通查询请求触发发布。

## 6. 浏览器自动化策略

使用 Playwright persistent context，`headless=False`，用户第一次在本机窗口中完成登录。每个账号使用独立 profile；不在服务端或日志中打印 Cookie。

页面动作必须按阶段保存证据：

1. 打开创作者中心并确认页面可用；
2. 选择本地 MP4，等待上传进度达到完成；
3. 填写标题和话题，检查文本仍存在；
4. 设置封面并截图；
5. 半自动模式暂停在发布按钮前；
6. 点击发布后等待成功提示或作品列表出现新作品；
7. 从页面提取作品 ID/链接，失败则截图并保留页面文本摘要。

选择器必须集中在 `publisher_browser.py`，不要散落在 API 路由里。页面元素找不到时立即进入 `failed`，不盲目重复点击。

## 7. UI 只增加查询能力

不做新的复杂工作台。现有视频详情页增加一个紧凑区域：

- “加入发布队列”；
- 标题和话题预览；
- 发布账号、计划时间、当前状态；
- 等待确认时显示“打开浏览器确认”；
- 成功后显示抖音链接；
- 失败后显示原因、截图和“重试”。

UI 不展示 Cookie、profile 路径或原始浏览器日志。

## 8. 第一阶段实施顺序

1. 新增数据库表、状态迁移函数和任务 CRUD。
2. 新增本地发布服务，但先实现 `health`、队列轮询、文件校验和 `failed` 记录。
3. 接入可见浏览器登录与上传页面，先做到 `waiting_user`。
4. 在 UI 增加任务查询、入队、取消、重试和确认接口。
5. 用一个无敏感内容的短 MP4 做真实本机验证：上传成功、页面暂停、确认发布、结果回写。
6. 连续至少 3 次成功后，再评估自动点击发布。

## 9. 验收标准

- UI 查询系统原有采集、评论和趋势功能回归通过。
- 本地发布服务只能监听 `127.0.0.1`，无 Cookie 外传。
- 同一账号同时只有一个发布任务进入浏览器。
- 上传失败、页面元素缺失、登录失效和发布失败都能进入 `failed`，并保留截图及脱敏错误。
- `success` 必须同时具备页面成功证据和回写的抖音作品链接或作品 ID；不能仅凭点击按钮判定成功。
- 时间统一使用 ISO 8601；定时发布时间明确为本机 Asia/Shanghai 时区，不能把毫秒时间戳与秒混用。
- 未完成真实抖音账号发布验证前，状态报告只能写“代码已实现/本地离线测试通过/真实发布未验证”。

## 10. 当前阻断与建议

当前已知需要真实验证的部分是抖音创作者中心页面结构、账号登录状态、上传限制和成功提示。它们不能通过离线测试替代。

建议先实现半自动模式并使用一个测试账号；不要在第一版直接启用多账号、定时发布和自动点击发布。
