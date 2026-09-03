"""aishuch 图像任务 API 的可配置 transport。

``create_image_task.py`` 和 ``get_task_result.py`` 已经包含原插件的参数清洗、
重试和轮询逻辑；本模块只提供真实 HTTP 传输，不改变上层契约。
"""

from __future__ import annotations

import json
import io
import os
from collections.abc import Mapping
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .create_image_task import CreateImageTaskRequest
from .material_models import IMAGE_MODEL_API_IDS
from .get_task_result import GetTaskResultRequest


DEFAULT_BASE_URL = "https://api.aishuch.com/v1/"


class AishuchTransportError(RuntimeError):
    """aishuch 请求失败或响应不是对象。"""


Requester = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None, float], Any]


def _default_requester(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any] | None,
    timeout: float,
) -> Mapping[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", **headers},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        raise AishuchTransportError(f"aishuch 网络请求失败：{type(exc).__name__}") from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"raw_response": raw[:800]}
    if not isinstance(body, Mapping):
        raise AishuchTransportError("aishuch 响应根节点必须是对象")
    if status >= 400:
        # 保留原节点对 urllib.error.HTTPError 的分支判断（400/401/403/404
        # 立即失败，其他状态按原重试策略处理）。
        raise HTTPError(
            url,
            status,
            json.dumps(dict(body), ensure_ascii=False)[:800],
            hdrs=None,
            fp=io.BytesIO(json.dumps(dict(body), ensure_ascii=False).encode("utf-8")),
        )
    return dict(body)


class AishuchHTTPClient:
    """为两个图像任务节点提供可注入 HTTP transport。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        requester: Requester | None = None,
    ) -> None:
        if not api_key.strip():
            raise AishuchTransportError("aishuch API key 不能为空")
        self.api_key = api_key.strip()
        self.base_url = base_url.strip().rstrip("/") + "/"
        self.timeout = float(timeout)
        self.requester = requester or _default_requester

    @classmethod
    def from_env(cls, *, timeout: float = 15.0) -> "AishuchHTTPClient":
        key = os.environ.get("AISHUCH_API_KEY", "").strip()
        if not key:
            raise AishuchTransportError("未配置 AISHUCH_API_KEY")
        return cls(
            key,
            base_url=os.environ.get("AISHUCH_BASE_URL", DEFAULT_BASE_URL),
            timeout=timeout,
        )

    def _headers(self, request_key: str) -> dict[str, str]:
        value = request_key.strip() or self.api_key
        if not value.lower().startswith("bearer "):
            value = "Bearer " + value
        return {
            "Authorization": value,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Agent-Trigger/1.0",
        }

    def create_image(self, request: CreateImageTaskRequest) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "model": IMAGE_MODEL_API_IDS[request.image_model],
            "prompt": request.prompt,
            "n": request.n,
            "size": request.size,
            "resolution": request.resolution,
            "official_fallback": request.official_fallback,
        }
        if request.image_urls:
            payload["image_urls"] = list(request.image_urls)
        return self.requester(
            "POST",
            urljoin(self.base_url, "images/generations"),
            self._headers(request.api_key),
            payload,
            self.timeout,
        )

    def get_task(self, request: GetTaskResultRequest) -> Mapping[str, Any]:
        return self.requester(
            "GET",
            urljoin(self.base_url, "tasks/" + request.task_id),
            self._headers(request.api_key),
            None,
            self.timeout,
        )


__all__ = ["AishuchHTTPClient", "AishuchTransportError"]
