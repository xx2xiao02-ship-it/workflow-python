import requests
import time
import tos


# ==========================================
# 1. 配置区
# ==========================================
DEFAULT_MAX_WAIT = 175
DEFAULT_CHECK_INTERVAL = 10

AUTH_CONFIGS = [
    {
        "name": "主账号 (新鉴权)",
        "api_key": "__REDACTED_CREDENTIAL__"
    },
    {
        "name": "备用账号 (旧鉴权)",
        "api_key": "__REDACTED_CREDENTIAL__"
    }
]

TOS_CONFIGS = [
    {
        "name": "TOS主账号 (新存储15p2)",
        "ak": "__REDACTED_CREDENTIAL__",
        "sk": "__REDACTED_CREDENTIAL__",
        "bucket": "temp-video-seedance15p2",
        "endpoint": "https://tos-cn-beijing.volces.com",
        "region": "cn-beijing"
    },
    {
        "name": "TOS备用账号 (旧存储15p)",
        "ak": "__REDACTED_CREDENTIAL__",
        "sk": "__REDACTED_CREDENTIAL__",
        "bucket": "temp-video-seedance15p",
        "endpoint": "https://tos-cn-beijing.volces.com",
        "region": "cn-beijing"
    }
]

MAX_CONSECUTIVE_ERRORS = 5


# ==========================================
# 2. 工具函数
# ==========================================
def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def get_value(data, key, default=None):
    if isinstance(data, dict):
        value = data.get(key, default)
    else:
        value = getattr(data, key, default)
    return default if value is None else value


def get_full_video_url(data):
    if isinstance(data, dict):
        outputs = data.get("output", [])
        if isinstance(outputs, list):
            for item in outputs:
                if isinstance(item, dict):
                    video_url = clean_text(item.get("video_url", ""))
                    if video_url.startswith("http") and ".mp4" in video_url.lower():
                        return video_url
    if isinstance(data, dict):
        for _, value in data.items():
            if isinstance(value, str):
                if value.startswith("http") and ".mp4" in value.lower():
                    return value
            result = get_full_video_url(value)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = get_full_video_url(item)
            if result:
                return result
    return ""


def get_error_info(resp):
    raw_text = clean_text(getattr(resp, "text", ""))
    try:
        data = resp.json()
    except Exception:
        return {"code": "", "message": raw_text, "param": ""}
    if not isinstance(data, dict):
        return {"code": "", "message": raw_text, "param": ""}
    error = data.get("error", data)
    if not isinstance(error, dict):
        error = {}
    return {
        "code": clean_text(error.get("code", "")),
        "message": clean_text(error.get("message") or data.get("message") or raw_text),
        "param": clean_text(error.get("param") or data.get("param") or "")
    }


def translate_ark_error(status_code, error_info):
    code = clean_text(error_info.get("code", ""))
    message = clean_text(error_info.get("message", ""))
    param = clean_text(error_info.get("param", ""))
    merged = f"{code} {message} {param}".lower()

    if "task not found" in merged or ("not found" in merged and "task" in merged):
        return "未找到该任务：任务 ID 不存在、已过期，或不属于当前账号"
    if "resource not found" in merged and "image" in merged:
        return "图片资源无法访问：首帧或尾帧图片链接无法被方舟服务端读取"
    if "quota" in merged or "insufficient" in merged or "balance" in merged:
        return "账号额度不足或余额不足，请检查方舟账户额度"
    if "rate limit" in merged or "too many requests" in merged:
        return "请求过于频繁，触发限流，请稍后再试"
    if "content policy" in merged or "safety" in merged:
        return "任务内容触发安全审核，请调整提示词或图片后重试"
    if "invalidparameter" in merged or "invalid parameter" in merged:
        return f"参数错误：{message}"
    if "internal error" in merged or "system error" in merged:
        return "模型服务内部异常，请稍后重试"

    if status_code == 401:
        return "鉴权失败：API Key 无效、已失效，或 Bearer 格式不正确"
    if status_code == 403:
        return "没有权限查询该任务：当前账号可能不是创建任务的账号"
    if status_code == 404:
        return "未找到任务：任务 ID、接口地址或模型服务不存在"
    if status_code == 429:
        return "请求受限：触发并发、频率或额度限制，请稍后重试"
    if status_code >= 500:
        return "方舟服务暂时异常，请稍后重试"

    return message or "方舟任务查询失败，请检查任务 ID、账号鉴权和网络连接"


def translate_task_failed_error(task_data):
    """重点优化：精准读取官方任务失败的原始错误"""
    error_data = task_data.get("error", {})
    
    if isinstance(error_data, dict):
        error_code = clean_text(error_data.get("code", ""))
        error_msg = clean_text(error_data.get("message", ""))
        error_param = clean_text(error_data.get("param", ""))
    else:
        error_code = ""
        error_msg = clean_text(error_data)
        error_param = ""

    # 优先返回官方原始错误，避免笼统提示
    if error_msg:
        merged = f"{error_code} {error_msg} {error_param}".lower()
        
        if "content policy" in merged or "safety" in merged:
            return f"任务生成失败：触发安全审核。官方原因：{error_msg}"
        if "image" in merged and ("download" in merged or "fetch" in merged or "invalid" in merged):
            return f"任务生成失败：图片无法读取。官方原因：{error_msg}"
        if "duration" in merged:
            return f"任务生成失败：时长不支持。官方原因：{error_msg}"
        if "parameter" in merged or "invalid" in merged:
            return f"任务生成失败：参数非法。官方原因：{error_msg}"
        if "not support" in merged or "unsupported" in merged:
            return f"任务生成失败：功能不支持。官方原因：{error_msg}"
        
        return f"任务生成失败：{error_msg}"
    
    return "任务生成失败，接口未返回具体错误原因"


def is_configured_api_key(api_key):
    api_key = clean_text(api_key)
    if not api_key:
        return False
    if "在这里填" in api_key or "请替换" in api_key:
        return False
    if api_key == "Bearer":
        return False
    return True


def is_configured_tos(conf):
    ak = clean_text(conf.get("ak", ""))
    sk = clean_text(conf.get("sk", ""))
    bucket = clean_text(conf.get("bucket", ""))
    if not ak or not sk or not bucket:
        return False
    if "在这里填" in ak or "在这里填" in sk:
        return False
    return True


def upload_to_tos(video_content, object_key):
    last_error = ""
    for tos_conf in TOS_CONFIGS:
        tos_name = tos_conf.get("name", "未知 TOS 账号")
        if not is_configured_tos(tos_conf):
            last_error = f"{tos_name}：未配置 AK、SK 或 Bucket"
            continue
        for retry in range(2):
            try:
                tos_client = tos.TosClientV2(
                    tos_conf["ak"],
                    tos_conf["sk"],
                    tos_conf["endpoint"],
                    tos_conf["region"]
                )
                tos_client.put_object(
                    tos_conf["bucket"],
                    object_key,
                    content=video_content,
                    content_type="video/mp4"
                )
                public_url = f"https://{tos_conf['bucket']}.tos-cn-beijing.volces.com/{object_key}"
                return True, public_url, tos_name
            except Exception as e:
                last_error = f"{tos_name}：{str(e)}"
                if retry == 0:
                    time.sleep(1)
                continue
    return False, "", last_error


# ==========================================
# 3. 主函数
# ==========================================
def handler(args):
    input_data = getattr(args, "input", None)
    if input_data is None:
        input_data = getattr(args, "params", {})
    if isinstance(input_data, dict) and isinstance(input_data.get("_input"), dict):
        input_data = input_data["_input"]

    task_id = clean_text(get_value(input_data, "task_id", ""))
    max_wait = int(get_value(input_data, "max_wait", DEFAULT_MAX_WAIT))
    check_interval = int(get_value(input_data, "check_interval", DEFAULT_CHECK_INTERVAL))
    enable_tos = bool(get_value(input_data, "enable_tos_upload", True))
    tos_prefix = clean_text(get_value(input_data, "tos_path_prefix", "seedance-videos"))

    if not task_id:
        return {
            "success": False,
            "msg": "参数缺失：必须填写任务 ID",
            "public_video_url": "",
            "temp_video_url": "",
            "status": "参数错误",
            "waited_seconds": 0,
            "file_size_mb": 0,
            "raw_error": ""
        }

    query_url = f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}"

    # 匹配可用账号
    valid_headers = None
    locked_auth_name = ""
    last_auth_error = ""

    for auth in AUTH_CONFIGS:
        api_key = clean_text(auth.get("api_key", ""))
        if not is_configured_api_key(api_key):
            last_auth_error = f"{auth.get('name', '未知账号')} 未配置 API Key"
            continue
        temp_headers = {"Authorization": api_key, "Content-Type": "application/json"}
        try:
            test_resp = requests.get(query_url, headers=temp_headers, timeout=8)
            if test_resp.status_code == 200:
                valid_headers = temp_headers
                locked_auth_name = auth.get("name", "未知账号")
                break
            error_info = get_error_info(test_resp)
            last_auth_error = f"{auth.get('name', '未知账号')}：{translate_ark_error(test_resp.status_code, error_info)}"
        except Exception as e:
            last_auth_error = f"{auth.get('name', '未知账号')}：{str(e)}"

    if not valid_headers:
        return {
            "success": False,
            "msg": f"查询失败：所有方舟鉴权都无法读取该任务。最后错误：{last_auth_error}",
            "public_video_url": "",
            "temp_video_url": "",
            "status": "鉴权或任务异常",
            "waited_seconds": 0,
            "file_size_mb": 0,
            "raw_error": last_auth_error
        }

    # 轮询任务
    start_time = time.time()
    consecutive_errors = 0
    last_query_error = ""
    raw_error = ""

    while True:
        waited = int(time.time() - start_time)
        try:
            query_resp = requests.get(query_url, headers=valid_headers, timeout=12)

            if query_resp.status_code != 200:
                consecutive_errors += 1
                error_info = get_error_info(query_resp)
                last_query_error = translate_ark_error(query_resp.status_code, error_info)
                raw_error = f"{error_info.get('code', '')}: {error_info.get('message', '')}"

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS or waited >= max_wait:
                    return {
                        "success": False,
                        "msg": f"查询失败：{last_query_error}",
                        "public_video_url": "",
                        "temp_video_url": "",
                        "status": "查询异常",
                        "waited_seconds": waited,
                        "file_size_mb": 0,
                        "raw_error": raw_error
                    }
                time.sleep(check_interval)
                continue

            consecutive_errors = 0
            task_data = query_resp.json()
            status = clean_text(task_data.get("status", "")).lower()

            # 任务完成
            if status in ["succeeded", "completed"]:
                temp_video_url = get_full_video_url(task_data)
                if not temp_video_url:
                    return {
                        "success": False,
                        "msg": "任务已完成，但接口没有返回可用的视频链接",
                        "public_video_url": "",
                        "temp_video_url": "",
                        "status": "视频链接异常",
                        "waited_seconds": waited,
                        "file_size_mb": 0,
                        "raw_error": ""
                    }

                try:
                    video_resp = requests.get(temp_video_url, timeout=60)
                    if video_resp.status_code != 200:
                        return {
                            "success": False,
                            "msg": "视频下载失败：无法从方舟临时链接获取视频文件",
                            "public_video_url": "",
                            "temp_video_url": temp_video_url,
                            "status": "生成完成，下载失败",
                            "waited_seconds": waited,
                            "file_size_mb": 0,
                            "raw_error": ""
                        }
                    video_content = video_resp.content
                    file_size_mb = round(len(video_content) / 1024 / 1024, 2)
                    if len(video_content) < 1024 * 50:
                        return {
                            "success": False,
                            "msg": "视频下载失败：下载到的文件过小，可能是临时链接失效",
                            "public_video_url": "",
                            "temp_video_url": temp_video_url,
                            "status": "生成完成，文件异常",
                            "waited_seconds": waited,
                            "file_size_mb": file_size_mb,
                            "raw_error": ""
                        }
                except Exception as e:
                    return {
                        "success": False,
                        "msg": f"视频下载失败：{str(e)}",
                        "public_video_url": "",
                        "temp_video_url": temp_video_url,
                        "status": "生成完成，下载异常",
                        "waited_seconds": waited,
                        "file_size_mb": 0,
                        "raw_error": str(e)
                    }

                if not enable_tos:
                    return {
                        "success": True,
                        "msg": "视频生成完成（未开启 TOS 转存，返回临时链接）",
                        "public_video_url": temp_video_url,
                        "temp_video_url": temp_video_url,
                        "status": "生成完成",
                        "waited_seconds": waited,
                        "file_size_mb": file_size_mb,
                        "tip": f"方舟查询账号：{locked_auth_name}",
                        "raw_error": ""
                    }

                object_key = f"{tos_prefix}/{task_id}.mp4"
                upload_ok, public_url, tos_info = upload_to_tos(video_content, object_key)
                if upload_ok:
                    return {
                        "success": True,
                        "msg": "视频生成完成，已成功转存为永久链接",
                        "public_video_url": public_url,
                        "temp_video_url": temp_video_url,
                        "status": "生成完成",
                        "waited_seconds": waited,
                        "file_size_mb": file_size_mb,
                        "tip": f"方舟查询：{locked_auth_name}｜TOS 存储：{tos_info}",
                        "raw_error": ""
                    }
                else:
                    return {
                        "success": False,
                        "msg": f"视频已生成，但转存 TOS 失败：{tos_info}",
                        "public_video_url": "",
                        "temp_video_url": temp_video_url,
                        "status": "生成完成，转存失败",
                        "waited_seconds": waited,
                        "file_size_mb": file_size_mb,
                        "raw_error": tos_info
                    }

            # 任务失败
            if status in ["failed", "error", "cancelled", "canceled"]:
                error_msg = translate_task_failed_error(task_data)
                raw_error = str(task_data.get("error", ""))
                return {
                    "success": False,
                    "msg": error_msg,
                    "public_video_url": "",
                    "temp_video_url": "",
                    "status": "生成失败",
                    "waited_seconds": waited,
                    "file_size_mb": 0,
                    "raw_error": raw_error
                }

            # 仍在生成
            if waited >= max_wait:
                return {
                    "success": False,
                    "msg": f"任务等待超时：已等待 {max_wait} 秒，任务仍未完成",
                    "public_video_url": "",
                    "temp_video_url": "",
                    "status": "等待超时",
                    "waited_seconds": waited,
                    "file_size_mb": 0,
                    "raw_error": ""
                }

            time.sleep(check_interval)

        except Exception as e:
            consecutive_errors += 1
            last_query_error = str(e)
            raw_error = str(e)

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS or waited >= max_wait:
                return {
                    "success": False,
                    "msg": f"查询异常：{last_query_error}",
                    "public_video_url": "",
                    "temp_video_url": "",
                    "status": "查询异常",
                    "waited_seconds": waited,
                    "file_size_mb": 0,
                    "raw_error": raw_error
                }

            time.sleep(check_interval)



class VideoQueryTransportRequired(RuntimeError):
    """未提供离线传输层时，阻止真实网络请求。"""


def run_video_query(params, *, requests_client=None, clock=None, sleep=None):
    """运行脱敏原始逻辑；requests_client 为空时不访问外部服务。"""
    if requests_client is None:
        raise VideoQueryTransportRequired("未配置 video_query 传输层；当前只允许离线契约测试")
    import types
    original_requests = globals().get("requests")
    original_time = globals().get("time")
    clock = clock or types.SimpleNamespace(time=lambda: 0.0)
    sleep_fn = sleep or (lambda _seconds: None)
    time_proxy = types.SimpleNamespace(sleep=sleep_fn, time=clock.time)
    globals()["requests"] = requests_client
    globals()["time"] = time_proxy
    try:
        return handler(types.SimpleNamespace(input=params))
    finally:
        globals()["requests"] = original_requests
        globals()["time"] = original_time


def to_coze_node_output(raw_result):
    """投影到工作流 YAML 声明的八个输出字段。"""
    return {
        "errorBody": raw_result.get("errorBody"),
        "file_size_mb": raw_result.get("file_size_mb"),
        "isSuccess": raw_result.get("success", False),
        "msg": raw_result.get("msg"),
        "public_video_url": raw_result.get("public_video_url"),
        "status": raw_result.get("status"),
        "success": raw_result.get("success"),
        "tip": raw_result.get("tip"),
    }

