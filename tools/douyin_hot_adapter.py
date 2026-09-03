"""抖音热点数据适配器。

这个模块只负责把 douyin-hot-hub 使用的抖音榜单接口转换为选题中心的
统一候选字段。它不负责下载视频、不读取登录 Cookie，也不把抖音正文伪造
成可供文案节点消费的文章内容。

网络接口和抓取频率由上层 Horizon bridge 控制；本模块可以独立做实时烟雾
测试，方便在不影响主工作链路的情况下替换或停用抖音信源。
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import quote

import httpx


DOUYIN_HEADERS = {"user-agent": "okhttp3", "accept": "application/json"}
DOUYIN_QUERIES = {
    "device_platform": "android",
    "version_name": "13.2.0",
    "version_code": "130200",
    "aid": "1128",
}

BOARD_LABELS = {
    "hot_search": "抖音热榜",
    "star": "明星榜",
    "live": "直播榜",
    "music": "音乐榜",
}

BOARD_ENDPOINTS = {
    "hot_search": "https://aweme.snssdk.com/aweme/v1/hot/search/list/",
    "star": "https://aweme.snssdk.com/aweme/v1/hotsearch/star/billboard/",
    "live": "https://webcast.amemv.com/webcast/ranklist/hot/",
    "music": "https://aweme.snssdk.com/aweme/v1/chart/music/list/",
}

_CATEGORY_KEYWORDS = {
    "知识解释": ("科普", "解释", "原理", "知识", "教育", "读懂"),
    "商业财经": ("财经", "公司", "企业", "股票", "基金", "经济", "银行", "商业", "裁员"),
    "科技趋势": ("人工智能", "ai", "机器人", "芯片", "科技", "模型", "算法", "手机", "汽车"),
    "社会观察": ("社会", "民生", "政策", "公共", "警方", "法院", "事故", "台风", "地震"),
    "女性成长": ("女性", "女生", "职场女性", "婚姻", "育儿", "成长"),
    "故事评论": ("故事", "评论", "争议", "回应", "真相", "内幕"),
    "热点快评": ("热搜", "最新", "突发", "登顶", "爆", "冲上"),
}


class DouyinHotSourceError(RuntimeError):
    """抖音热点源不可用或返回格式不符合预期。"""


async def _fetch_json(url: str, params: Mapping[str, str], *, timeout: float = 20.0) -> Mapping[str, Any]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=DOUYIN_HEADERS) as client:
        response = await client.get(url, params=dict(params))
        response.raise_for_status()
        try:
            value = response.json()
        except ValueError as exc:  # pragma: no cover - depends on external response
            raise DouyinHotSourceError("抖音接口返回的不是 JSON") from exc
    if not isinstance(value, Mapping):
        raise DouyinHotSourceError("抖音接口返回格式无效")
    return value


def classify_hot_title(title: str) -> str:
    """使用可审计的关键词规则给热点一个初始选题分类。"""
    text = str(title or "").strip().lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword.lower() in text for keyword in keywords):
            return category
    return "综合热点"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _source_score(rank: int, limit: int) -> int:
    if rank <= 0:
        return 50
    # 榜单排名评分只用于排序提示，不冒充模型评分。
    return max(20, min(100, 100 - round((rank - 1) * 80 / max(1, limit - 1))))


def _base_item(
    *,
    board: str,
    title: str,
    rank: int,
    url: str,
    hot_value: int = 0,
    category: str = "综合热点",
    author: str = "",
    limit: int = 50,
    captured_at: str,
) -> dict[str, Any]:
    title = str(title or "").strip()
    identity = f"douyin:{board}:{title}:{url}"
    source_score = _source_score(rank, limit)
    return {
        "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16],
        "source_type": "douyin_hot",
        "source": BOARD_LABELS.get(board, "抖音热点"),
        "title": title,
        "url": url,
        "content": "；".join(
            part
            for part in (
                f"榜单：{BOARD_LABELS.get(board, board)}",
                f"排名：{rank}" if rank else "",
                f"热度：{hot_value}" if hot_value else "",
                f"分类：{category}" if category else "",
                f"作者：{author}" if author else "",
            )
            if part
        ),
        "published_at": captured_at,
        "profile": "douyin-hot",
        "metadata": {
            "feed_name": BOARD_LABELS.get(board, "抖音热点"),
            "source_name": BOARD_LABELS.get(board, "抖音热点"),
            "category": category,
            "board_type": board,
            "rank": rank,
            "hot_value": hot_value,
            "author": author,
            "source_score": source_score,
            "captured_at": captured_at,
            "collector_route": "douyin_hot_search" if board == "hot_search" else "douyin_video",
        },
    }


def _normalize_items(board: str, payload: Mapping[str, Any], *, limit: int, captured_at: str) -> list[dict[str, Any]]:
    nested = payload.get("data") if isinstance(payload.get("data"), Mapping) else None
    data = nested if nested else payload
    rows: list[Mapping[str, Any]] = []
    if board == "hot_search":
        rows = [item for item in (data.get("word_list") or []) if isinstance(item, Mapping)]
        output = []
        for index, item in enumerate(rows[:limit], start=1):
            title = str(item.get("word") or "").strip()
            if not title:
                continue
            rank = _safe_int(item.get("position"), index)
            output.append(
                _base_item(
                    board=board,
                    title=title,
                    rank=rank,
                    url="https://www.douyin.com/search/" + quote(title),
                    hot_value=_safe_int(item.get("hot_value")),
                    category=classify_hot_title(title),
                    limit=limit,
                    captured_at=captured_at,
                )
            )
        return output
    if board == "star":
        rows = [item for item in (data.get("user_list") or []) if isinstance(item, Mapping)]
        output = []
        for index, item in enumerate(rows[:limit], start=1):
            user = item.get("user_info") if isinstance(item.get("user_info"), Mapping) else {}
            title = str(user.get("nickname") or "").strip()
            if not title:
                continue
            uid = str(user.get("uid") or "").strip()
            sec_uid = str(user.get("sec_uid") or "").strip()
            url = "https://www.iesdouyin.com/share/user/" + uid
            if sec_uid:
                url += "?sec_uid=" + quote(sec_uid)
            output.append(
                _base_item(
                    board=board,
                    title=title,
                    rank=_safe_int(item.get("rank"), index),
                    url=url,
                    hot_value=_safe_int(item.get("hot_value")),
                    category="人物热点",
                    limit=limit,
                    captured_at=captured_at,
                )
            )
        return output
    if board == "live":
        rows = [item for item in (data.get("ranks") or []) if isinstance(item, Mapping)]
        output = []
        for index, item in enumerate(rows[:limit], start=1):
            user = item.get("user") if isinstance(item.get("user"), Mapping) else {}
            room = item.get("room") if isinstance(item.get("room"), Mapping) else {}
            title = str(room.get("title") or "看直播").strip()
            room_id = str(room.get("id") or "").strip()
            if not room_id:
                continue
            output.append(
                _base_item(
                    board=board,
                    title=title,
                    rank=_safe_int(item.get("rank"), index),
                    url="https://webcast.amemv.com/webcast/reflow/" + room_id,
                    hot_value=_safe_int(item.get("score")),
                    category=str(item.get("label") or "直播热点"),
                    author=str(user.get("nickname") or "").strip(),
                    limit=limit,
                    captured_at=captured_at,
                )
            )
        return output
    if board == "music":
        rows = [item for item in (data.get("music_list") or []) if isinstance(item, Mapping)]
        output = []
        for index, item in enumerate(rows[:limit], start=1):
            info = item.get("music_info") if isinstance(item.get("music_info"), Mapping) else {}
            title = str(info.get("title") or item.get("title") or "").strip()
            if not title:
                continue
            play_url = info.get("play_url") if isinstance(info.get("play_url"), Mapping) else {}
            url = str(play_url.get("uri") or "").strip() or "https://www.douyin.com/music/" + str(info.get("id") or item.get("id") or "")
            output.append(
                _base_item(
                    board=board,
                    title=title,
                    rank=_safe_int(item.get("position"), index),
                    url=url,
                    hot_value=_safe_int(item.get("heat") or item.get("user_count")),
                    category="音乐热点",
                    author=str(info.get("author") or "").strip(),
                    limit=limit,
                    captured_at=captured_at,
                )
            )
        return output
    raise DouyinHotSourceError(f"不支持的抖音榜单：{board}")


async def fetch_douyin_board(board: str = "hot_search", *, limit: int = 50, timeout: float = 20.0) -> dict[str, Any]:
    board = str(board or "hot_search").strip().lower()
    if board not in BOARD_ENDPOINTS:
        raise DouyinHotSourceError(f"不支持的抖音榜单：{board}")
    params = dict(DOUYIN_QUERIES)
    if board == "music":
        params.update({"chart_id": "6853972723954146568", "count": str(limit)})
    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    payload = await _fetch_json(BOARD_ENDPOINTS[board], params, timeout=timeout)
    items = _normalize_items(board, payload, limit=max(1, min(int(limit), 100)), captured_at=captured_at)
    return {
        "status": "ready" if items else "empty",
        "source": "douyin-hot-hub-compatible",
        "board": board,
        "board_label": BOARD_LABELS[board],
        "captured_at": captured_at,
        "items": items,
        "message": "已读取抖音榜单" if items else "抖音榜单当前没有可用数据",
    }


__all__ = [
    "BOARD_ENDPOINTS",
    "BOARD_LABELS",
    "DouyinHotSourceError",
    "classify_hot_title",
    "fetch_douyin_board",
]
