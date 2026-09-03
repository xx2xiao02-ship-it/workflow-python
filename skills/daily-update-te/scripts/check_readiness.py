"""检查 Daily_Update_Te Skill 的本地与外部依赖就绪度。

默认只做本地文件检查，不调用网络、不启动服务、不读取或打印任何密钥。
使用 ``--probe-capcut`` 时，才会对用户显式配置的 CapCut Mate 地址发起一次
``/openapi.json`` 请求；响应只保留状态和服务标题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, ProxyHandler, build_opener, urlopen


WORKFLOW_ARCHIVE = "Workflow-Daily_Update_Te-draft-8364.zip"
PURE_NODES = (
    "timeline_planning",
    "end_frame_extension",
    "prompt_generation",
    "video_infos",
    "scene_type_recognition",
    "host_task_assembly",
    "bgm_task_assembly",
    "video_data_aggregation",
    "keyframes_infos",
)
TRANSPORT_NODES = {
    "directors_v2": "需要当前附件版本的 DIRECTORS_V2_API_KEY 和能让 output_5_5 返回 8364 四字段 JSON 的 system_prompt/插件配置",
    "shot_refinement": "103964/127095 编排与代码已接入；需要内部 197742 镜头精细化导演 LLM transport",
    "shot_visual_arrangement": "源码逻辑已接入；需要全局规划模型和首帧资料模型 transport",
    "create_draft/save_draft": "需要可访问的 CapCut Mate 服务",
    "draft_writes": "需要 CapCut Mate 的 add_videos/add_audios/add_captions/add_keyframes/add_effects transport",
    "image_tasks": "需要首帧图像任务 API 及查询 transport",
    "seedance_video": "需要视频生成 API、任务查询和视频转存配置",
    "host_infinite_talk": "InfiniteTalk 创建/查询以及 Host 音频/视频切分 transport 已接入；仍需要 INFINITETALK_API_KEY、TOS/ffmpeg 配置和音频合并真实执行器",
    "ark_tts": "官方单向流式 TTS transport 已接入；需要 X-Api-Key、音色目录中的已验证 speaker/resource 绑定和 TOS 音频发布配置",
    "bgm_merge": "需要独立 BGM 时间轴融合器 merge_bgm_timeline transport",
}
CODE_GAPS = {
    "179757": "转场音效：8364 导出仍是默认示例代码，必须提供真实代码或确认该节点仅为占位",
}

TOP_LEVEL_NODE_COUNT = 52
FULL_NODE_COUNT = 61
TOP_LEVEL_EDGE_COUNT = 56
INTERNAL_NODE_IDS = {
    "197742", "127095", "142186", "106538", "159953",
    "181639", "186546", "139653", "106157",
}

# 与 references/workflow-map.md 保持同一顺序。状态只描述当前证据，不等同于
# 整条工作流成功：尤其是 transport_mapped 只代表字段和路由已接入。
NODE_STATUS_DEFINITIONS = [
    ("100001", "开始", "orchestration_mapped", "读取 text/host_image/ref_images"),
    ("182422", "create_draft", "verified_live", "CapCut Mate create_draft 已真实调用"),
    ("129109", "directors_v2", "transport_implemented_live_pending", "当前附件版 transport 已实现；缺本机 API 配置"),
    ("103964", "镜头精细化", "transport_implemented_live_pending", "批处理与 127095 code transport 已接入；缺 197742 LLM transport"),
    ("197742", "镜头精细化导演", "transport_pending", "需要镜头精细化内部 LLM transport"),
    ("127095", "时长计算时间线规划", "offline_verified", "代码级和真实 9 项 fixture 对照"),
    ("102833", "全文视觉意图导演", "contract_implemented_transport_pending", "输入/输出契约适配器已实现；需要真实 LLM transport"),
    ("184922", "首帧生成", "transport_implemented_live_pending", "aishuch create/query transport 已实现；缺真实 API 配置和批处理验收"),
    ("142186", "create_image_task", "transport_implemented_live_pending", "aishuch 创建任务 transport 已实现；缺真实 API 配置"),
    ("106538", "get_task_result", "transport_implemented_live_pending", "aishuch 查询任务 transport 已实现；缺真实 API 配置和真实轮询验收"),
    ("178142", "循环", "orchestration_mapped_transport_pending", "循环顺序已接入编排器；内部官方 TTS transport 等待本机配置"),
    ("159953", "speech_synthesis", "transport_implemented_live_pending", "官方 unidirectional Chunked TTS transport、Base64 合并和 TOS 发布边界已实现；等待目录音色、鉴权和真实音频验收"),
    ("165901", "时间线", "verified_live_route", "audio_timelines(links) 已真实返回并自动接入主编排器；仍可显式覆盖 runner"),
    ("105880", "TTS-STT 字幕与坑位规划", "transport_implemented_live_pending", "CapCut STT + Mini + 2.5–5 秒坑位规划已接入；等待真实服务验收"),
    ("111882", "字幕", "verified_live_route", "caption_infos 已按 texts/timelines 真实返回 1 项 infos"),
    ("116616", "Shot_Visual_Arrangement", "transport_implemented_live_pending", "源码逻辑、契约测试和主编排注入点已接入；缺全局规划与首帧资料模型 transport"),
    ("195692", "生成视频提示词撰写", "transport_implemented_live_pending", "批处理契约已接入；缺内部 181639 模型 transport"),
    ("181639", "图生视频提示词导演", "transport_implemented_live_pending", "请求/输出契约已实现；缺真实模型 transport"),
    ("170263", "首尾帧顺延", "offline_verified", "代码级和真实页面结构证据"),
    ("115723", "AIGC动画", "verified_live_route", "离线实现已对照；漫画撕纸参数的 video_infos 已真实返回 1 项"),
    ("156199", "画面类型识别", "offline_verified", "8364 Python 导出代码移植、测试并接入主编排器"),
    ("139488", "host 镜头识别", "contract_implemented_transport_pending", "YAML 输入/输出、候选 idx 约束和主编排注入点已实现；需要 Host LLM transport"),
    ("117861", "Host 任务组装", "offline_verified", "8364 Python 导出代码移植、测试并接入主编排器"),
    ("175652", "host_audio_split", "transport_implemented_live_pending", "用户提供源码已接入 ffmpeg/TOS 媒体 transport；缺本机媒体配置与真实媒体验收"),
    ("143380", "merge_audio_urls", "contract_implemented_transport_pending", "用户提供源码已登记并接入输入输出契约；默认 ffmpeg 合并执行器与真实 TOS 验收待补"),
    ("173596", "Create_infinitetalk_Task", "transport_implemented_live_pending", "用户提供源码已接入 RunningHub 创建/上传 transport；缺真实 API Key 与媒体验收"),
    ("118959", "time_sleep", "offline_verified", "175 秒整数契约的本地 sleeper 实现已测试；InfiniteTalk 编排尚未接入"),
    ("121815", "变量聚合", "contract_implemented_orchestration_pending", "YAML mergeGroups 顺序和非空 output_url 选择契约已实现；InfiniteTalk 分支编排仍待接入"),
    ("137386", "query_infinitetalk_task", "transport_implemented_live_pending", "用户提供源码已接入 RunningHub 查询/轮询 transport；缺真实 API Key 与任务验收"),
    ("1178381", "query_infinitetalk_task_1", "transport_implemented_live_pending", "共用 RunningHub 查询/轮询 transport；缺真实 API Key 与任务验收"),
    ("160040", "video_split_by_timeline_2", "transport_implemented_live_pending", "用户提供源码已接入 ffmpeg/TOS 视频切分 transport；缺本机媒体配置与真实媒体验收"),
    ("152553", "HOST数字人", "verified_live_route", "胶片定格参数的 video_infos 已真实返回 1 项；Host 上游同输入待验收"),
    ("192311", "BGM 任务组装", "offline_verified", "8364 Python 导出代码移植并测试"),
    ("141288", "批处理_2", "contract_implemented_transport_pending", "BGM 任务逐项顺序适配已实现；缺少真实 gen_bgm transport"),
    ("186546", "gen_bgm", "transport_pending", "未找到独立官方 BGM 生成接口或插件源码；不会复用 TTS transport"),
    ("165818", "merge_bgm_timeline", "transport_pending", "契约适配器已建立；独立 BGM 时间轴融合器仍缺源码/接口，不能误用 CapCut audio_timelines"),
    ("116592", "背景音乐", "verified_live_route", "volume=0.4 的 audio_infos 已真实返回 1 项"),
    ("191683", "解说", "verified_live_route", "人声增强、volume=1.2 的 audio_infos 已真实返回 1 项"),
    ("135313", "解说*", "verified_live_route", "add_audios 已用真实 MP3 写入 1 个音频素材；8364 同输入等价待上游完成"),
    ("110576", "背景音乐*", "verified_live_route", "共用 add_audios 路由已真实写入；8364 BGM 同输入等价待上游完成"),
    ("174651", "AIGC动画*", "verified_live_route", "add_videos 已用真实 MP4 写入 1 个视频素材；8364 同输入等价待上游完成"),
    ("116930", "数字人*", "verified_live_route", "共用 add_videos 路由已有真实证据；Coze 18 项结构已审计；Host 同输入本地写入待验收"),
    ("179989", "字幕*", "verified_live_route", "add_captions 真实最小链路已调用"),
    ("143635", "save_draft", "verified_live", "CapCut Mate save_draft 已真实调用"),
    ("113743", "提示词生成", "offline_verified", "synthetic 边界测试通过"),
    ("110697", "批处理", "transport_implemented_live_pending", "批处理编排已实现；缺 video_generate/video_query 真实配置和验收"),
    ("139653", "video_generate", "transport_implemented_live_pending", "原插件逻辑和可注入 requests transport 已保留；缺真实 Seedance 配置和验收"),
    ("106157", "video_query", "transport_implemented_live_pending", "原插件逻辑和可注入 requests transport 已保留；缺真实 Seedance/TOS 配置和验收"),
    ("151394", "keyframes_infos", "verified_live_route", "13 项代码级等价通过；UNIFORM_SCALE 0|100 / 1|1.15 已真实生成 2 个关键帧"),
    ("1693143", "query_infinitetalk_task_2", "transport_implemented_live_pending", "共用 RunningHub 查询/轮询 transport；缺真实 API Key 与任务验收"),
    ("115137", "add_keyframes", "verified_live_route", "12 个解析场景代码级等价；2 个关键帧已真实写入视频 segment"),
    ("1904923", "关键帧", "verified_live_route", "共用 keyframes_infos 契约已按 8364 常量真实验证"),
    ("1962357", "关键帧+", "verified_live_route", "共用严格 add_keyframes 契约与真实写入路由"),
    ("187358", "add_effects", "verified_live_route", "暗角 effect 已真实写入并返回 1 个 effect_id/segment_id"),
    ("197721", "effect_infos", "verified_live_route", "暗角已真实生成 1 项 infos"),
    ("1922689", "add_effects_1", "verified_live_route", "蓝色丝印 effect 已真实写入并返回 1 个 effect_id/segment_id"),
    ("1765956", "effect_infos_2", "verified_live_route", "蓝色丝印已真实生成 1 项 infos"),
    ("167309", "视频数据汇总", "offline_verified", "JavaScript 逻辑已移植并测试"),
    ("179757", "转场音效", "source_placeholder", CODE_GAPS["179757"]),
    ("191914", "audio_infos", "contract_inconsistent", "8364 YAML 没有 node_inputs，但当前 audio_infos 接口强制要求 mp3_urls/timelines；等待 179757 真实代码和映射"),
    ("900001", "结束", "contract_implemented_orchestration_pending", "已实现 143635.draft_url -> content 契约；全链到 save_draft 后才能执行"),
]


def build_node_status() -> list[dict[str, str]]:
    return [
        {
            "node_id": node_id,
            "name": name,
            "status": status,
            "evidence": evidence,
            "scope": "internal" if node_id in INTERNAL_NODE_IDS else "top_level",
        }
        for node_id, name, status, evidence in NODE_STATUS_DEFINITIONS
    ]


def _walk_node_records(value: Any, output: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("id"), (str, int)) and isinstance(value.get("type"), str):
            output.append({
                "node_id": str(value["id"]),
                "type": value["type"],
                "name": str(value.get("title", "")),
            })
        for child in value.values():
            _walk_node_records(child, output)
    elif isinstance(value, list):
        for child in value:
            _walk_node_records(child, output)


def inspect_workflow_archive(path: Path) -> dict[str, Any]:
    """读取当前 8364 压缩包的节点/边元数据，不执行工作流。"""

    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    try:
        with zipfile.ZipFile(path) as archive_file:
            yaml_names = [
                name for name in archive_file.namelist()
                if name.lower().endswith((".yaml", ".yml"))
            ]
            if not yaml_names:
                return {**result, "parse_status": "yaml_not_found"}
            yaml_name = next(
                (name for name in yaml_names if name.lower().endswith(".yaml")),
                yaml_names[0],
            )
            raw = archive_file.read(yaml_name)
        result.update({
            "yaml_path": yaml_name,
            "yaml_sha256": hashlib.sha256(raw).hexdigest(),
        })
        try:
            import yaml  # type: ignore
        except ImportError:
            return {**result, "parse_status": "pyyaml_unavailable"}
        document = yaml.safe_load(raw)
        if not isinstance(document, dict):
            return {**result, "parse_status": "yaml_root_not_object"}
        records: list[dict[str, str]] = []
        _walk_node_records(document, records)
        top_level_nodes = document.get("nodes")
        edges = document.get("edges")
        ids = [item["node_id"] for item in records]
        result.update({
            "parse_status": "ok",
            "top_level_node_count": len(top_level_nodes) if isinstance(top_level_nodes, list) else 0,
            "full_node_count": len(records),
            "edge_count": len(edges) if isinstance(edges, list) else 0,
            "node_ids_unique": len(ids) == len(set(ids)),
            "node_ids": ids,
        })
        return result
    except (OSError, zipfile.BadZipFile) as exc:
        return {**result, "parse_status": "archive_error", "error_type": type(exc).__name__}


def find_root(configured: str | None = None) -> Path:
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((Path.cwd(), Path(__file__).resolve().parents[3]))
    for candidate in candidates:
        if (candidate / "src" / "workflow_1256").is_dir():
            return candidate.resolve()
    raise SystemExit("找不到工作流项目根目录；请在项目根目录执行，或传入 --root。")


def inspect_tts_voice_catalog(root: Path) -> dict[str, Any]:
    """只读检查 TTS 是否有目录绑定的可选音色，不把全局 speaker 环境变量当必填。

    ``ARK_TTS_SPEAKER_ID`` 在迁移期只用于一致性校验；新任务从唯一音色目录
    解析 speaker/resource。目录不存在时读取源码内置种子，绝不因 readiness
    检查而写入运行时文件。
    """

    configured = os.environ.get("VOICE_CATALOG_PATH", "").strip()
    path = Path(configured).expanduser() if configured else (
        root / ".runtime-governance-live" / "data" / "VideoProductionConsole" / "voice_catalog.json"
    )
    source = "runtime" if path.is_file() else "built_in_seed"
    try:
        source_path = str(root / "src")
        if source_path not in sys.path:
            sys.path.insert(0, source_path)
        from workflow_1256.voice_catalog import DEFAULT_VOICE_CATALOG, normalize_voice_catalog
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = DEFAULT_VOICE_CATALOG
        if not isinstance(value, dict):
            raise ValueError("音色目录根节点不是对象")
        # 使用同一解析器的标准化规则，避免 readiness 自己复制一套目录契约。
        normalized = normalize_voice_catalog(value)
        voices = normalized.get("voices", [])
        selectable = [
            item for item in voices
            if isinstance(item, dict)
            and item.get("enabled") is True
            and item.get("verified") is True
            and str(item.get("speaker_id") or "").strip()
        ]
        return {
            "path": str(path),
            "source": source,
            "exists": path.is_file(),
            "voice_count": len(voices),
            "selectable_count": len(selectable),
            "status": "ready" if selectable else "missing_verified_voice",
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ImportError, ValueError) as exc:
        return {
            "path": str(path),
            "source": source,
            "exists": path.is_file(),
            "voice_count": 0,
            "selectable_count": 0,
            "status": "invalid",
            "error_type": type(exc).__name__,
        }


def _check_file(path: Path) -> dict[str, Any]:
    return {"path": str(path), "exists": path.is_file()}


def _capcut_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/") + "/"
    if base.rstrip("/").endswith("/openapi/capcut-mate/v1"):
        return urljoin(base, "../../../../openapi.json")
    return urljoin(base, "openapi.json")


def _is_loopback_url(url: str) -> bool:
    """本机 CapCut Mate 探测不应绕到系统代理，避免未启动时无效等待。"""
    host = (urlparse(url).hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _capcut_probe_timeout(base_url: str) -> float:
    """本机探测快速失败，远端地址保留较宽松的网络等待时间。"""
    raw = os.environ.get("CAPCUT_MATE_PROBE_TIMEOUT", "").strip()
    if raw:
        try:
            return max(0.25, min(float(raw), 30.0))
        except ValueError:
            pass
    return 1.5 if _is_loopback_url(base_url) else 5.0


def _loopback_port_is_reachable(url: str, timeout: float = 0.25) -> bool:
    """先做本机端口探测，避免 HTTP 层在未启动时额外等待。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def probe_capcut(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    url = _capcut_url(base_url)
    request = Request(url, headers={"Accept": "application/json"})
    if _is_loopback_url(url) and not _loopback_port_is_reachable(url):
        return {"configured": True, "reachable": False, "error_type": "ConnectionRefusedError"}
    try:
        # 本机服务不可达时，禁用系统代理可避免把 127.0.0.1 请求送到代理后等待。
        opener = build_opener(ProxyHandler({})) if _is_loopback_url(url) else None
        response_context = opener.open(request, timeout=timeout) if opener else urlopen(request, timeout=timeout)
        with response_context as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            return {
                "configured": True,
                "reachable": response.status < 400,
                "status": response.status,
                "title": payload.get("info", {}).get("title") if isinstance(payload, dict) else None,
            }
    except HTTPError as exc:
        return {"configured": True, "reachable": False, "status": exc.code}
    except (URLError, TimeoutError, OSError) as exc:
        return {"configured": True, "reachable": False, "error_type": type(exc).__name__}


def build_report(
    root: Path,
    *,
    archive: Path | None = None,
    probe: bool = False,
    base_url: str | None = None,
) -> dict[str, Any]:
    archive = archive or (Path.home() / "Downloads" / WORKFLOW_ARCHIVE)
    skill = root / "skills" / "daily-update-te"
    source = root / "src" / "workflow_1256"

    pure = []
    for name in PURE_NODES:
        module = source / f"{name}.py"
        pure.append({"name": name, "implemented": module.is_file()})

    capcut: dict[str, Any]
    configured_url = (base_url or os.environ.get("CAPCUT_MATE_BASE_URL") or "").strip()
    if probe and configured_url:
        capcut = probe_capcut(configured_url)
    else:
        capcut = {"configured": bool(configured_url), "probed": False}

    node_status = build_node_status()
    status_ids = {item["node_id"] for item in node_status}
    archive_report = inspect_workflow_archive(archive)
    archive_valid = (
        archive_report.get("parse_status") == "ok"
        and archive_report.get("top_level_node_count") == TOP_LEVEL_NODE_COUNT
        and archive_report.get("full_node_count") == FULL_NODE_COUNT
        and archive_report.get("edge_count") == TOP_LEVEL_EDGE_COUNT
        and archive_report.get("node_ids_unique") is True
        and set(archive_report.get("node_ids", [])) == status_ids
    )

    local_checks = {
        "archive": _check_file(archive),
        "skill": _check_file(skill / "SKILL.md"),
        "workflow_map": _check_file(skill / "references" / "workflow-map.md"),
        "node_contracts": _check_file(skill / "references" / "node-contracts.md"),
        "runner": _check_file(skill / "scripts" / "run_node_contract.py"),
        "visual_intent_contract": _check_file(source / "visual_intent.py"),
        "speech_synthesis_contract": _check_file(source / "speech_synthesis.py"),
        "host_shot_recognition_contract": _check_file(source / "host_shot_recognition.py"),
        "time_sleep_contract": _check_file(source / "time_sleep.py"),
        "project_root_helper": _check_file(skill / "scripts" / "project_root.py"),
        "workflow_end_contract": _check_file(source / "workflow_end.py"),
        "variable_merge_contract": _check_file(source / "variable_merge.py"),
        "add_keyframes_contract": _check_file(source / "add_keyframes.py"),
        "source_package": {"path": str(source), "exists": source.is_dir()},
    }
    local_ready = (
        all(item["exists"] for item in local_checks.values())
        and all(item["implemented"] for item in pure)
        and archive_valid
    )
    capcut_mate_ready = bool(capcut.get("reachable"))
    node_map_valid = (
        len(node_status) == FULL_NODE_COUNT
        and len(status_ids) == FULL_NODE_COUNT
        and archive_valid
    )
    workflow_ready = all(
        item["status"] in {"offline_verified", "offline_verified_transport_mapped", "verified_live", "verified_live_route", "orchestration_mapped"}
        for item in node_status
    )
    status_counts: dict[str, int] = {}
    for item in node_status:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

    tts_voice_catalog = inspect_tts_voice_catalog(root)
    user_actions: list[dict[str, Any]] = []
    if not os.environ.get("DIRECTORS_V2_API_KEY", "").strip():
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["129109"],
            "request": "在运行本机 PowerShell 设置 DIRECTORS_V2_API_KEY；不要把密钥发到聊天或写入文件。",
            "verification": "重新运行工作流后，directors_v2 不再因缺少密钥阻断。",
        })
    if not os.environ.get("DIRECTORS_V2_SYSTEM_PROMPT", "").strip():
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["129109"],
            "request": "在运行本机 PowerShell 设置当前插件实际使用的 DIRECTORS_V2_SYSTEM_PROMPT，并要求输出 8364 的四字段 JSON。",
            "verification": "output_5_5 能解析为 director_plan、ok、segment_beats、segments。",
        })
    if not os.environ.get("AISHUCH_API_KEY", "").strip():
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["184922", "142186", "106538"],
            "request": "在运行本机 PowerShell 设置 AISHUCH_API_KEY；不要把密钥发到聊天或写入文件。",
            "verification": "首帧任务创建和查询 transport 可以进行真实请求。",
        })
    if not os.environ.get("INFINITETALK_API_KEY", "").strip():
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["173596", "137386", "1178381", "1693143"],
            "request": "在本机 PowerShell 设置 INFINITETALK_API_KEY；不要把密钥发送到聊天或写入文件。",
            "verification": "RunningHub 创建、查询和轮询 transport 可以进行真实请求。",
        })
    if not (
        os.environ.get("ARK_TTS_API_KEY", "").strip()
        or os.environ.get("ARK_AUDIO_API_KEY", "").strip()
    ):
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["159953"],
            "request": "在本机 PowerShell 设置 ARK_TTS_API_KEY；不要把鉴权发到聊天或写入文件。",
            "verification": "TTS 请求能够通过 X-Api-Key 鉴权。",
        })
    if (
        not os.environ.get("ARK_TTS_SPEAKER_ID", "").strip()
        and int(tts_voice_catalog.get("selectable_count") or 0) == 0
    ):
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["159953"],
            "request": "在编导音色管理中登记并完成至少一个已验证 speaker_id/resource_id 绑定；ARK_TTS_SPEAKER_ID 仅用于迁移期一致性校验，不再作为新任务必填项。",
            "verification": "编导选择的 voice_key 能解析为目录中的真实 speaker_id 和 resource_id。",
        })
    if not (
        os.environ.get("ARK_TTS_TOS_ACCESS_KEY", "").strip()
        and os.environ.get("ARK_TTS_TOS_SECRET_KEY", "").strip()
        and os.environ.get("ARK_TTS_TOS_BUCKET", "").strip()
        and os.environ.get("ARK_TTS_TOS_ENDPOINT", "").strip()
        and os.environ.get("ARK_TTS_TOS_REGION", "").strip()
    ):
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["159953"],
            "request": "在本机配置 ARK_TTS_TOS_ACCESS_KEY/SECRET_KEY/BUCKET/ENDPOINT/REGION；不要把凭据发到聊天。",
            "verification": "Base64 音频能够发布为 CapCut Mate 可访问的 https URL。",
        })
    if not os.environ.get("SEEDANCE_PRIMARY_API_KEY", "").strip():
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["139653", "106157"],
            "request": "在运行本机 PowerShell 设置 SEEDANCE_PRIMARY_API_KEY；可选设置 SEEDANCE_BACKUP_API_KEY、SEEDANCE_THIRD_API_KEY、SEEDANCE_FOURTH_API_KEY。模型默认使用官方 Model ID，也可配置 SEEDANCE_MODEL_1_5_EP/SEEDANCE_MODEL_1_0_EP。不要把密钥发到聊天。",
            "verification": "video_generate 按 1.5 Pro 优先顺序创建 task_id，额度/权限级失败时进入 1.0 Pro；video_query 查询成功并返回 public_video_url 或明确失败分支。",
        })
    if not (
        os.environ.get("INFINITETALK_TOS_ACCESS_KEY", "").strip()
        and os.environ.get("INFINITETALK_TOS_SECRET_KEY", "").strip()
        and os.environ.get("INFINITETALK_TOS_BUCKET", "").strip()
    ):
        user_actions.append({
            "kind": "local_config",
            "node_ids": ["175652", "143380", "160040"],
            "request": "在本机 PowerShell 设置 INFINITETALK_TOS_ACCESS_KEY、INFINITETALK_TOS_SECRET_KEY、INFINITETALK_TOS_BUCKET；可选设置 INFINITETALK_FFMPEG_PATH。不要把凭据发到聊天。",
            "verification": "Host 音频切分、音频合并和视频切分可以使用真实媒体 URL 写入可访问 TOS URL。",
        })
    if "179757" in CODE_GAPS:
        user_actions.append({
            "kind": "source_or_decision",
            "node_ids": ["179757", "191914"],
            "request": "提供 179757 转场音效的真实代码，并说明它应如何给 191914 audio_infos 提供 mp3_urls/timelines；或明确确认这两个节点均为占位。",
            "verification": "179757 不再是默认示例，且 191914 获得满足接口必填字段的显式输入；或两节点被正式登记为无需执行。",
        })
    if not capcut_mate_ready:
        user_actions.append({
            "kind": "service",
            "node_ids": ["182422", "143635"],
            "request": "启动 CapCut Mate 并提供可访问的地址，例如 http://127.0.0.1:30000。",
            "verification": "--probe-capcut 能访问 /openapi.json。",
        })
    user_actions.append({
        "kind": "model_transport",
        "node_ids": [
            "197742", "102833", "116616", "181639", "139488",
        ],
        "request": "提供可用模型端点/鉴权与同输入测试配置；这些节点的 YAML 契约或源码逻辑已登记，不需要重复索要父级 batch 源码。",
        "verification": "各模型输出通过现有 schema/顺序校验，并推进到下一个真实节点。",
    })
    user_actions.append({
        "kind": "missing_plugin_source",
        "node_ids": ["165818"],
        "request": "提供独立 BGM 时间轴融合器 merge_bgm_timeline 的真实源码或官方接口；TTS 与 gen_bgm 已改用官方火山音频接口。",
        "verification": "merge_bgm_timeline 完成请求/响应契约测试，并执行真实音频裁剪、淡入淡出/交叉淡化验收。",
    })

    if not local_ready:
        interpretation = "本地 Skill 文件或离线节点仍不完整。"
    elif capcut.get("probed") is False:
        interpretation = (
            "本地契约入口已就绪；CapCut Mate 已配置但本轮未探测；"
            "全链路仍需补齐代码缺口并逐项注入 transport。"
            if capcut.get("configured")
            else "本地契约入口已就绪；CapCut Mate 本轮未配置且未探测；"
            "全链路仍需补齐代码缺口并逐项注入 transport。"
        )
    elif capcut_mate_ready:
        interpretation = (
            "本地契约入口已就绪；CapCut Mate 本轮探测可达；"
            "全链路仍需补齐代码缺口并逐项注入 transport。"
        )
    else:
        interpretation = (
            "本地契约入口已就绪；CapCut Mate 本轮探测不可达；"
            "全链路仍需补齐代码缺口并逐项注入 transport。"
        )

    return {
        "workflow": "Daily_Update_Te",
        "root": str(root),
        "local_ready": local_ready,
        "capcut_mate_ready": capcut_mate_ready,
        "workflow_ready": workflow_ready,
        "node_count": len(node_status),
        "top_level_node_count": archive_report.get("top_level_node_count"),
        "full_node_count": archive_report.get("full_node_count"),
        "node_map_valid": node_map_valid,
        "archive": archive_report,
        "local_checks": local_checks,
        "node_status_counts": status_counts,
        "node_status": node_status,
        "pure_python_nodes": pure,
        "tts_voice_catalog": tts_voice_catalog,
        "code_gaps": CODE_GAPS,
        "capcut_mate": capcut,
        "required_user_inputs": TRANSPORT_NODES,
        "user_actions": user_actions,
        "interpretation": interpretation,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Daily_Update_Te Skill 就绪度")
    parser.add_argument("--root", help="工作流项目根目录")
    parser.add_argument("--archive", type=Path, help="8364 工作流压缩包路径")
    parser.add_argument("--probe-capcut", action="store_true", help="探测 CAPCUT_MATE_BASE_URL")
    parser.add_argument("--base-url", help="CapCut Mate 地址；也可使用 CAPCUT_MATE_BASE_URL")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    report = build_report(
        find_root(args.root),
        archive=args.archive,
        probe=args.probe_capcut,
        base_url=args.base_url,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"工作流：{report['workflow']}")
        print(f"本地契约：{'就绪' if report['local_ready'] else '未就绪'}")
        archive = report["archive"]
        print(
            f"归档校验：{'通过' if report['node_map_valid'] else '未通过'}；"
            f"顶层 {archive.get('top_level_node_count', 0)} 个，"
            f"含内部节点共 {archive.get('full_node_count', 0)} 个，"
            f"边 {archive.get('edge_count', 0)} 条"
        )
        capcut = report["capcut_mate"]
        if capcut.get("probed") is False:
            print(f"CapCut Mate：{'已配置' if capcut.get('configured') else '未配置'}（未探测）")
        else:
            print(f"CapCut Mate：{'可访问' if capcut.get('reachable') else '不可访问'}")
        tts_catalog = report["tts_voice_catalog"]
        print(
            "TTS 音色目录："
            f"{tts_catalog.get('status')}；"
            f"共 {tts_catalog.get('voice_count', 0)} 项，"
            f"可选已验证 {tts_catalog.get('selectable_count', 0)} 项"
        )
        print("离线节点：" + ", ".join(item["name"] for item in report["pure_python_nodes"] if item["implemented"]))
        print(f"可执行节点状态：{report['node_count']} 个，" + ", ".join(f"{key}={value}" for key, value in report["node_status_counts"].items()))
        print("代码缺口：" + ", ".join(report["code_gaps"]))
        print("能力依赖：" + ", ".join(report["required_user_inputs"]))
        print("当前仍需你协助：")
        for action in report["user_actions"]:
            print(f"- [{action['kind']}] 节点 {','.join(action['node_ids'])}：{action['request']}")
        print(report["interpretation"])
    return 0 if report["local_ready"] and report["node_map_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
