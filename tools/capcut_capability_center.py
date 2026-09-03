"""剪映小助手能力中心：只读展示 CapCut Mate 实际可查询的素材与可写能力。

运行：python tools\\capcut_capability_center.py
打开：http://127.0.0.1:8766

这个页面不会修改剪映草稿，也不会下载或写入素材；它只代理本机 CapCut Mate
的“获取目录”接口，并将“目录可见”“项目可写”“Windows 草稿已验收”严格区分。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from local_service_governance import SERVICE_SPECS, ServiceGovernanceError, acquire_server_lease, runtime_identity


HOST = "127.0.0.1"
PORT = 8766
SERVICE_SPEC = SERVICE_SPECS["capcut_capability_center"]
SERVICE_IDENTITY = runtime_identity(SERVICE_SPEC)
BASE_URL = os.environ.get("CAPCUT_MATE_BASE_URL", "http://127.0.0.1:30000").rstrip("/")
API_PREFIX = "/openapi/capcut-mate/v1"
CACHE_TTL_SECONDS = 120
CACHE: dict[str, object] = {"loaded_at": 0.0, "payload": None}


SAFE_REGISTRY = {
    "transitions": [
        {
            "name": "漫画撕纸",
            "kind": "视频转场",
            "status": "conditional",
            "note": "原 Coze AIGC 分支真实输入证据；通过 video_infos.transition 写入，时长单位为微秒。",
        },
        {
            "name": "胶片定格",
            "kind": "视频转场",
            "status": "conditional",
            "note": "原 Coze 数字人分支真实输入证据；通过 video_infos.transition 写入，时长单位为微秒。",
        },
    ],
    "effects": [
        {
            "name": "暗角",
            "kind": "画面特效",
            "status": "sdk_verified_native_pending",
            "note": "CapCut Mate 写入路由曾真实通过；尚未重新通过当前 Windows 原生草稿稳定性验收。",
        },
        {
            "name": "蓝色丝印",
            "kind": "画面特效",
            "status": "sdk_verified_native_pending",
            "note": "CapCut Mate 写入路由曾真实通过；尚未重新通过当前 Windows 原生草稿稳定性验收。",
        },
    ],
    "keyframes": [
        {"name": "位置 X / Y", "api": "KFTypePositionX / KFTypePositionY", "status": "sdk_verified"},
        {"name": "缩放 X / Y / 统一缩放", "api": "KFTypeScaleX / KFTypeScaleY / UNIFORM_SCALE", "status": "sdk_verified"},
        {"name": "旋转", "api": "KFTypeRotation", "status": "catalog_supported"},
        {"name": "透明度", "api": "KFTypeAlpha", "status": "catalog_supported"},
        {"name": "饱和度 / 对比度 / 亮度", "api": "KFTypeSaturation / KFTypeContrast / KFTypeBrightness", "status": "catalog_supported"},
        {"name": "音量", "api": "KFTypeVolume", "status": "catalog_supported"},
    ],
    "blocked": [
        {"name": "文字动画写入", "status": "blocked", "note": "项目有 get_text_animations 目录接口，但 add_captions 没有动画写入字段。"},
        {"name": "图片动画写入", "status": "blocked", "note": "项目有 get_image_animations 目录接口，但 add_images 没有动画写入字段。"},
        {"name": "转场音效", "status": "blocked", "note": "工作流节点 179757 的真实源码/字段映射缺失，当前不能执行。"},
        {"name": "人声增强", "status": "blocked", "note": "曾导致 CapCut Mate 产物异常；当前默认禁用，待单独验收。"},
        {"name": "字体目录", "status": "catalog_only", "note": "add_captions 可传 font 名称，但项目没有 get_fonts 接口；无法把客户端字体误报为小助手可用字体。"},
        {"name": "蒙版目录", "status": "catalog_only", "note": "项目有 add_masks 写入接口，但没有 get_masks 枚举接口；需先补目录接口或逐项实测。"},
        {"name": "贴纸目录", "status": "catalog_only", "note": "项目有 search_sticker 与 add_sticker，但没有全量列表接口；应按关键词检索后再入白名单。"},
    ],
}


def _post(endpoint: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"{BASE_URL}{API_PREFIX}/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _catalog_item(item: dict[str, object], category: str, write_status: str, write_note: str) -> dict[str, object]:
    return {
        "category": category,
        "name": item.get("name") or item.get("title") or "未命名",
        "id": item.get("effect_id") or item.get("id") or item.get("resource_id") or "",
        "resource_id": item.get("resource_id") or "",
        "vip": bool(item.get("is_vip", False)),
        "icon_url": item.get("icon_url") or "",
        "duration_us": item.get("duration"),
        "animation_type": item.get("type"),
        "write_status": write_status,
        "write_note": write_note,
    }


def load_catalog(force: bool = False) -> dict[str, object]:
    now = time.time()
    cached = CACHE.get("payload")
    if not force and cached and now - float(CACHE["loaded_at"]) < CACHE_TTL_SECONDS:
        return cached  # type: ignore[return-value]

    jobs = [
        ("画面特效", "get_effects", {"mode": 0}, "effects", "sdk_verified_native_pending", "目录可写入 add_effects；仅白名单已做 SDK 真实写入。"),
        ("花字效果", "get_text_effects", {"mode": 0}, "text_effects", "catalog_supported", "可通过 add_captions.text_effect 传名称或 ID；未做本地草稿验收。"),
        ("文字入场动画", "get_text_animations", {"mode": 0, "type": "in"}, "effects", "catalog_only", "项目目前仅能列出，缺少字幕动画写入字段。"),
        ("文字出场动画", "get_text_animations", {"mode": 0, "type": "out"}, "effects", "catalog_only", "项目目前仅能列出，缺少字幕动画写入字段。"),
        ("文字循环动画", "get_text_animations", {"mode": 0, "type": "loop"}, "effects", "catalog_only", "项目目前仅能列出，缺少字幕动画写入字段。"),
        ("图片入场动画", "get_image_animations", {"mode": 0, "type": "in"}, "effects", "catalog_only", "项目目前仅能列出，缺少图片动画写入字段。"),
        ("图片出场动画", "get_image_animations", {"mode": 0, "type": "out"}, "effects", "catalog_only", "项目目前仅能列出，缺少图片动画写入字段。"),
        ("图片循环动画", "get_image_animations", {"mode": 0, "type": "loop"}, "effects", "catalog_only", "项目目前仅能列出，缺少图片动画写入字段。"),
        ("滤镜", "get_filters", {"mode": 0}, "filters", "catalog_supported", "目录可写入 add_filters；尚未做本地草稿验收。"),
    ]
    items: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for category, endpoint, body, response_key, status, note in jobs:
        try:
            response = _post(endpoint, body)
            for value in response.get(response_key, []):
                if isinstance(value, dict):
                    items.append(_catalog_item(value, category, status, note))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append({"category": category, "error": str(exc)})
    payload = {
        "loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": BASE_URL,
        "items": items,
        "registry": SAFE_REGISTRY,
        "errors": errors,
    }
    CACHE.update({"loaded_at": now, "payload": payload})
    return payload


PAGE = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>剪映小助手能力中心</title>
<style>
:root{--bg:#07111f;--panel:#101d30;--line:#24364f;--text:#eaf2ff;--muted:#94a9c7;--blue:#4da3ff;--green:#38d39f;--yellow:#ffc45c;--red:#ff8080}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Microsoft YaHei",system-ui}.top{padding:22px max(22px,calc((100% - 1420px)/2));background:linear-gradient(120deg,#152d52,#0c1728);border-bottom:1px solid var(--line)}h1{margin:0 0 6px;font-size:25px}.sub{color:var(--muted);max-width:960px}.wrap{max-width:1420px;margin:20px auto;padding:0 20px}.notice{padding:12px 14px;background:#14243a;border:1px solid #28517b;border-radius:10px;color:#cfe5ff}.controls{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.controls input,.controls select,.controls button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.controls input{min-width:260px;flex:1}.controls button{cursor:pointer}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.stat,.rule{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px}.stat b{font-size:22px;display:block;color:var(--blue)}h2{margin:28px 0 10px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}.card{min-height:262px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:9px;cursor:pointer}.card:hover{border-color:var(--blue);transform:translateY(-1px)}.preview{height:112px;position:relative;overflow:hidden;border-radius:7px;background:linear-gradient(135deg,#213d63,#101b30)}.preview img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.58}.preview:after{content:"";position:absolute;inset:0;background:linear-gradient(120deg,#07111f55,transparent 45%,#07111f55)}.preview .sample{position:absolute;z-index:1;left:12px;right:12px;top:39px;text-align:center;font-size:20px;font-weight:800;letter-spacing:2px;text-shadow:0 2px 6px #000}.preview.effect .sample{animation:glow 1.8s ease-in-out infinite}.preview.text-effect .sample{background:linear-gradient(90deg,#fff,#ffd365,#ff6fb0,#fff);background-size:230% 100%;background-clip:text;color:transparent;animation:shimmer 2s linear infinite}.preview.filter:before{content:"";position:absolute;z-index:1;inset:0;background:linear-gradient(135deg,#ffbe57aa,#7c3e9faa);mix-blend-mode:color}.preview.motion-in .sample{animation:enter 1.8s ease-in-out infinite}.preview.motion-out .sample{animation:leave 1.8s ease-in-out infinite}.preview.motion-loop .sample{animation:pulse 1.35s ease-in-out infinite}.preview .hint{position:absolute;z-index:2;right:7px;bottom:6px;font-size:10px;color:#dfeaff;background:#081221b8;border-radius:4px;padding:2px 5px}.body{min-width:0}.name{font-weight:700;word-break:break-word}.meta,.note{color:var(--muted);font-size:12px;margin-top:5px}.badge{display:inline-block;padding:2px 6px;border-radius:999px;font-size:11px;margin:5px 4px 0 0}.vip{background:#553c0c;color:#ffe29b}.free{background:#123e35;color:#98f5d2}.safe{background:#123c35;color:#9cf5d4}.pending{background:#52370c;color:#ffe09b}.catalog{background:#23344f;color:#cfe1ff}.blocked{background:#4c2028;color:#ffb2bc}.modal{position:fixed;inset:0;background:#0009;display:none;align-items:center;justify-content:center;padding:20px}.modal.show{display:flex}.dialog{max-width:640px;width:100%;background:#102038;border:1px solid var(--line);border-radius:12px;padding:20px}.dialog pre{white-space:pre-wrap;word-break:break-word;background:#07111f;padding:12px;border-radius:8px;max-height:40vh;overflow:auto}.close{float:right}.footer{padding:24px 0;color:var(--muted)}.modal-preview{height:210px;margin:10px 0;border-radius:9px}.modal-preview .sample{top:80px;font-size:34px}@keyframes glow{0%,100%{filter:drop-shadow(0 0 0 #fff);transform:scale(1)}50%{filter:drop-shadow(0 0 10px #fff);transform:scale(1.05)}}@keyframes shimmer{to{background-position:230% 0}}@keyframes enter{0%,18%{opacity:0;transform:translateX(-115%)}45%,78%{opacity:1;transform:translateX(0)}100%{opacity:0;transform:translateX(115%)}}@keyframes leave{0%,55%{opacity:1;transform:translateX(0)}100%{opacity:0;transform:translateX(115%)}}@keyframes pulse{50%{transform:scale(1.13);letter-spacing:5px}}
</style><body><header class="top"><h1>剪映小助手能力中心</h1><div class="sub">数据来自本机 CapCut Mate 项目的实时目录接口。目录存在不等于可安全写入当前 Windows 草稿；页面已明确分级。</div></header><main class="wrap"><div class="notice">当前草稿采用“无转场、无特效”稳定策略。请仅从 <b>SDK 已验证</b> 或完成后续 <b>Windows 原生草稿验收</b> 的白名单中启用。</div><section id="stats" class="stats"></section><section class="controls"><input id="q" placeholder="搜索名称，例如：暗角、漫画、向上滑动"><select id="category"><option value="">全部分类</option></select><select id="level"><option value="">全部状态</option><option value="sdk_verified_native_pending">SDK 已验证，原生待验收</option><option value="catalog_supported">目录支持</option><option value="catalog_only">仅目录可见</option></select><select id="vip"><option value="">全部会员状态</option><option value="free">仅免费</option><option value="vip">仅 VIP</option></select><button id="refresh">刷新目录</button></section><div id="state" class="sub"></div><section><h2>素材目录</h2><div id="cards" class="grid"></div><div class="controls"><button id="more">加载更多</button></div></section><section><h2>项目安全白名单与阻断项</h2><div id="rules" class="grid"></div></section><p class="footer">缩略图仅代表官方素材外观，不是已套用到你的视频后的渲染预览。点击卡片可查看项目 ID、状态及写入条件。</p></main><div id="modal" class="modal"><div class="dialog"><button class="close" id="close">关闭</button><h2 id="mt"></h2><pre id="md"></pre></div></div><script>
let catalog=[],shown=0,limit=60,data={};const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
function demoClass(i){let c=String(i.category||''),t=String(i.animation_type||'');if(c.includes('\u52a8\u753b'))return `motion-${t==='in'?'in':t==='out'?'out':'loop'}`;if(c.includes('\u6ee4\u955c'))return 'filter';if(c.includes('\u82b1\u5b57'))return 'text-effect';return 'effect'}
function visual(i,extra=''){let klass=demoClass(i);return `<div class="preview ${klass} ${extra}"><img src="${esc(i.icon_url||'')}" onerror="this.style.visibility='hidden'"><div class="sample">${esc(i.name||'效果')}</div><span class="hint">图形示意</span></div>`}
function badge(i){let s=i.write_status||i.status||'';let cls=s==='sdk_verified_native_pending'||s==='sdk_verified'?'safe':s==='catalog_supported'?'pending':s==='catalog_only'||s==='conditional'?'catalog':'blocked';let label={'sdk_verified_native_pending':'SDK 已验证，原生待验收','sdk_verified':'SDK 已验证','catalog_supported':'目录支持','catalog_only':'仅目录可见','conditional':'条件支持','blocked':'当前阻断'}[s]||s;return `<span class="badge ${cls}">${esc(label)}</span>`}
function filtered(){let q=$('#q').value.trim().toLowerCase(),c=$('#category').value,l=$('#level').value,v=$('#vip').value;return catalog.filter(i=>(!q||String(i.name).toLowerCase().includes(q))&&(!c||i.category===c)&&(!l||i.write_status===l)&&(!v||(v==='vip'?i.vip:!i.vip)))}
function render(){let a=filtered();shown=Math.min(shown||limit,a.length);$('#cards').innerHTML=a.slice(0,shown).map((i,n)=>`<article class="card" data-i="${catalog.indexOf(i)}">${visual(i)}<div class="body"><div class="name">${esc(i.name)}</div><div class="meta">${esc(i.category)}</div>${i.vip?'<span class="badge vip">VIP</span>':'<span class="badge free">免费</span>'}${badge(i)}<div class="note">${esc(i.write_note||'')}</div></div></article>`).join('')||'<p class="sub">没有匹配条目。</p>';$('#more').style.display=shown<a.length?'inline-block':'none';document.querySelectorAll('.card').forEach(x=>x.onclick=()=>show(catalog[+x.dataset.i]));$('#state').textContent=`当前显示 ${shown}/${a.length} 条；目录加载于 ${data.loaded_at||'-'}；来源：${data.source||'-'}`}
function show(i){$('#mt').textContent=i.name;let pv=document.getElementById('mv');if(!pv){pv=document.createElement('div');pv.id='mv';$('#mt').after(pv)}pv.innerHTML=visual(i,'modal-preview');$('#md').textContent=JSON.stringify(i,null,2);$('#modal').classList.add('show')}
function rules(){let r=data.registry||{};let all=[...(r.transitions||[]),...(r.effects||[]),...(r.keyframes||[]),...(r.blocked||[])];$('#rules').innerHTML=all.map(i=>`<article class="rule"><div class="name">${esc(i.name)}</div>${badge(i)}<div class="meta">${esc(i.kind||i.api||'')}</div><div class="note">${esc(i.note||'')}</div></article>`).join('')}
async function load(force=false){$('#state').textContent='正在读取剪映小助手目录…';let r=await fetch('/api/catalog'+(force?'?refresh=1':'')).then(x=>x.json());data=r;catalog=r.items||[];shown=limit;let cats=[...new Set(catalog.map(x=>x.category))];$('#category').innerHTML='<option value="">全部分类</option>'+cats.map(x=>`<option>${esc(x)}</option>`).join('');let counts=cats.map(c=>[c,catalog.filter(i=>i.category===c).length]);$('#stats').innerHTML=counts.map(([k,v])=>`<div class="stat"><b>${v}</b>${esc(k)}</div>`).join('');rules();render()}
['q','category','level','vip'].forEach(id=>$('#'+id).addEventListener('input',()=>{shown=limit;render()}));$('#more').onclick=()=>{shown+=limit;render()};$('#refresh').onclick=()=>load(true);$('#close').onclick=()=>$('#modal').classList.remove('show');$('#modal').onclick=e=>{if(e.target.id==='modal')$('#modal').classList.remove('show')};load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/service/health":
            self._json(dict(SERVICE_IDENTITY))
            return
        if path == "/api/catalog":
            try:
                self._json(load_catalog(force="refresh=1" in self.path))
            except Exception as exc:  # show a useful state in the page instead of stopping the server
                self._json({"items": [], "registry": SAFE_REGISTRY, "errors": [{"category": "服务", "error": str(exc)}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode("utf-8"))


if __name__ == "__main__":
    lease = None
    server = None
    try:
        lease = acquire_server_lease(SERVICE_SPEC, SERVICE_IDENTITY)
        print(f"剪映小助手能力中心：http://{HOST}:{PORT}")
        print(f"目录来源：{BASE_URL}{API_PREFIX}")
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        server.serve_forever()
    except ServiceGovernanceError as exc:
        print(f"服务单实例治理阻止启动：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except OSError as exc:
        print(f"服务绑定 {HOST}:{PORT} 失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        if server is not None:
            server.server_close()
        if lease is not None:
            lease.release()
