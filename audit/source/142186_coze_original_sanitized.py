from runtime import Args
import urllib.request
import urllib.error
import json
import time


def handler(args: Args) -> dict:
    if isinstance(args.input, dict):
        params = args.input
    elif hasattr(args.input, "__dict__"):
        params = args.input.__dict__
    else:
        try:
            params = dict(args.input)
        except:
            params = {}

    raw_api_key = str(params.get("api_key", "")).strip()
    if not raw_api_key or raw_api_key == "None":
        return {"error": True, "message": "缺失必填参数: api_key"}

    api_key = raw_api_key if raw_api_key.startswith("Bearer ") else f"Bearer {raw_api_key}"

    prompt = str(params.get("prompt", "")).strip()
    if not prompt or prompt == "None":
        return {"error": True, "message": "提示词不能为空"}

    raw_n = params.get("n", 1)
    n = int(raw_n) if raw_n is not None and str(raw_n).isdigit() else 1
    size = params.get("size", "auto") or "auto"
    resolution = params.get("resolution", "1k") or "1k"

    raw_urls = params.get("image_urls", [])
    image_urls = raw_urls if isinstance(raw_urls, list) else []
    official_fallback = bool(params.get("official_fallback", False))

    create_url = "https://api.aishuch.com/v1/images/generations"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Agent-Trigger/1.0",
    }

    payload = {
        "model": "gpt-image-2",
        "prompt": prompt,
        "n": n,
        "size": str(size),
        "resolution": str(resolution),
        "official_fallback": official_fallback,
    }
    if len(image_urls) > 0:
        payload["image_urls"] = image_urls

    data = json.dumps(payload).encode("utf-8")

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    for attempt in range(MAX_RETRIES):
        req_create = urllib.request.Request(create_url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req_create, timeout=15) as response:
                raw_result = json.loads(response.read().decode("utf-8"))
                data_array = raw_result.get("data", [])

                if data_array and isinstance(data_array, list) and len(data_array) > 0:
                    task_id = data_array[0].get("task_id")
                    if task_id:
                        return {
                            "code": 202,
                            "status": "processing",
                            "task_id": task_id,
                            "attempted": attempt + 1,
                            "message": "任务创建成功！已转交下游处理",
                        }

                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                    continue
                return {
                    "error": True,
                    "message": "接口调用成功，但未返回有效的 task_id，可能 API 结构已变更。",
                }

        except urllib.error.HTTPError as e:
            if e.code in [400, 401, 403, 404]:
                return {
                    "error": True,
                    "status_code": e.code,
                    "message": f"客户端致命错误，请检查参数或 API_KEY: {e.read().decode('utf-8')[:200]}",
                }

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                RETRY_DELAY *= 1.5
                continue
            return {
                "error": True,
                "status_code": e.code,
                "message": "远端服务器持续不可用或超载，创建任务失败。",
            }

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                RETRY_DELAY *= 1.5
                continue
            return {"error": True, "message": f"网络请求失败，已重试 {MAX_RETRIES} 次: {str(e)}"}

    return {"error": True, "message": "任务创建最终失败。"}
