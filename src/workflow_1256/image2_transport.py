"""GPT Image 2 All 的同步/原生异步图片 API transport。"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Mapping, Sequence
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import requests


DEFAULT_IMAGE2_ALL_BASE_URL = "https://api.apimodels.app/v1/"
DEFAULT_IMAGE2_ALL_MODEL = "gpt-image-2-all"
DEFAULT_IMAGE2_ALL_QUALITY = "low"


class Image2TransportError(RuntimeError):
    """Image2 请求失败或返回结构不符合服务契约。"""


class Image2ResponseError(Image2TransportError):
    """服务商已返回完整响应，但响应体明确报告任务失败。"""

    def __init__(self, message: str, *, task_id: str = "") -> None:
        super().__init__(message)
        self.task_id = str(task_id or "").strip()


class Image2RequestUncertainError(Image2TransportError):
    """请求可能已经被服务商接受，但客户端没有拿到完整响应。

    这类错误禁止自动切换到下一个 Key 重发，否则可能产生重复扣费。
    ``task_id`` 来自服务商响应头时可用于后续查询。
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        status_code: int | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = str(task_id or "").strip()
        self.status_code = status_code
        self.response_headers = {
            str(key).lower(): str(value)
            for key, value in (response_headers or {}).items()
        }


class Image2HTTPError(HTTPError):
    """带状态码和脱敏响应信息的 Image2 HTTP 错误。"""

    def __init__(
        self,
        url: str,
        status_code: int,
        detail: str,
        *,
        response_headers: Mapping[str, str] | None = None,
        task_id: str = "",
    ) -> None:
        body = str(detail or "")[:800]
        super().__init__(
            url,
            int(status_code),
            body,
            hdrs=dict(response_headers or {}),
            fp=io.BytesIO(body.encode("utf-8", errors="replace")),
        )
        self.status_code = int(status_code)
        self.response_headers = {
            str(key).lower(): str(value)
            for key, value in (response_headers or {}).items()
        }
        self.task_id = str(task_id or "").strip()


class Image2Response(dict[str, Any]):
    """Mapping 兼容的响应对象，同时保留不落盘的 HTTP 元数据。"""

    def __init__(
        self,
        value: Mapping[str, Any],
        *,
        status_code: int = 200,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(value)
        self.status_code = int(status_code)
        self.response_headers = {
            str(key).lower(): str(item)
            for key, item in (response_headers or {}).items()
        }


Requester = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None, float], Any]


def _default_requester(method: str, url: str, headers: Mapping[str, str], payload: Mapping[str, Any] | None, timeout: float) -> Mapping[str, Any]:
    return _requests_requester(method, url, headers, payload, timeout)


def _requests_requester(method: str, url: str, headers: Mapping[str, str], payload: Mapping[str, Any] | None, timeout: float) -> Mapping[str, Any]:
    request_headers = {"Accept": "application/json", **headers}
    response_headers: dict[str, str] = {}
    response_status = 0
    try:
        response = requests.request(
            method,
            url,
            headers=request_headers,
            json=payload if payload is not None else None,
            timeout=(30, max(60.0, float(timeout))),
            stream=True,
        )
        response_status = int(response.status_code)
        response_headers = {
            str(key).lower(): str(value)
            for key, value in response.headers.items()
        }
        try:
            raw = response.content.decode("utf-8", errors="replace")
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            OSError,
        ) as exc:
            provider_task_id = response_headers.get("x-apimodels-task-id", "")
            raise Image2RequestUncertainError(
                "Image2 响应读取超时；请求状态不确定，禁止自动重试",
                task_id=provider_task_id,
                status_code=response_status or None,
                response_headers=response_headers,
            ) from exc
    except Image2RequestUncertainError:
        raise
    except requests.exceptions.RequestException as exc:
        raise Image2RequestUncertainError(
            f"Image2 网络请求状态不确定：{type(exc).__name__}",
            status_code=response_status or None,
            response_headers=response_headers,
        ) from exc
    return _parse_http_response(
        url,
        response_status,
        response_headers,
        raw,
    )


def _urllib_requester(method: str, url: str, headers: Mapping[str, str], payload: Mapping[str, Any] | None, timeout: float) -> Mapping[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, method=method, headers={"Accept": "application/json", **headers})
    response_headers: dict[str, str] = {}
    response_status = 0
    try:
        with urlopen(request, timeout=timeout) as response:
            response_status = int(getattr(response, "status", 200))
            response_headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            try:
                raw = response.read().decode("utf-8", errors="replace")
            except (TimeoutError, OSError) as exc:
                provider_task_id = response_headers.get("x-apimodels-task-id", "")
                raise Image2RequestUncertainError(
                    "Image2 响应读取超时；请求状态不确定，禁止自动重试",
                    task_id=provider_task_id,
                    status_code=response_status or None,
                    response_headers=response_headers,
                ) from exc
            status = response_status
    except HTTPError as exc:
        response_status = int(exc.code)
        response_headers = {
            str(key).lower(): str(value)
            for key, value in (exc.headers.items() if exc.headers else [])
        }
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    except Image2RequestUncertainError:
        raise
    except (URLError, TimeoutError, OSError) as exc:
        # 没有拿到响应头时也不能确定服务商是否已受理请求；对 POST
        # 尤其不能盲目切换 Key，避免一次用户操作被重复计费。
        raise Image2RequestUncertainError(
            f"Image2 网络请求状态不确定：{type(exc).__name__}",
            status_code=response_status or None,
            response_headers=response_headers,
        ) from exc
    return _parse_http_response(
        url,
        status,
        response_headers,
        raw,
    )


def _parse_http_response(url: str, status: int, response_headers: Mapping[str, str], raw: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw_response": raw[:800]}
    if not isinstance(parsed, Mapping):
        raise Image2TransportError("Image2 响应根节点必须是对象")
    if status >= 400:
        detail = json.dumps(dict(parsed), ensure_ascii=False)[:800]
        parsed_result = extract_image_result(parsed, response_headers=response_headers)
        raise Image2HTTPError(
            url,
            status,
            detail,
            response_headers=response_headers,
            task_id=str(parsed_result.get("task_id") or ""),
        )
    return Image2Response(
        parsed,
        status_code=status,
        response_headers=response_headers,
    )


def _clean_model_id(value: str) -> str:
    model = str(value or "").strip()
    return DEFAULT_IMAGE2_ALL_MODEL if model in {"", "image2", "gpt-image-2"} else model


class Image2AllHTTPClient:
    """单一 Image2 接入点客户端，兼容同步和原生异步两种模式。"""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_IMAGE2_ALL_BASE_URL, model_id: str = DEFAULT_IMAGE2_ALL_MODEL, timeout: float = 180.0, requester: Requester | None = None) -> None:
        if not str(api_key or "").strip():
            raise Image2TransportError("Image2 API key 不能为空")
        endpoint = str(base_url or "").strip()
        if not endpoint:
            raise Image2TransportError("Image2 接入点不能为空")
        self.api_key = str(api_key).strip()
        self.base_url = endpoint.rstrip("/") + "/"
        self.model_id = _clean_model_id(model_id)
        self.timeout = max(90.0, float(timeout))
        self.requester = requester or _default_requester

    def create_image(self, *, prompt: str, size: str = "1024x1536", quality: str = DEFAULT_IMAGE2_ALL_QUALITY, aspect_ratio: str | None = None, resolution: str | None = None, n: int = 1, image_urls: Sequence[str] = (), image_base64: str = "", image_mime_type: str = "image/png", output_format: str = "", background: str = "") -> Mapping[str, Any]:
        prompt_value = str(prompt or "").strip()
        if not prompt_value:
            raise Image2TransportError("Image2 prompt 不能为空")
        quality_value = str(quality or DEFAULT_IMAGE2_ALL_QUALITY).strip().lower()
        if quality_value not in {"low", "medium", "high", "auto"}:
            raise Image2TransportError("Image2 quality 必须是 low、medium、high 或 auto")
        payload: dict[str, Any] = {"model": self.model_id, "prompt": prompt_value, "quality": quality_value, "n": max(1, min(10, int(n or 1)))}
        size_value = str(size or "").strip()
        if size_value:
            payload["size"] = size_value
        elif aspect_ratio:
            payload["aspect_ratio"] = str(aspect_ratio).strip()
        if resolution:
            payload["resolution"] = str(resolution).strip().upper()
        urls = [str(item).strip() for item in image_urls if str(item).strip()]
        if urls:
            payload["image_urls"] = urls[:16]
            if len(urls) == 1:
                payload["image_url"] = urls[0]
        if image_base64:
            payload["image_base64"] = str(image_base64)
            payload["image_mime_type"] = str(image_mime_type or "image/png")
        if output_format:
            payload["output_format"] = str(output_format).strip().lower()
        if background:
            payload["background"] = str(background).strip().lower()
        return self.requester("POST", urljoin(self.base_url, "images/generations"), self._headers(), payload, self.timeout)

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        """按 APIMODELS 文档查询原生异步任务，不创建新任务。"""

        value = str(task_id or "").strip()
        if not value:
            raise Image2TransportError("Image2 task_id 不能为空")
        query_url = urljoin(self.base_url, "images/generations")
        query_url += "?" + urlencode({"task_id": value})
        return self.requester(
            "GET",
            query_url,
            self._headers(),
            None,
            self.timeout,
        )

    def _headers(self) -> dict[str, str]:
        value = self.api_key if self.api_key.lower().startswith("bearer ") else "Bearer " + self.api_key
        return {"Authorization": value, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Agent-Trigger/1.0"}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _collect_image_values(value: Any, urls: list[str], b64_values: list[str]) -> None:
    if isinstance(value, Mapping):
        for key in ("resultUrls", "result_urls", "urls"):
            urls.extend(_string_list(value.get(key)))
        for key in ("url", "image_url", "imageUrl"):
            urls.extend(_string_list(value.get(key)))
        for key in ("b64_json", "b64Json", "image_base64", "imageBase64"):
            b64_values.extend(_string_list(value.get(key)))
        for key in ("images", "result", "data"):
            nested = value.get(key)
            if nested is not value:
                _collect_image_values(nested, urls, b64_values)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_image_values(item, urls, b64_values)


def _first_error(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        return "", ""
    for key in ("error", "fail", "failure"):
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            code = str(candidate.get("code") or candidate.get("type") or "").strip()
            message = str(candidate.get("message") or candidate.get("msg") or candidate.get("detail") or "").strip()
            if code or message:
                return code, message
        elif isinstance(candidate, str) and candidate.strip():
            return "", candidate.strip()
    code = str(value.get("error_code") or value.get("errorCode") or "").strip()
    state = str(value.get("state") or value.get("status") or "").strip().lower()
    # 普通轮询响应也可能带 message（例如“排队中”）；只有失败态下
    # 才把它视为错误，避免把 processing 误判成 failed。
    message = str(value.get("failMsg") or "").strip()
    if not message and state in {"failed", "error", "cancelled", "canceled"}:
        message = str(value.get("message") or value.get("msg") or "").strip()
    return code, message


def _first_task_id(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    direct = str(value.get("taskId") or value.get("task_id") or "").strip()
    if direct:
        return direct
    for key in ("error", "data", "result"):
        candidate = value.get(key)
        found = _first_task_id(candidate)
        if found:
            return found
    return ""


def extract_image_result(
    response: Mapping[str, Any],
    *,
    response_headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """统一解析同步、异步轮询和 HTTP 200 体内错误响应。

    APIMODELS 的同步长请求可能先发 200，再把错误放在 ``error`` 字段；
    任务句柄也可能只出现在 ``x-apimodels-task-id`` 响应头，二者都不能丢。
    """

    root = response if isinstance(response, Mapping) else {}
    data = root.get("data")
    state = str(root.get("state") or root.get("status") or "").strip().lower()
    task_id = _first_task_id(root)
    urls: list[str] = []
    b64_values: list[str] = []
    error_code, error_message = _first_error(root)
    if isinstance(data, Mapping):
        state = str(data.get("state") or data.get("status") or state).strip().lower()
        task_id = _first_task_id(data) or task_id
        nested_code, nested_message = _first_error(data)
        error_code = nested_code or error_code
        error_message = nested_message or error_message
    _collect_image_values(root, urls, b64_values)
    headers = response_headers or getattr(response, "response_headers", {}) or {}
    if isinstance(headers, Mapping):
        task_id = task_id or str(
            headers.get("x-apimodels-task-id")
            or headers.get("X-Apimodels-Task-Id")
            or ""
        ).strip()
    if urls or b64_values:
        state = "completed"
    elif error_code or error_message:
        state = state or "failed"
    error = f"{error_code}: {error_message}" if error_code and error_message else error_code or error_message
    return {
        "task_id": task_id,
        "state": state,
        "status": state,
        "image_urls": list(dict.fromkeys(urls)),
        "image_b64": list(dict.fromkeys(b64_values)),
        "error": error,
        "error_code": error_code,
    }


def decode_image_base64(value: str) -> bytes:
    raw = str(value or "").strip()
    if raw.startswith("data:") and "," in raw: raw = raw.split(",", 1)[1]
    if not raw: raise Image2TransportError("Image2 返回空 b64_json")
    try: return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc: raise Image2TransportError("Image2 返回的 b64_json 无法解码") from exc


__all__ = [
    "DEFAULT_IMAGE2_ALL_BASE_URL",
    "DEFAULT_IMAGE2_ALL_MODEL",
    "DEFAULT_IMAGE2_ALL_QUALITY",
    "Image2AllHTTPClient",
    "Image2HTTPError",
    "Image2RequestUncertainError",
    "Image2ResponseError",
    "Image2Response",
    "Image2TransportError",
    "extract_image_result",
    "decode_image_base64",
]
