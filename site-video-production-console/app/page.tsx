"use client";

import { useEffect, useState } from "react";

type PageKey = "writing" | "director" | "assets" | "editing";
type CollectionTask = { state: "idle" | "checking" | "queued" | "running" | "succeeded" | "failed"; stage: string; progress: number; message: string; task_id?: string };

const executorUrl = "http://127.0.0.1:8768";
const pages: Array<{ key: PageKey; number: string; title: string; note: string }> = [
  { key: "writing", number: "0", title: "文案创作", note: "抖音风格包、案例二创、人工确认" },
  { key: "director", number: "1", title: "编导审核", note: "故事、分镜与镜头时间线" },
  { key: "assets", number: "2", title: "素材生产", note: "角色、首帧、视频、语音与音乐" },
  { key: "editing", number: "3", title: "剪辑交付", note: "Windows 剪映草稿、字幕与轨道" },
];

function Tag({ children, tone = "blue" }: { children: React.ReactNode; tone?: "blue" | "green" | "gold" }) { return <span className={`tag ${tone}`}>{children}</span>; }

function TaskProgress({ task }: { task: CollectionTask }) {
  const stages = ["主页解析", "作品采集", "逐条转写", "风格蒸馏", "人工审核"];
  const current = task.stage === "creator_resolved" ? 0 : task.stage === "collecting" || task.stage === "collected" ? 1 : task.stage === "transcribing" || task.stage === "transcribed" ? 2 : task.stage === "distilling" ? 3 : 4;
  return <div className={`task ${task.state}`} aria-live="polite"><div className="task-head"><b>{task.state === "failed" ? "任务未启动" : task.state === "succeeded" ? "风格包已生成" : "采集任务状态"}</b><span>{task.progress}%</span></div><div className="progress"><i style={{ width: `${task.progress}%` }} /></div><p>{task.message}</p><ol className="task-stages">{stages.map((stage, index) => <li key={stage} className={index < current || task.state === "succeeded" ? "done" : index === current && task.state !== "idle" ? "active" : ""}>{stage}</li>)}</ol>{task.state === "failed" && <small>请先启动本机执行器；页面不会伪造采集成功或伪造进度。</small>}</div>;
}

function Writing() {
  useEffect(() => {
    void fetch(`${executorUrl}/api/style-tasks`)
      .then((response) => response.ok ? response.json() : null)
      .then((data: { tasks?: CollectionTask[] } | null) => {
        const latest = data?.tasks?.[0];
        if (!latest) return;
        setTask(latest);
        if (latest.task_id && (latest.state === "checking" || latest.state === "queued" || latest.state === "running")) void pollTask(latest.task_id);
      })
      .catch(() => undefined);
  }, []);
  const [creatorUrl, setCreatorUrl] = useState("");
  const [sampleTarget, setSampleTarget] = useState("50");
  const [task, setTask] = useState<CollectionTask>({ state: "idle", stage: "idle", progress: 0, message: "尚未创建采集任务" });
  async function pollTask(taskId: string) { for (let attempts = 0; attempts < 360; attempts += 1) { await new Promise((resolve) => window.setTimeout(resolve, 2000)); try { const response = await fetch(`${executorUrl}/api/style-tasks/${taskId}`); if (!response.ok) throw new Error("任务查询失败"); const next = await response.json() as CollectionTask; setTask(next); if (next.state === "succeeded" || next.state === "failed") return; } catch { setTask({ state: "failed", stage: "executor_disconnected", progress: 0, message: "采集过程中与本机执行器失去连接；任务未被标记为成功。" }); return; } } setTask({ state: "failed", stage: "poll_timeout", progress: 0, message: "任务状态超过 12 分钟未返回，请在本机执行器中检查日志。" }); }
  async function startCollection() { if (!/https:\/\/(?:www\.)?(?:v\.)?douyin\.com\//.test(creatorUrl.trim())) { setTask({ state: "failed", stage: "invalid_url", progress: 0, message: "请粘贴抖音分享文案、v.douyin.com 短链或 /user/ 主页链接。" }); return; } setTask({ state: "checking", stage: "executor_check", progress: 2, message: "正在检测本机执行器连接…" }); try { const health = await fetch(`${executorUrl}/api/executor/health`); if (!health.ok) throw new Error("执行器接口不可用"); setTask({ state: "queued", stage: "queued", progress: 3, message: "本机执行器已连接，正在从最新作品开始创建采集任务…" }); const response = await fetch(`${executorUrl}/api/style-tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ creator_home_url: creatorUrl.trim(), sample_target: Number(sampleTarget) }) }); if (!response.ok) throw new Error("创建任务失败"); const created = await response.json() as CollectionTask; setTask(created); if (created.task_id) void pollTask(created.task_id); } catch { window.location.href = "video-production-console://start"; setTask({ state: "checking", stage: "launcher_started", progress: 1, message: "已请求启动本机控制台，正在等待它就绪…" }); for (let attempt = 1; attempt <= 10; attempt += 1) { await new Promise((resolve) => window.setTimeout(resolve, 2000)); try { const health = await fetch(`${executorUrl}/api/executor/health`); if (health.ok) { void startCollection(); return; } } catch {} setTask({ state: "checking", stage: "launcher_started", progress: Math.min(18, attempt * 2), message: "本机控制台正在启动，请稍候…" }); } setTask({ state: "failed", stage: "launcher_timeout", progress: 0, message: "本机控制台 20 秒内未就绪。请检查是否允许浏览器打开“视频制作控制台”，或双击启动脚本。" }); } }
  return <><section className="intro"><Tag>在线控制台</Tag><h2>从风格参考，到可审核的二创文案</h2><p>先建立风格包，再提取案例视频文案；每一个关键产物都需要你确认后，才交给编导层。</p></section><div className="notice"><b>本机执行器：</b>抖音采集、Whisper 转写与风格蒸馏运行在你的电脑。可直接粘贴“长按复制此条消息”得到的整段分享文案，系统会先展开短链。</div><section className="card"><div className="card-head"><span className="step">01</span><div><h3>抖音达人主页 → 风格包</h3><p>支持完整分享文案、v.douyin.com 短链及 /user/ 主页链接；只提炼表达结构与节奏，不复制原句或人设。</p></div></div><label>抖音达人主页分享<input value={creatorUrl} onChange={(event) => setCreatorUrl(event.target.value)} placeholder="粘贴整段分享文案、短链或主页链接" /></label><label>采集梯度<select value={sampleTarget} onChange={(event) => setSampleTarget(event.target.value)}><option value="50">50 条有效样本（默认）</option><option value="30">30 条有效样本</option><option value="80">80 条有效样本</option></select><small>从最新作品开始采集；有效样本不足时自动向历史作品补抓，凑够目标立即停止。翻到底或最多扫描 100 条候选作品仍不足，才报告样本不足。</small></label><button onClick={startCollection} disabled={task.state === "checking" || task.state === "queued" || task.state === "running"}>开始采集并生成风格包</button>{task.state !== "idle" && <TaskProgress task={task} />}</section><section className="card"><div className="card-head"><span className="step">02</span><div><h3>案例视频 → 二创草案</h3><p>在风格包通过人工审核后，保留可追溯事实和观点，生成二创提纲与正文。</p></div></div><label>抖音案例视频链接<input placeholder="https://www.douyin.com/video/…" /></label><button disabled>待风格包审核后可用</button></section></>;
}

function Director() { return <><section className="intro"><Tag tone="green">编导层</Tag><h2>把获批文案变成可执行的故事与镜头</h2><p>编导层只接收 <code>approved_copy</code> 和 <code>style_profile_id</code>，避免未经确认的材料进入制作。</p></section><div className="grid"><section className="card"><h3>输入</h3><ul><li>已确认文案</li><li>画幅：9:16 / 16:9 / 1:1 / 3:2 / 2:3</li><li>音色、语速与情绪偏好</li></ul></section><section className="card"><h3>输出</h3><ul><li>完整默片故事与大分段桥段</li><li>3–6 秒优先的镜头坑位</li><li><code>shot_id / start_us / end_us</code> 时间线</li></ul></section></div></>; }
function Assets() { return <><section className="intro"><Tag tone="gold">素材层</Tag><h2>让故事规划变成可用素材清单</h2><p>用户在本层补充画风参考图、数字人形象图和可选 BGM；其余镜头需求由编导层传入。</p></section><div className="grid"><section className="card"><h3>用户可提供</h3><ul><li>多张画风参考图</li><li>多张数字人形象图</li><li>可选 BGM 与转场音效</li><li>首帧策略：单图 / 2×2 / 3×3</li></ul></section><section className="card"><h3>自动生产</h3><ul><li>主角三视图与角色一致性资产</li><li>全片电影海报式首帧</li><li>逐镜首帧与 AIGC 视频</li><li>TTS、BGM、数字人视频</li></ul></section></div></>; }
function Editing() { return <><section className="intro"><Tag tone="gold">剪辑层</Tag><h2>按镜头坑位生成 Windows 剪映草稿</h2><p>使用剪映小助手的 Windows 原生草稿创建方式，不使用 iOS 或 macOS 模板。</p></section><section className="timeline"><div className="track"><b>首帧参考轨</b><span>仅非数字人镜头，覆盖完整镜头坑位</span><em className="bluebar" /></div><div className="track"><b>AIGC / 数字人主视觉轨</b><span>视频按 <code>shot_id</code> 放入对应坑位</span><em className="bluebar" /></div><div className="track"><b>解说与 BGM 轨</b><span>TTS 与 BGM 独立混音</span><em className="greenbar" /></div><div className="track"><b>字幕轨</b><span>坑位内按语义切分，默认居中</span><em className="goldbar" /></div></section></>; }
export default function Home() { const [active, setActive] = useState<PageKey>("writing"); const content = active === "writing" ? <Writing /> : active === "director" ? <Director /> : active === "assets" ? <Assets /> : <Editing />; return <main className="shell"><header><div><p className="eyebrow">VIDEO PRODUCTION CONSOLE</p><h1>视频制作控制台</h1><p className="sub">文案创作 · 编导审核 · 素材生产 · 剪辑交付</p></div><span className="private">私有站点</span></header><div className="layout"><nav aria-label="制作流程">{pages.map((page) => <button key={page.key} onClick={() => setActive(page.key)} className={active === page.key ? "nav active" : "nav"}><span>{page.number}</span><strong>{page.title}</strong><small>{page.note}</small></button>)}</nav><article>{content}</article></div><footer>线上控制台只在本机执行器确认连接后才会创建真实任务。</footer></main>; }
