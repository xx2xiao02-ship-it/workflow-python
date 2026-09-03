import requests
import time
import hashlib
import tos
from urllib.parse import urlparse

from .material_models import normalize_video_model


# =========================================================
# 鉴权配置
# =========================================================

PRIMARY_API_KEY = "__REDACTED_CREDENTIAL__"
# 方舟视频生成 API 支持直接使用模型 ID，也支持 Endpoint ID。
# 自动模式先用 1.5 Pro；1.5 的额度/权限级失败才降级到 1.0 Pro。
PRIMARY_MODEL_1_5 = "doubao-seedance-1-5-pro-251215"
PRIMARY_MODEL_EP = "ep-20260816000749-hp8mq"

BACKUP_API_KEY = "__REDACTED_CREDENTIAL__"
BACKUP_MODEL_EP = "ep-20260816001250-lh9k5"
MODEL_1_0_DEFAULT = "doubao-seedance-1-0-pro-250528"

AUTH_CONFIGS = [
    {
        "name": "主账号",
        "api_key": PRIMARY_API_KEY,
        "model_ep": PRIMARY_MODEL_EP,
        "model_ep_1_5": PRIMARY_MODEL_1_5,
        "model_ep_1_0": PRIMARY_MODEL_EP,
    },
    {
        "name": "备用账号",
        "api_key": BACKUP_API_KEY,
        "model_ep": BACKUP_MODEL_EP,
        "model_ep_1_5": PRIMARY_MODEL_1_5,
        "model_ep_1_0": BACKUP_MODEL_EP,
    },
]

# TOS 图床配置
TOS_CONFIGS = [
    {
        "name": "TOS主账号",
        "ak": "__REDACTED_CREDENTIAL__",
        "sk": "__REDACTED_CREDENTIAL__",
        "bucket": "temp-video-seedance15p2",
        "endpoint": "https://tos-cn-beijing.volces.com",
        "region": "cn-beijing"
    },
    {
        "name": "TOS备用账号",
        "ak": "__REDACTED_CREDENTIAL__",
        "sk": "__REDACTED_CREDENTIAL__",
        "bucket": "temp-video-seedance15p",
        "endpoint": "https://tos-cn-beijing.volces.com",
        "region": "cn-beijing"
    }
]

TOS_PATH_PREFIX = "seedance-input-frames"
IMAGE_DOWNLOAD_TIMEOUT = 15
MIN_IMAGE_SIZE_KB = 5
ENABLE_TOS_UPLOAD = True


# =========================================================
# 内置默认参数
# =========================================================
DEFAULT_DURATION = 5
DEFAULT_CAMERA_FIXED = True
DEFAULT_WATERMARK = False
DEFAULT_GENERATE_AUDIO = False
DEFAULT_DRAFT = False
DEFAULT_PRE_CHECK_WAIT = 20
DEFAULT_SEED = -1


# =========================================================
# 基础配置
# =========================================================

API_URL = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
MAX_ATTEMPTS_PER_AUTH = 2
REQUEST_TIMEOUT = 30

PROMPT_400_RETRY_DELAY = 5
PROMPT_400_RETRY_BACKOFF = 1.5
TRANSIENT_RETRY_DELAY = 2
TRANSIENT_RETRY_BACKOFF = 1.5


# =========================================================
# 工具函数
# =========================================================

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


def normalize_valid_image_url(value):
    if isinstance(value, (list, tuple, dict, set)):
        return ""
    text = clean_text(value)
    if not text:
        return ""
    placeholder_values = {"null", "none", "undefined", "nan", "n/a", "na", "[]", "{}", "-", "--"}
    if text.lower() in placeholder_values:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


def parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ["true", "1", "yes", "y", "on", "是", "开启", "开"]:
            return True
        if text in ["false", "0", "no", "n", "off", "否", "关闭", "关", ""]:
            return False
    return default


def normalize_duration(value, default=5):
    try:
        number = int(float(value))
        return number if number > 0 else default
    except Exception:
        return default


def get_error_info(resp):
    raw_text = clean_text(getattr(resp, "text", ""))
    try:
        data = resp.json()
    except Exception:
        return {"code": "", "message": raw_text, "param": ""}
    if not isinstance(data, dict):
        return {"code": "", "message": raw_text, "param": ""}

    error = data.get("error", {})
    if isinstance(error, dict):
        return {
            "code": clean_text(error.get("code", "")),
            "message": clean_text(error.get("message", "")),
            "param": clean_text(error.get("param", ""))
        }
    return {"code": "", "message": raw_text, "param": ""}


def is_account_level_error(message="", *, status_code=None, error_info=None):
    """判断是否应切换到下一组鉴权，而不是把错误返回给下游。"""
    parts = [clean_text(message)]
    if isinstance(error_info, dict):
        parts.extend(
            clean_text(error_info.get(field, ""))
            for field in ("code", "message", "param")
        )
    text = " ".join(part for part in parts if part).lower()
    if status_code in {401, 403, 429} or (isinstance(status_code, int) and status_code >= 500):
        return True
    account_markers = [
        "setlimitexceeded",
        "inference limit",
        "inference_limit",
        "accountinference",
        "reached the set",
        "safe experience mode",
        "安全体验模式",
        "安心体验模式",
        "quota",
        "insufficient",
        "balance",
        "额度不足",
        "余额不足",
        "已达到调用上限",
        "服务已暂停",
        "rate limit",
        "too many requests",
        "resource id is mismatched",
        "resource mismatch",
        "unauthorized",
        "forbidden",
        "没有权限",
        "鉴权失败",
        "model not found",
        "model unavailable",
        "model access",
        "endpoint not found",
        "endpoint unavailable",
        "接入点不存在",
        "模型不存在",
        "模型不可用",
    ]
    if any(marker in text for marker in account_markers):
        return True
    # 创建接口返回 404 时，只有明确指向模型/接入点才切换配置；
    # 查询接口的 task not found 不经过这里，避免误判已创建任务。
    return status_code == 404 and any(marker in text for marker in ("model", "endpoint", "模型", "接入点"))


def get_field_name_cn(param, message):
    text = f"{param} {message}".lower()
    if "duration" in text:
        return "视频时长 duration"
    if "generate_audio" in text or "audio" in text:
        return "声音生成 generate_audio"
    if "camera_fixed" in text or "camerafixed" in text:
        return "固定镜头 camera_fixed"
    if "watermark" in text:
        return "水印 watermark"
    if "ratio" in text:
        return "画幅比例 ratio"
    if "resolution" in text:
        return "分辨率 resolution"
    if "seed" in text:
        return "随机种子 seed"
    if "image_url" in text or "image" in text:
        return "首帧或尾帧图片"
    if "prompt" in text or "content" in text or "text" in text:
        return "提示词 content.text"
    if "model" in text:
        return "模型接入点 model"
    return ""


def translate_error(status_code, error_info):
    code = clean_text(error_info.get("code", ""))
    message = clean_text(error_info.get("message", ""))
    param = clean_text(error_info.get("param", ""))
    merged = f"{code} {message} {param}".lower()
    field_cn = get_field_name_cn(param, message)

    if "resource not found" in merged and "image" in merged:
        return "图片资源无法访问：首帧或尾帧图片链接无法被模型读取，请确认图片链接可公网访问。"
    if "invalid image" in merged or "image download" in merged or "image fetch" in merged:
        return "图片下载失败：首帧或尾帧图片链接无效、已过期或无法被服务端拉取。"
    if "duration" in merged and ("not supported" in merged or "invalid" in merged or "unsupported" in merged):
        return "视频时长不被当前模型支持，请检查 duration 取值范围。"
    if "content policy" in merged or "safety" in merged:
        return "内容触发安全审核，请调整提示词或图片后重试。"
    if "invalid parameter" in merged or "invalidparameter" in merged:
        if field_cn:
            return f"参数错误：{field_cn} 不符合官方规范。"
        return f"参数错误：{message}"
    if status_code == 400:
        if field_cn:
            return f"请求参数错误：{field_cn} 不符合要求。"
        return f"请求参数错误：{message}"
    if status_code == 401:
        return "鉴权失败：API Key 无效、已失效，或 Bearer 格式不正确。"
    if status_code == 403:
        return "没有权限调用当前模型，请检查账号权限或模型接入点。"
    if status_code == 404:
        return "接口地址或模型接入点不存在，请检查配置。"
    if status_code == 429:
        return "请求受限：触发限流、并发限制或额度限制。"
    if status_code >= 500:
        return "方舟服务暂时异常，请稍后重试。"
    return message or "请求失败，请检查模型接入点、鉴权配置和入参。"


def is_prompt_temporary_400(error_info):
    code = clean_text(error_info.get("code", "")).lower()
    message = clean_text(error_info.get("message", "")).lower()
    param = clean_text(error_info.get("param", "")).lower()
    merged = f"{code} {message} {param}"

    hard_stop_markers = [
        "content policy", "safety", "sensitive", "violation",
        "image", "duration", "generate_audio", "camera_fixed",
        "watermark", "ratio", "resolution", "seed", "model",
        "resource not found", "download", "fetch"
    ]
    if any(marker in merged for marker in hard_stop_markers):
        return False

    text_field_markers = ["prompt", "content", "text"]
    invalid_markers = ["invalid", "not valid", "not supported", "parameter", "参数错误", "不符合"]
    has_text_field = any(marker in merged for marker in text_field_markers)
    has_invalid_signal = any(marker in merged for marker in invalid_markers)
    return has_text_field and has_invalid_signal


def make_fail(msg, error_code="", error_field=""):
    return {
        "success": False,
        "msg": msg,
        "task_id": "",
        "error_code": error_code,
        "error_field": error_field,
        "first_frame_tos_url": "",
        "last_frame_tos_url": ""
    }


def is_valid_api_key(api_key):
    api_key = clean_text(api_key)
    if not api_key or api_key == "Bearer":
        return False
    if "在这里填" in api_key or "请替换" in api_key:
        return False
    return True


# =========================================================
# 图片转存 TOS
# =========================================================

def is_tos_own_url(url):
    if not url:
        return False
    return "tos-cn-beijing.volces.com" in url


def download_image(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        if resp.status_code != 200:
            return None, f"下载失败，HTTP 状态码 {resp.status_code}"
        content = resp.content
        if len(content) < MIN_IMAGE_SIZE_KB * 1024:
            return None, f"下载文件过小（{len(content)}字节），可能不是有效图片"
        return content, ""
    except Exception as e:
        return None, f"下载异常：{str(e)}"


def upload_to_tos(content, object_key):
    last_error = ""
    for tos_conf in TOS_CONFIGS:
        tos_name = clean_text(tos_conf.get("name", ""))
        ak = clean_text(tos_conf.get("ak", ""))
        sk = clean_text(tos_conf.get("sk", ""))
        bucket = clean_text(tos_conf.get("bucket", ""))
        endpoint = clean_text(tos_conf.get("endpoint", ""))
        region = clean_text(tos_conf.get("region", ""))

        if not ak or not sk or not bucket:
            last_error = f"{tos_name} 配置不完整"
            continue

        for retry in range(2):
            try:
                tos_client = tos.TosClientV2(ak, sk, endpoint, region)
                tos_client.put_object(
                    bucket,
                    object_key,
                    content=content,
                    content_type="image/png"
                )
                public_url = f"https://{bucket}.tos-cn-beijing.volces.com/{object_key}"
                return public_url, tos_name, ""
            except Exception as e:
                last_error = f"{tos_name} 上传失败：{str(e)}"
                if retry == 0:
                    time.sleep(1)
                continue
    return "", "", last_error


def process_image_url(raw_url):
    raw_url = normalize_valid_image_url(raw_url)
    if not raw_url:
        return "", "", ""
    if not ENABLE_TOS_UPLOAD:
        return raw_url, "", ""
    if is_tos_own_url(raw_url):
        return raw_url, "已是TOS链接，跳过上传", ""

    url_hash = hashlib.md5(raw_url.encode("utf-8")).hexdigest()
    object_key = f"{TOS_PATH_PREFIX}/{url_hash}.png"

    img_content, download_err = download_image(raw_url)
    if download_err:
        return "", "", download_err

    tos_url, tos_name, upload_err = upload_to_tos(img_content, object_key)
    if upload_err:
        return "", "", f"图片转存TOS失败：{upload_err}"

    return tos_url, f"已转存至 {tos_name}", ""


# =========================================================
# 创建后预检：检查是否为账号限额失败
# =========================================================

def pre_check_task_status(task_id, api_key, wait_seconds):
    if wait_seconds <= 0:
        return True, ""

    time.sleep(wait_seconds)

    query_url = f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json"
    }

    try:
        resp = requests.get(query_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return True, ""

        task_data = resp.json()
        status = clean_text(task_data.get("status", "")).lower()

        if status in ["succeeded", "running", "pending", "queued"]:
            return True, ""

        if status in ["failed", "error", "cancelled", "canceled"]:
            error_data = task_data.get("error", {})
            error_msg = ""
            if isinstance(error_data, dict):
                error_msg = clean_text(error_data.get("message", ""))
            else:
                error_msg = clean_text(error_data)

            if is_account_level_error(error_msg, error_info=error_data):
                return False, error_msg
            else:
                return True, ""

    except Exception:
        return True, ""

    return True, ""


# =========================================================
# 主函数
# =========================================================

def _iter_model_auth_candidates(selected_model):
    """按“模型优先级 > 四套鉴权顺序”生成创建候选。

    自动/1.5 模式先尝试所有配置的 1.5，再在额度、权限或接入点不可用
    时进入 1.0；显式选择 1.0 时不调用 1.5。这样不会因为第一套鉴权
    暂时没有 1.5 额度，就跳过仍可用的其它 1.5 鉴权。
    """
    phases = [("seedance_1_5_pro", "model_ep_1_5")]
    if selected_model == "seedance_1_0_pro":
        phases = [("seedance_1_0_pro", "model_ep_1_0")]
    else:
        phases.append(("seedance_1_0_pro", "model_ep_1_0"))
    for model_name, model_field in phases:
        for current_auth in AUTH_CONFIGS:
            yield model_name, model_field, current_auth

def handler(args):
    input_data = getattr(args, "input", None)
    if input_data is None:
        input_data = getattr(args, "params", {})
    if isinstance(input_data, dict) and isinstance(input_data.get("_input"), dict):
        input_data = input_data["_input"]

    # 读取参数
    prompt = clean_text(get_value(input_data, "prompt", ""))
    duration = normalize_duration(get_value(input_data, "duration", DEFAULT_DURATION), DEFAULT_DURATION)
    ratio = clean_text(get_value(input_data, "ratio", ""))
    resolution = clean_text(get_value(input_data, "resolution", ""))
    seed = get_value(input_data, "seed", DEFAULT_SEED)
    generate_audio = parse_bool(get_value(input_data, "generate_audio", DEFAULT_GENERATE_AUDIO), DEFAULT_GENERATE_AUDIO)
    draft = parse_bool(get_value(input_data, "draft", DEFAULT_DRAFT), DEFAULT_DRAFT)
    watermark = parse_bool(get_value(input_data, "watermark", DEFAULT_WATERMARK), DEFAULT_WATERMARK)
    camera_fixed = parse_bool(get_value(input_data, "camera_fixed", DEFAULT_CAMERA_FIXED), DEFAULT_CAMERA_FIXED)
    pre_check_wait = int(get_value(input_data, "pre_check_wait", DEFAULT_PRE_CHECK_WAIT))
    try:
        video_model = normalize_video_model(get_value(input_data, "video_model", "auto"))
    except ValueError as error:
        return make_fail(str(error), "UNSUPPORTED_MODEL", "video_model")

    # 图片转存
    raw_first = normalize_valid_image_url(get_value(input_data, "first_frame_url", ""))
    raw_last = normalize_valid_image_url(get_value(input_data, "last_frame_url", ""))

    first_frame_url, first_tip, first_err = process_image_url(raw_first)
    if first_err:
        return make_fail(first_err, "IMAGE_DOWNLOAD_FAILED", "first_frame_url")

    last_frame_url, last_tip, last_err = process_image_url(raw_last)
    if last_err:
        return make_fail(last_err, "IMAGE_DOWNLOAD_FAILED", "last_frame_url")

    # 入参校验
    if not prompt and not first_frame_url:
        return make_fail(
            "参数缺失：必须填写 prompt 或 first_frame_url（首帧图片地址）。",
            "MISSING_INPUT",
            "prompt / first_frame_url"
        )

    # ========== 修复：按官方规范组装 content 数组，补充 role 字段 ==========
    content = []
    # 1. 文本提示词
    if prompt:
        content.append({"type": "text", "text": prompt})
    # 2. 首帧图（带官方标准 role）
    if first_frame_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": first_frame_url},
            "role": "first_frame"
        })
    # 3. 尾帧图（带官方标准 role，有值才加）
    if last_frame_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": last_frame_url},
            "role": "last_frame"
        })

    # =========================================================
    # 多账号轮询创建任务
    # =========================================================

    last_error_msg = ""
    last_error_code = "ALL_AUTH_FAILED"
    last_error_field = ""

    for selected_model, model_field, current_auth in _iter_model_auth_candidates(video_model):
        account_name = clean_text(current_auth.get("name", "未命名账号"))
        api_key = clean_text(current_auth.get("api_key", ""))
        model_ep = clean_text(
            current_auth.get(model_field)
            or current_auth.get("model_ep" if model_field == "model_ep_1_0" else "", "")
        )

        if not is_valid_api_key(api_key):
            last_error_msg = f"{account_name} 未配置 API Key。"
            last_error_code = "INVALID_API_KEY"
            continue
        if not model_ep:
            last_error_msg = f"{account_name} 未配置模型接入点。"
            last_error_code = "MISSING_MODEL_EP"
            continue

        headers = {
            "Authorization": api_key,
            "Content-Type": "application/json"
        }

        request_data = {
            "model": model_ep,
            "content": content,
            "duration": duration,
            "generate_audio": generate_audio,
            "draft": draft,
            "watermark": watermark,
            "camera_fixed": camera_fixed
        }

        if seed is not None and clean_text(seed) not in ["", "-1"]:
            request_data["seed"] = seed

        # 纯文生视频才传比例和分辨率
        if not first_frame_url and not last_frame_url:
            if ratio:
                request_data["ratio"] = ratio
            if resolution:
                request_data["resolution"] = resolution

        prompt_retry_delay = PROMPT_400_RETRY_DELAY
        transient_retry_delay = TRANSIENT_RETRY_DELAY
        account_failed = False

        for attempt_index in range(MAX_ATTEMPTS_PER_AUTH):
            attempt_no = attempt_index + 1
            try:
                resp = requests.post(
                    API_URL,
                    headers=headers,
                    json=request_data,
                    timeout=REQUEST_TIMEOUT
                )

                # 创建接口报错
                if resp.status_code < 200 or resp.status_code >= 300:
                    error_info = get_error_info(resp)
                    error_msg = translate_error(resp.status_code, error_info)
                    error_code = clean_text(error_info.get("code", "")) or f"HTTP_{resp.status_code}"
                    error_field = get_field_name_cn(
                        error_info.get("param", ""),
                        error_info.get("message", "")
                    )

                    # 账号级错误：直接切下一个账号
                    if is_account_level_error(
                        error_msg,
                        status_code=resp.status_code,
                        error_info=error_info,
                    ):
                        last_error_msg = f"{account_name}：{error_msg}"
                        last_error_code = error_code
                        last_error_field = error_field
                        account_failed = True
                        break

                    # 提示词临时400：同账号重试
                    if resp.status_code == 400 and is_prompt_temporary_400(error_info):
                        if attempt_index < MAX_ATTEMPTS_PER_AUTH - 1:
                            time.sleep(prompt_retry_delay)
                            prompt_retry_delay *= PROMPT_400_RETRY_BACKOFF
                            continue
                        last_error_msg = f"{account_name}：提示词接口临时拒绝，已提交 {MAX_ATTEMPTS_PER_AUTH} 次仍未创建成功。"
                        last_error_code = error_code or "PROMPT_TEMPORARY_REJECTED"
                        last_error_field = "提示词 content.text"
                        account_failed = True
                        break

                    # 其他参数错误：直接返回
                    return make_fail(error_msg, error_code or "INVALID_PARAMETER", error_field)

                # 创建成功
                try:
                    data = resp.json()
                except Exception:
                    return make_fail(
                        "接口返回成功，但返回内容无法解析。",
                        "INVALID_RESPONSE"
                    )

                task_id = clean_text(data.get("id") or data.get("task_id", ""))
                if not task_id:
                    return make_fail(
                        "接口已接受请求，但没有返回任务 ID。为避免重复提交，插件未再次提交。",
                        "MISSING_TASK_ID"
                    )

                # 预检账号限额
                check_pass, check_err = pre_check_task_status(task_id, api_key, pre_check_wait)
                if not check_pass:
                    last_error_msg = f"{account_name}：{check_err}"
                    last_error_code = "SetLimitExceeded"
                    last_error_field = "账号推理限额"
                    account_failed = True
                    break

                # 正常返回
                retry_note = ""
                if attempt_no > 1:
                    retry_note = f"经过 {attempt_no} 次提交后成功，"

                upload_note = ""
                if first_tip:
                    upload_note += f"首帧{first_tip}；"
                if last_tip:
                    upload_note += f"尾帧{last_tip}"

                # 显式 1.0 是旧节点兼容路径，保持原 msg 契约；自动/1.5
                # 路径才把实际模型写入提示，便于页面确认是否发生降级。
                usage_note = (
                    f"已使用：{account_name}"
                    if video_model == "seedance_1_0_pro"
                    else f"已使用：{selected_model} / {account_name}"
                )

                return {
                    "success": True,
                    "msg": f"任务创建成功，{retry_note}{usage_note}。{upload_note}",
                    "task_id": task_id,
                    "generate_audio": generate_audio,
                    "first_frame_tos_url": first_frame_url,
                    "last_frame_tos_url": last_frame_url,
                    "tip": "已转交下游任务查询节点。"
                }

            except requests.exceptions.Timeout:
                if attempt_index < MAX_ATTEMPTS_PER_AUTH - 1:
                    time.sleep(transient_retry_delay)
                    transient_retry_delay *= TRANSIENT_RETRY_BACKOFF
                    continue
                last_error_msg = f"{account_name}：请求超时，接口在 {REQUEST_TIMEOUT} 秒内未响应。"
                last_error_code = "REQUEST_TIMEOUT"
                last_error_field = ""
                account_failed = True
                break

            except requests.exceptions.ConnectionError:
                if attempt_index < MAX_ATTEMPTS_PER_AUTH - 1:
                    time.sleep(transient_retry_delay)
                    transient_retry_delay *= TRANSIENT_RETRY_BACKOFF
                    continue
                last_error_msg = f"{account_name}：网络连接失败，请检查服务器网络和接口地址。"
                last_error_code = "CONNECTION_ERROR"
                last_error_field = ""
                account_failed = True
                break

            except Exception as exc:
                last_error_msg = f"{account_name}：插件执行异常：{clean_text(exc)}"
                last_error_code = "PLUGIN_EXCEPTION"
                last_error_field = ""
                account_failed = True
                break

    return make_fail(
        f"所有鉴权配置均无法完成任务创建。最后错误：{last_error_msg}",
        last_error_code or "ALL_AUTH_FAILED",
        last_error_field
    )



class VideoGenerateTransportRequired(RuntimeError):
    """未提供离线传输层时，阻止真实网络请求。"""


def run_video_generate(params, *, requests_client=None, sleep=None):
    """运行脱敏原始逻辑；requests_client 为空时不访问外部服务。"""
    if requests_client is None:
        raise VideoGenerateTransportRequired("未配置 video_generate 传输层；当前只允许离线契约测试")
    import types
    original_requests = globals().get("requests")
    original_time = globals().get("time")
    sleep_fn = sleep or (lambda _seconds: None)
    time_proxy = types.SimpleNamespace(sleep=sleep_fn, time=original_time.time)
    globals()["requests"] = requests_client
    globals()["time"] = time_proxy
    try:
        return handler(types.SimpleNamespace(input=params))
    finally:
        globals()["requests"] = original_requests
        globals()["time"] = original_time


def to_coze_node_output(raw_result):
    """投影到工作流 YAML 声明的四个输出字段，忽略插件源码的扩展字段。"""
    return {
        "msg": raw_result.get("msg"),
        "success": raw_result.get("success"),
        "task_id": raw_result.get("task_id"),
        "tip": raw_result.get("tip"),
    }
