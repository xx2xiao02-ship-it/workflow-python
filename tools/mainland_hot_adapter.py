"""中文热点榜单适配器。

本模块只负责读取公开榜单并转换为选题中心的统一候选字段，不下载正文，
不读取登录 Cookie，也不把热榜摘要伪造成可直接用于文案创作的文章正文。

当前接入：微博热搜、今日头条热榜、百度热搜、B 站热门。各来源独立失败，
不会阻断其它来源；上层桥接器负责缓存、意图筛选和去重。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import quote, urljoin

import httpx

try:  # certifi 让 Windows 本地证书链与 Python HTTP 客户端保持一致。
    import certifi
except ModuleNotFoundError:  # pragma: no cover - 运行时通常由 httpx 间接提供
    certifi = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:  # pragma: no cover - JSON 来源仍可独立测试
    BeautifulSoup = None  # type: ignore[assignment,misc]


MAINLAND_HOT_SOURCE_LABELS = {
    "weibo_hot": "微博热搜",
    "toutiao_hot": "今日头条热榜",
    "baidu_hot": "百度热搜",
    "bilibili_hot": "B站热门",
}

MAINLAND_HOT_ENDPOINTS = {
    "weibo_hot": "https://weibo.com/ajax/side/hotSearch?type=60",
    "toutiao_hot": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
    "baidu_hot": "https://top.baidu.com/board?platform=pc&tab=realtime",
    "bilibili_hot": "https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1",
}

_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.7",
}
_REFERERS = {
    "weibo_hot": "https://weibo.com/",
    "toutiao_hot": "https://www.toutiao.com/",
    "baidu_hot": "https://top.baidu.com/",
    "bilibili_hot": "https://www.bilibili.com/",
}


class MainlandHotSourceError(RuntimeError):
    """公开中文热点源不可用或返回格式不符合预期。"""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _source_score(rank: int, limit: int) -> int:
    if rank <= 0:
        return 50
    return max(20, min(100, 100 - round((rank - 1) * 80 / max(1, limit - 1))))


def _category(title: str, fallback: str = "综合热点") -> str:
    text = str(title or "").lower()
    groups = {
        "商业财经": ("股票", "基金", "经济", "银行", "公司", "裁员", "消费", "金融"),
        "科技趋势": ("人工智能", "ai", "芯片", "机器人", "科技", "模型", "算法", "手机"),
        "社会观察": ("法院", "警方", "事故", "台风", "地震", "民生", "政策", "交通"),
        "女性成长": ("女性", "女生", "职场", "婚姻", "育儿", "成长"),
        "故事评论": ("争议", "回应", "真相", "内幕", "评论"),
    }
    for name, keywords in groups.items():
        if any(keyword in text for keyword in keywords):
            return name
    return fallback


def _item(
    *,
    source_key: str,
    title: str,
    url: str,
    rank: int,
    hot_value: int,
    content: str,
    category: str,
    captured_at: str,
    author: str = "",
) -> dict[str, Any]:
    label = MAINLAND_HOT_SOURCE_LABELS[source_key]
    title = html.unescape(str(title or "").strip())
    url = str(url or "").strip()
    identity = f"{source_key}:{title}:{url}"
    return {
        "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16],
        "source_type": source_key,
        "source": label,
        "title": title,
        "url": url,
        "content": "；".join(
            part
            for part in (
                f"榜单：{label}",
                f"排名：{rank}" if rank else "",
                f"热度：{hot_value}" if hot_value else "",
                f"分类：{category}" if category else "",
                f"作者：{author}" if author else "",
                str(content or "").strip(),
            )
            if part
        ),
        "published_at": captured_at,
        "profile": "mainland-hot",
        "metadata": {
            "feed_name": label,
            "source_name": label,
            "category": category,
            "board_type": source_key,
            "rank": rank,
            "hot_value": hot_value,
            "author": author,
            "source_score": _source_score(rank, 50),
            "captured_at": captured_at,
            "collector_route": source_key,
        },
    }


def normalize_weibo_payload(payload: Mapping[str, Any], *, limit: int = 50, captured_at: str = "") -> list[dict[str, Any]]:
    rows = payload.get("data", {}).get("realtime", []) if isinstance(payload.get("data"), Mapping) else []
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("word") or row.get("note") or "").strip()
        if not title:
            continue
        rank = _safe_int(row.get("realpos") or row.get("rank"), index)
        output.append(
            _item(
                source_key="weibo_hot",
                title=title,
                url="https://s.weibo.com/weibo?q=" + quote(title),
                rank=rank,
                hot_value=_safe_int(row.get("num")),
                content=str(row.get("note") or ""),
                category=_category(title),
                captured_at=captured_at,
            )
        )
    return output


def normalize_toutiao_payload(payload: Mapping[str, Any], *, limit: int = 50, captured_at: str = "") -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("Title") or row.get("title") or "").strip()
        if not title:
            continue
        url = str(row.get("Url") or row.get("url") or "").strip()
        output.append(
            _item(
                source_key="toutiao_hot",
                title=title,
                url=url or "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
                rank=index,
                hot_value=_safe_int(row.get("HotValue") or row.get("hot_value")),
                content=str(row.get("Label") or ""),
                category=_category(title),
                captured_at=captured_at,
            )
        )
    return output


def normalize_bilibili_payload(payload: Mapping[str, Any], *, limit: int = 50, captured_at: str = "") -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    rows = data.get("list") if isinstance(data.get("list"), list) else []
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:limit], start=1):
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        bvid = str(row.get("bvid") or "").strip()
        url = str(row.get("short_link_v2") or "").strip() or (
            "https://www.bilibili.com/video/" + bvid if bvid else "https://www.bilibili.com/v/popular/ranking/"
        )
        stat = row.get("stat") if isinstance(row.get("stat"), Mapping) else {}
        output.append(
            _item(
                source_key="bilibili_hot",
                title=title,
                url=url,
                rank=index,
                hot_value=_safe_int(stat.get("view")),
                content=str(row.get("desc") or ""),
                category=_category(title, str(row.get("tname") or "综合热点")),
                captured_at=captured_at,
                author=str((row.get("owner") or {}).get("name") or "") if isinstance(row.get("owner"), Mapping) else "",
            )
        )
    return output


def normalize_baidu_html(markup: str, *, limit: int = 50, captured_at: str = "") -> list[dict[str, Any]]:
    if BeautifulSoup is None:
        raise MainlandHotSourceError("百度热搜解析需要 beautifulsoup4")
    soup = BeautifulSoup(str(markup or ""), "html.parser")
    cards = soup.select(".category-wrap_iQLoo")
    output: list[dict[str, Any]] = []
    for index, card in enumerate(cards[:limit], start=1):
        title_node = card.select_one(".title_dIF3B .c-single-text-ellipsis") or card.select_one(".title_dIF3B")
        link = card.select_one("a.title_dIF3B") or card.select_one("a")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title:
            continue
        href = str(link.get("href") or "").strip() if link else ""
        output.append(
            _item(
                source_key="baidu_hot",
                title=title,
                url=urljoin("https://top.baidu.com/board?platform=pc&tab=realtime", href),
                rank=index,
                hot_value=_safe_int((card.select_one(".hot-index_1Bl1a") or {}).get_text(" ", strip=True) if card.select_one(".hot-index_1Bl1a") else 0),
                content=(card.select_one(".hot-desc_1m_jR") or card).get_text(" ", strip=True)[:300],
                category=_category(title),
                captured_at=captured_at,
            )
        )
    return output


async def _fetch_source(client: httpx.AsyncClient, source_key: str, *, limit: int, captured_at: str) -> dict[str, Any]:
    url = MAINLAND_HOT_ENDPOINTS[source_key]
    headers = {**_HEADERS, "referer": _REFERERS[source_key]}
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        if source_key == "baidu_hot":
            items = normalize_baidu_html(response.text, limit=limit, captured_at=captured_at)
        else:
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise MainlandHotSourceError("返回格式不是对象")
            if source_key == "weibo_hot":
                items = normalize_weibo_payload(payload, limit=limit, captured_at=captured_at)
            elif source_key == "toutiao_hot":
                items = normalize_toutiao_payload(payload, limit=limit, captured_at=captured_at)
            else:
                items = normalize_bilibili_payload(payload, limit=limit, captured_at=captured_at)
        return {"source": source_key, "status": "ready" if items else "empty", "count": len(items), "items": items}
    except Exception as exc:  # each source is isolated from the others
        return {
            "source": source_key,
            "status": "unavailable",
            "count": 0,
            "items": [],
            "message": f"{type(exc).__name__}: {str(exc)[:180]}",
        }


async def fetch_mainland_hot_sources(
    sources: list[str] | tuple[str, ...] | None = None,
    *,
    limit: int = 50,
    timeout: float = 20.0,
) -> dict[str, Any]:
    selected = [
        str(source).strip().lower()
        for source in (sources or MAINLAND_HOT_SOURCE_LABELS)
        if str(source).strip().lower() in MAINLAND_HOT_SOURCE_LABELS
    ]
    selected = list(dict.fromkeys(selected))
    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    verify = certifi.where() if certifi is not None else True
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=verify) as client:
        results = await asyncio.gather(
            *(_fetch_source(client, source, limit=max(1, min(int(limit), 100)), captured_at=captured_at) for source in selected)
        )
    items = [item for result in results for item in result.get("items", [])]
    return {
        "status": "ready" if items else "empty",
        "captured_at": captured_at,
        "items": items,
        "sources": [{key: value for key, value in result.items() if key != "items"} for result in results],
        "message": "已读取中文热点榜单" if items else "中文热点榜单当前没有可用数据",
    }


__all__ = [
    "MAINLAND_HOT_ENDPOINTS",
    "MAINLAND_HOT_SOURCE_LABELS",
    "MainlandHotSourceError",
    "fetch_mainland_hot_sources",
    "normalize_baidu_html",
    "normalize_bilibili_payload",
    "normalize_toutiao_payload",
    "normalize_weibo_payload",
]
