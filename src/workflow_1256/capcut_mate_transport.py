"""CapCut Mate HTTP transport。

该模块把剪映小助手 HTTP API 封装成可注入到工作流节点的客户端。它只做
请求发送和响应返回，不替工作流猜测字段、不吞掉服务端错误，也不把旧的
``draft_url`` 覆盖新地址。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


API_SUFFIX = "/openapi/capcut-mate/v1/"
POST_ENDPOINTS = {
    "create_draft",
    "save_draft",
    "add_videos",
    "add_audios",
    "add_images",
    "add_keyframes",
    "add_captions",
    "add_effects",
    "video_infos",
    "audio_infos",
    "caption_infos",
    "effect_infos",
    "keyframes_infos",
    "timelines",
    "audio_timelines",
}


class CapCutMateTransportError(RuntimeError):
    """CapCut Mate 无法完成请求或返回了不可解析响应。"""


Requester = Callable[[str, Mapping[str, Any], float], Any]
GetRequester = Callable[[str, Mapping[str, Any], float], Any]


def _open_request(request: Request, timeout: float):
    """本机 CapCut Mate 不经过系统代理，避免服务未启动时额外等待。"""
    host = (urlparse(request.full_url).hostname or "").strip().lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def _default_requester(url: str, payload: Mapping[str, Any], timeout: float) -> Any:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with _open_request(request, timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise CapCutMateTransportError(
            f"CapCut Mate HTTP {exc.code}: {raw[:800]}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CapCutMateTransportError(
            f"CapCut Mate 网络请求失败：{type(exc).__name__}"
        ) from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapCutMateTransportError("CapCut Mate 响应不是合法 JSON") from exc


def _default_get_requester(url: str, params: Mapping[str, Any], timeout: float) -> Any:
    query = urlencode({key: str(value) for key, value in params.items()})
    request_url = url + ("?" + query if query else "")
    request = Request(request_url, method="GET", headers={"Accept": "application/json"})
    try:
        with _open_request(request, timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        raise CapCutMateTransportError(
            f"CapCut Mate GET 网络请求失败：{type(exc).__name__}"
        ) from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapCutMateTransportError("CapCut Mate GET 响应不是合法 JSON") from exc
    if status >= 400:
        raise CapCutMateTransportError(
            f"CapCut Mate GET HTTP {status}: " + json.dumps(result, ensure_ascii=False)[:800]
        )
    if not isinstance(result, Mapping):
        raise CapCutMateTransportError("CapCut Mate GET 响应根节点必须是对象")
    return dict(result)


class CapCutMateClient:
    """可注入到节点适配器和工作流编排器的 CapCut Mate 客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        requester: Requester | None = None,
        get_requester: GetRequester | None = None,
    ) -> None:
        value = base_url.strip()
        if not value:
            raise CapCutMateTransportError("CapCut Mate base_url 不能为空")
        self.base_url = value.rstrip("/")
        self.timeout = float(timeout)
        self.requester = requester or _default_requester
        self.get_requester = get_requester or _default_get_requester

    @classmethod
    def from_env(cls, *, timeout: float = 60.0) -> "CapCutMateClient":
        base_url = os.environ.get("CAPCUT_MATE_BASE_URL", "").strip()
        if not base_url:
            raise CapCutMateTransportError(
                "未配置 CAPCUT_MATE_BASE_URL；不会默认调用公网服务"
            )
        return cls(base_url, timeout=timeout)

    def endpoint_url(self, endpoint: str) -> str:
        if endpoint not in POST_ENDPOINTS:
            raise CapCutMateTransportError(f"不支持的 CapCut Mate endpoint：{endpoint}")
        if self.base_url.endswith("/openapi/capcut-mate/v1"):
            return self.base_url + "/" + endpoint
        return urljoin(self.base_url + "/", API_SUFFIX + endpoint)

    def call(self, endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self.requester(self.endpoint_url(endpoint), payload, self.timeout)
        if not isinstance(response, Mapping):
            raise CapCutMateTransportError("CapCut Mate 响应根节点必须是对象")
        if response.get("error_type") or response.get("detail") and response.get("status_code"):
            raise CapCutMateTransportError(
                "CapCut Mate 返回错误：" + json.dumps(dict(response), ensure_ascii=False)[:800]
            )
        return dict(response)

    def create_draft(self, request: Any) -> dict[str, Any]:
        return self.call("create_draft", {"height": request.height, "width": request.width})

    def save_draft(self, request: Any) -> dict[str, Any]:
        return self.call("save_draft", {"draft_url": request.draft_url})

    def add_audios(
        self,
        draft_url: str,
        audio_infos: str,
        _normalized: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.call("add_audios", {"draft_url": draft_url, "audio_infos": audio_infos})

    def add_videos(
        self,
        draft_url: str,
        video_infos: str,
        **options: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"draft_url": draft_url, "video_infos": video_infos}
        for key in (
            "scene_timelines",
            "alpha",
            "scale_x",
            "scale_y",
            "transform_x",
            "transform_y",
        ):
            if key in options:
                payload[key] = options[key]
        return self.call("add_videos", payload)

    def add_captions(self, draft_url: str, captions: str, **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"draft_url": draft_url, "captions": captions}
        payload.update(kwargs)
        return self.call("add_captions", payload)

    def add_keyframes(self, draft_url: str, keyframes: str) -> dict[str, Any]:
        return self.call("add_keyframes", {"draft_url": draft_url, "keyframes": keyframes})

    def add_effects(self, draft_url: str, effect_infos: str) -> dict[str, Any]:
        return self.call("add_effects", {"draft_url": draft_url, "effect_infos": effect_infos})

    def get_draft(self, draft_url_or_id: str) -> dict[str, Any]:
        """读取草稿文件清单，只读验证，不修改草稿。"""

        value = str(draft_url_or_id).strip()
        parsed = urlparse(value)
        draft_id = parse_qs(parsed.query).get("draft_id", [""])[0] if parsed.query else value
        if not draft_id:
            raise CapCutMateTransportError("get_draft 缺少 draft_id")
        url = urljoin(self.base_url + "/", API_SUFFIX + "get_draft")
        response = self.get_requester(url, {"draft_id": draft_id}, self.timeout)
        if not isinstance(response, Mapping):
            raise CapCutMateTransportError("get_draft 响应根节点必须是对象")
        files = response.get("files", [])
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise CapCutMateTransportError("get_draft.files 必须是字符串数组")
        return {"files": list(files)}


__all__ = ["CapCutMateClient", "CapCutMateTransportError"]
