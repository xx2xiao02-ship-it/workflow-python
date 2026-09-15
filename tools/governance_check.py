"""Lightweight source-governance gate for E:\\codex project.

The gate is intentionally stdlib-only. It checks the registry and source tree,
then delegates Python architecture and lint rules to the locked developer tools.
It does not execute business tasks or change source files.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "governance" / "module_registry.json"
DEFAULT_SCOPE = ROOT / "governance" / "graphify_scope.json"
DEFAULT_MIGRATION_REGISTER = ROOT / "governance" / "migration_file_register.json"
DEFAULT_REPORT = ROOT / "governance" / "reports" / "latest.json"
MIGRATION_STAGES = {"public_foundation", "api_config", "topic_center", "joint_acceptance"}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    path: str = ""
    line: int | None = None
    module: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "module": self.module,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _excluded(relative_path: str, patterns: Iterable[str]) -> bool:
    candidate = relative_path.replace("\\", "/")
    return any(fnmatch.fnmatch(candidate, pattern) or Path(candidate).match(pattern) for pattern in patterns)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根值必须是对象：{path}")
    return value


def _pattern_matches(root: Path, pattern: str) -> list[Path]:
    if any(char in pattern for char in "*?["):
        return sorted(path for path in root.glob(pattern) if path.is_file())
    path = root / pattern
    return [path] if path.is_file() else []


def _production_index(root: Path, registry: Mapping[str, Any]) -> tuple[dict[str, list[str]], list[Finding]]:
    findings: list[Finding] = []
    index: dict[str, list[str]] = defaultdict(list)
    modules = registry.get("modules")
    if not isinstance(modules, list):
        return {}, [Finding("REG001", "error", "module_registry.modules 必须是数组", "governance/module_registry.json")]
    for module in modules:
        if not isinstance(module, Mapping):
            findings.append(Finding("REG002", "error", "模块登记项必须是对象", "governance/module_registry.json"))
            continue
        module_id = str(module.get("id") or "").strip()
        patterns = module.get("production_files") if isinstance(module.get("production_files"), list) else []
        if not module_id:
            findings.append(Finding("REG003", "error", "模块缺少 id", "governance/module_registry.json"))
            continue
        if not patterns:
            findings.append(Finding("REG004", "error", f"模块 {module_id} 未登记 production_files", "governance/module_registry.json", module=module_id))
        for pattern_value in patterns:
            pattern = str(pattern_value)
            matches = _pattern_matches(root, pattern)
            if not matches:
                findings.append(Finding("FILE001", "error", f"登记的生产文件不存在或 glob 无匹配：{pattern}", "governance/module_registry.json", module=module_id))
            for path in matches:
                relative = _relative(root, path)
                if not _excluded(relative, registry.get("project", {}).get("exclude_globs", [])):
                    index[relative].append(module_id)
    for relative, owners in sorted(index.items()):
        if len(owners) != 1:
            findings.append(Finding("FILE002", "error", f"生产文件必须且只能归属一个模块：{relative} -> {owners}", relative))
    return dict(index), findings


def _expected_production_files(root: Path, registry: Mapping[str, Any]) -> set[str]:
    project = registry.get("project") if isinstance(registry.get("project"), Mapping) else {}
    excludes = project.get("exclude_globs") if isinstance(project.get("exclude_globs"), list) else []
    expected: set[str] = set()
    for pattern_value in project.get("production_file_globs", []):
        for path in _pattern_matches(root, str(pattern_value)):
            relative = _relative(root, path)
            if not _excluded(relative, excludes):
                expected.add(relative)
    return expected


def _check_migration_file_register(
    root: Path,
    registry: Mapping[str, Any],
    register_path: Path = DEFAULT_MIGRATION_REGISTER,
) -> list[Finding]:
    """Require a one-to-one audit record for every migrated production file."""

    if register_path == DEFAULT_MIGRATION_REGISTER and root.resolve() != ROOT.resolve():
        register_path = root / "governance" / "migration_file_register.json"
    if not register_path.is_file():
        if root.resolve() == ROOT.resolve():
            return [Finding("MIG001", "error", "逐文件迁移登记不存在", "governance/migration_file_register.json")]
        return []
    try:
        register = _read_json(register_path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return [Finding("MIG001", "error", f"逐文件迁移登记不可读：{type(exc).__name__}: {exc}", str(register_path))]

    raw_entries = register.get("entries")
    if not isinstance(raw_entries, list):
        return [Finding("MIG001", "error", "逐文件迁移登记 entries 必须是数组", _relative(root, register_path))]

    expected = _expected_production_files(root, registry)
    owners: dict[str, list[str]] = defaultdict(list)
    for module in registry.get("modules", []):
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("id") or "")
        for pattern_value in module.get("production_files", []):
            for path in _pattern_matches(root, str(pattern_value)):
                relative = _relative(root, path)
                if relative in expected:
                    owners[relative].append(module_id)

    by_target: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    findings: list[Finding] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            findings.append(Finding("MIG002", "error", "逐文件迁移登记项必须是对象", _relative(root, register_path)))
            continue
        target = str(raw.get("target_path") or "").replace("\\", "/").strip()
        by_target[target].append(raw)
        if not target:
            findings.append(Finding("MIG003", "error", "迁移登记项缺少 target_path", _relative(root, register_path)))
            continue
        if not str(raw.get("source_path") or "").strip():
            findings.append(Finding("MIG004", "error", f"迁移登记缺少 source_path：{target}", _relative(root, register_path)))
        source_hash = str(raw.get("source_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            findings.append(Finding("MIG005", "error", f"迁移登记 source_sha256 无效：{target}", _relative(root, register_path)))
        if not str(raw.get("module") or "").strip():
            findings.append(Finding("MIG006", "error", f"迁移登记缺少 module：{target}", _relative(root, register_path)))
        if not str(raw.get("migration_reason") or "").strip():
            findings.append(Finding("MIG007", "error", f"迁移登记缺少 migration_reason：{target}", _relative(root, register_path)))
        if str(raw.get("stage") or "") not in MIGRATION_STAGES:
            findings.append(Finding("MIG008", "error", f"迁移登记 stage 无效：{target}", _relative(root, register_path)))
        target_path = root / target
        target_hash = str(raw.get("target_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", target_hash):
            findings.append(Finding("MIG013", "error", f"迁移登记 target_sha256 无效：{target}", _relative(root, register_path)))
        elif target_path.is_file() and hashlib.sha256(target_path.read_bytes()).hexdigest() != target_hash:
            findings.append(Finding("MIG014", "error", f"迁移登记 target_sha256 与当前文件不一致：{target}", target))

    for target in sorted(expected):
        entries = by_target.get(target, [])
        if not entries:
            findings.append(Finding("MIG009", "error", f"生产文件未进入逐文件迁移登记：{target}", target))
            continue
        if len(entries) != 1:
            findings.append(Finding("MIG010", "error", f"生产文件迁移登记必须且只能一项：{target} -> {len(entries)}", target))
            continue
        registered_module = str(entries[0].get("module") or "")
        registered_owners = owners.get(target, [])
        if len(registered_owners) == 1 and registered_module != registered_owners[0]:
            findings.append(Finding("MIG011", "error", f"迁移登记模块与生产归属不一致：{target} -> {registered_module} / {registered_owners[0]}", target))
    for target in sorted(set(by_target) - expected):
        findings.append(Finding("MIG012", "error", f"迁移登记包含非生产文件：{target}", _relative(root, register_path)))
    return findings


def _stdlib_roots() -> set[str]:
    return set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names) | {"__future__"}


def _top_level_local_names(root: Path) -> set[str]:
    result: set[str] = set()
    for path in root.iterdir():
        if path.name.startswith(".") or path.name in {"__pycache__", ".venv"}:
            continue
        if path.is_dir() or path.suffix == ".py":
            result.add(path.stem if path.is_file() else path.name)
    return result


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _module_lookup(registry: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    by_python: dict[str, str] = {}
    for raw in registry.get("modules", []):
        if not isinstance(raw, Mapping):
            continue
        module_id = str(raw.get("id") or "").strip()
        python_module = str(raw.get("python_module") or "").strip()
        if module_id:
            by_id[module_id] = raw
        if python_module:
            by_python[python_module] = module_id
    return by_id, by_python


def _resolve_registered_module(target: str, by_python: Mapping[str, str]) -> str | None:
    candidates = [name for name in by_python if target == name or target.startswith(name + ".")]
    if not candidates:
        return None
    return by_python[max(candidates, key=len)]


def _relative_import_targets(
    node: ast.ImportFrom,
    current_module: str,
    known_python: set[str],
    *,
    is_package: bool,
) -> list[tuple[str, str]]:
    if node.level:
        parts = current_module.split(".") if is_package else current_module.split(".")[:-1]
        levels_up = node.level - 1
        base_parts = parts[:-levels_up] if levels_up else parts
        base = ".".join(base_parts)
    else:
        base = str(node.module or "")
    imported_module = f"{base}.{node.module}" if base and node.module else (node.module or base)
    result: list[tuple[str, str]] = []
    for alias in node.names:
        if imported_module:
            result.append((imported_module, alias.name))
            continue
        candidate = f"{base}.{alias.name}" if base else alias.name
        result.append((candidate if candidate in known_python else base, alias.name))
    return result


def _check_imports(
    root: Path,
    path: Path,
    module: Mapping[str, Any],
    registry: Mapping[str, Any],
    by_python: Mapping[str, str],
) -> list[Finding]:
    findings: list[Finding] = []
    relative = _relative(root, path)
    current_module = str(module.get("python_module") or "")
    known_python = set(by_python)
    project = registry.get("project") if isinstance(registry.get("project"), Mapping) else {}
    source_roots = {str(value).replace("\\", "/").split("/", 1)[0] for value in project.get("source_roots", [])}
    forbidden_roots = {str(value).strip() for value in project.get("forbidden_import_roots", [])}
    allowed_external = {str(value).strip() for value in project.get("external_imports", [])}
    stdlib = _stdlib_roots()
    local_names = _top_level_local_names(root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return [Finding("PY001", "error", f"无法解析生产 Python 文件：{type(exc).__name__}: {exc}", relative, module=current_module)]

    nodes: Iterable[ast.AST] = ast.walk(tree)
    for node in nodes:
        if isinstance(node, ast.Import):
            imports = [(alias.name, alias.name.rsplit(".", 1)[-1]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = _relative_import_targets(node, current_module, known_python, is_package=path.name == "__init__.py")
        else:
            continue
        for target, symbol in imports:
            if not target:
                continue
            registered_id = _resolve_registered_module(target, by_python)
            top_level = target.split(".", 1)[0]
            if registered_id:
                allowed = {str(value) for value in module.get("allowed_dependencies", [])}
                if registered_id not in allowed and registered_id != str(module.get("id") or ""):
                    findings.append(Finding("DEP001", "error", f"未登记或违规跨模块依赖：{current_module} -> {target}", relative, node.lineno, current_module))
                target_record = next((raw for raw in registry.get("modules", []) if isinstance(raw, Mapping) and str(raw.get("id") or "") == registered_id), {})
                internal_names = {str(value) for value in target_record.get("internal_interfaces", [])}
                if symbol in internal_names or symbol.startswith("_") and symbol not in {"__all__"}:
                    if registered_id != str(module.get("id") or ""):
                        findings.append(Finding("DEP002", "error", f"禁止导入模块内部实现：{current_module} -> {target}.{symbol}", relative, node.lineno, current_module))
                continue
            if top_level == "topic_migration" or target.startswith("topic_migration."):
                findings.append(Finding("DEP003", "error", f"topic_migration 内部模块未登记：{target}", relative, node.lineno, current_module))
                continue
            if top_level in forbidden_roots or (top_level in local_names and top_level not in source_roots):
                findings.append(Finding("OLD001", "error", f"运行时导入旧工程或未登记本地包：{target}", relative, node.lineno, current_module))
                continue
            if top_level not in stdlib and top_level not in allowed_external:
                findings.append(Finding("DEP004", "error", f"外部依赖未登记：{target}", relative, node.lineno, current_module))
    return findings


_FILESYSTEM_METHODS = {"exists", "is_dir", "glob", "rglob", "stat", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "unlink", "open"}


def _check_boundaries(root: Path, path: Path, module: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    relative = _relative(root, path)
    module_id = str(module.get("id") or "")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return findings
    capabilities = module.get("boundary_capabilities") if isinstance(module.get("boundary_capabilities"), Mapping) else {}
    storage_caps = {str(value) for value in capabilities.get("storage", [])}
    config_caps = {str(value) for value in capabilities.get("config", [])}
    for node in ast.walk(tree):
        required: str | None = None
        code: str | None = None
        description = ""
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                imported_names = [str(item.name or "") for item in node.names]
            else:
                imported_names = [str(node.module or "")]
            if any(value == "sqlite3" or value.startswith("sqlite3.") for value in imported_names):
                required = "sqlite"
                code = "BND001"
                description = "sqlite 存储访问"
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            suffix = name.rsplit(".", 1)[-1]
            if name in {"__import__", "importlib.import_module", "importlib.util.spec_from_file_location"}:
                findings.append(Finding("DYN001", "error", f"检测到动态导入：{name}", relative, node.lineno, module_id))
                continue
            if name in {"open", "os.open", "os.replace", "os.remove", "os.unlink"} or suffix in _FILESYSTEM_METHODS:
                required = "filesystem"
                code = "BND001"
                description = "文件系统存储访问"
            if name == "sqlite3.connect":
                required = "sqlite"
                code = "BND001"
                description = "sqlite 存储访问"
            if name in {"os.getenv", "getenv"} or name.startswith("os.environ"):
                if "environment" not in config_caps:
                    findings.append(Finding("CFG001", "error", f"配置引用未登记：{name}", relative, node.lineno, module_id))
                continue
        elif isinstance(node, ast.Attribute):
            name = _dotted_name(node)
            if name.startswith("os.environ") and "environment" not in config_caps:
                findings.append(Finding("CFG001", "error", f"配置引用未登记：{name}", relative, node.lineno, module_id))
                continue
        if required and required not in storage_caps:
            findings.append(Finding(code or "BND001", "error", f"{description}未登记在模块能力中：{module_id}", relative, getattr(node, "lineno", None), module_id))
    return findings


def _literal_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return [value for item in node.elts for value in _literal_strings(item)]
    return []


class _RouteVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.method = ""
        self.routes: list[tuple[str, str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.method
        if node.name in {"do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE"}:
            self.method = node.name[3:]
            self.generic_visit(node)
            self.method = previous
            return
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Compare(self, node: ast.Compare) -> None:
        if self.method and len(node.ops) == 1 and isinstance(node.left, ast.Name) and node.left.id == "path":
            operator = node.ops[0]
            if isinstance(operator, ast.Eq):
                for value in _literal_strings(node.comparators[0]):
                    self.routes.append((self.method, value, node.lineno))
            elif isinstance(operator, ast.In):
                for value in _literal_strings(node.comparators[0]):
                    self.routes.append((self.method, value, node.lineno))
        self.generic_visit(node)


def _extract_exact_routes(path: Path) -> list[tuple[str, str, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    visitor = _RouteVisitor()
    visitor.visit(tree)
    return visitor.routes


def _check_routes(root: Path, registry: Mapping[str, Any], selected_ids: set[str] | None) -> list[Finding]:
    findings: list[Finding] = []
    routes = [route for route in registry.get("routes", []) if isinstance(route, Mapping)]
    seen: dict[tuple[str, str], int] = {}
    module_ids = {str(module.get("id") or "") for module in registry.get("modules", []) if isinstance(module, Mapping)}
    for route in routes:
        method = str(route.get("method") or "").upper()
        path_value = str(route.get("path") or "")
        owner = str(route.get("owner") or "")
        key = (method, path_value)
        if owner not in module_ids:
            findings.append(Finding("ROUTE001", "error", f"路由 owner 未登记：{owner}", "governance/module_registry.json", module=owner))
        if key in seen:
            findings.append(Finding("ROUTE002", "error", f"登记表存在重复路由：{method} {path_value}", "governance/module_registry.json", module=owner))
        seen[key] = seen.get(key, 0) + 1

    server_module = next(
        (
            module
            for module in registry.get("modules", [])
            if isinstance(module, Mapping)
            and (
                str(module.get("python_module") or "").endswith(".server")
                or str(module.get("id") or "").endswith(".server")
                or any(str(pattern).replace("\\", "/").endswith("/server.py") for pattern in module.get("production_files", []))
            )
        ),
        None,
    )
    if not server_module or selected_ids is not None and str(server_module.get("id") or "") not in selected_ids:
        return findings
    server_paths: list[Path] = []
    for pattern_value in server_module.get("production_files", []):
        server_paths.extend(path for path in _pattern_matches(root, str(pattern_value)) if path.suffix.lower() == ".py")
    if not server_paths:
        return findings
    server_path = sorted(server_paths)[0]
    registered_exact = {(str(route.get("method") or "").upper(), str(route.get("path") or "")) for route in routes if "{" not in str(route.get("path") or "") and "*" not in str(route.get("path") or "")}
    counts = Counter((method, path_value) for method, path_value, _line in _extract_exact_routes(server_path))
    for (method, path_value), count in counts.items():
        if count > 1:
            findings.append(Finding("ROUTE003", "error", f"源码存在重复精确路由判断：{method} {path_value} ({count} 次)", _relative(root, server_path), module=str(server_module.get("id") or "")))
        if (method, path_value) not in registered_exact:
            findings.append(Finding("ROUTE004", "error", f"源码路由未登记：{method} {path_value}", _relative(root, server_path), module=str(server_module.get("id") or "")))
    return findings


def compute_scope_hash(root: Path, scope: Mapping[str, Any]) -> tuple[str, list[str]]:
    included: set[str] = set()
    excludes = scope.get("excluded_globs") if isinstance(scope.get("excluded_globs"), list) else []
    for pattern_value in scope.get("included_globs", []):
        for path in _pattern_matches(root, str(pattern_value)):
            relative = _relative(root, path)
            if not _excluded(relative, excludes):
                included.add(relative)
    lines: list[str] = []
    for relative in sorted(included):
        digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        lines.append(f"{relative}\t{digest}")
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    return hashlib.sha256(payload).hexdigest(), sorted(included)


def check_graph_freshness(root: Path, scope_path: Path = DEFAULT_SCOPE) -> tuple[dict[str, Any], list[Finding]]:
    findings: list[Finding] = []
    if scope_path == DEFAULT_SCOPE and root.resolve() != ROOT.resolve():
        scope_path = root / "governance" / "graphify_scope.json"
    relative_scope = _relative(root, scope_path) if scope_path.is_relative_to(root) else str(scope_path)
    if not scope_path.is_file():
        return {"status": "missing", "scope_path": relative_scope}, [Finding("GRAPH001", "error", "独立生产图谱元数据不存在，请先运行 build_production_graph.py", relative_scope)]
    try:
        scope = _read_json(scope_path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return {"status": "invalid", "scope_path": relative_scope}, [Finding("GRAPH001", "error", f"图谱元数据不可读：{type(exc).__name__}: {exc}", relative_scope)]
    graph_relative = str(scope.get("graph_output") or "graphify-out/graph.json")
    report_relative = str(scope.get("report_output") or "graphify-out/GRAPH_REPORT.md")
    graph_path = root / graph_relative
    report_path = root / report_relative
    current_hash, files = compute_scope_hash(root, scope)
    recorded_hash = str(scope.get("source_hash") or "")
    status = "fresh"
    if not graph_path.is_file():
        findings.append(Finding("GRAPH002", "error", f"图谱文件不存在：{graph_relative}", graph_relative))
        status = "stale"
    if not report_path.is_file() or not report_path.read_text(encoding="utf-8", errors="replace").strip():
        findings.append(Finding("GRAPH003", "error", f"图谱报告不存在或为空：{report_relative}", report_relative))
        status = "stale"
    if not recorded_hash or recorded_hash != current_hash:
        findings.append(Finding("GRAPH004", "error", "图谱源码 hash 与当前生产源码不一致，图谱已过期", relative_scope))
        status = "stale"
    built_at = str(scope.get("built_at") or "")
    try:
        built = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - built.astimezone(timezone.utc)).total_seconds() / 3600
    except (TypeError, ValueError):
        age_hours = None
    max_age = float(scope.get("max_age_hours") or 24)
    if age_hours is None:
        findings.append(Finding("GRAPH005", "error", "图谱 built_at 缺失或格式无效，无法判断新鲜度", relative_scope))
        status = "stale"
    elif age_hours > max_age:
        findings.append(Finding("GRAPH006", "error", f"图谱已超过新鲜度期限：{age_hours:.1f}h > {max_age:g}h", relative_scope))
        status = "stale"
    info = {
        "status": status,
        "scope_path": relative_scope,
        "graph_output": graph_relative,
        "report_output": report_relative,
        "recorded_source_hash": recorded_hash,
        "current_source_hash": current_hash,
        "source_files": files,
        "age_hours": age_hours,
    }
    return info, findings


def _selected_module_records(registry: Mapping[str, Any], module_filter: str | None) -> tuple[list[Mapping[str, Any]], set[str] | None, list[Finding]]:
    modules = [module for module in registry.get("modules", []) if isinstance(module, Mapping)]
    if not module_filter:
        return modules, None, []
    selected = [module for module in modules if module_filter in {str(module.get("id") or ""), str(module.get("python_module") or ""), str(module.get("domain") or "")}]
    if not selected:
        return [], set(), [Finding("REG005", "error", f"未找到模块：{module_filter}", "governance/module_registry.json")]
    return selected, {str(module.get("id") or "") for module in selected}, []


def check_static(root: Path = ROOT, registry_path: Path = DEFAULT_REGISTRY, module_filter: str | None = None, check_graph: bool = True) -> dict[str, Any]:
    registry = _read_json(registry_path)
    all_index, findings = _production_index(root, registry)
    findings.extend(_check_migration_file_register(root, registry))
    selected_records, selected_ids, selection_findings = _selected_module_records(registry, module_filter)
    findings.extend(selection_findings)
    expected = _expected_production_files(root, registry)
    if selected_ids is None:
        for relative in sorted(expected - set(all_index)):
            findings.append(Finding("FILE003", "error", f"生产文件未登记：{relative}", relative))
    by_id, by_python = _module_lookup(registry)
    selected_index = all_index if selected_ids is None else {path: owners for path, owners in all_index.items() if owners and owners[0] in selected_ids}
    for relative, owners in sorted(selected_index.items()):
        if len(owners) != 1:
            continue
        module_id = owners[0]
        module = by_id.get(module_id)
        path = root / relative
        if not module or path.suffix.lower() != ".py":
            continue
        findings.extend(_check_imports(root, path, module, registry, by_python))
        findings.extend(_check_boundaries(root, path, module))
    findings.extend(_check_routes(root, registry, selected_ids))
    graph_info: dict[str, Any] = {"status": "skipped"}
    if check_graph:
        graph_info, graph_findings = check_graph_freshness(root)
        findings.extend(graph_findings)
    errors = [finding for finding in findings if finding.severity == "error"]
    return {
        "checked_at": _now(),
        "root": str(root),
        "registry": _relative(root, registry_path) if registry_path.is_relative_to(root) else str(registry_path),
        "module_filter": module_filter,
        "status": "failed" if errors else "passed",
        "findings": [finding.to_dict() for finding in findings],
        "graph": graph_info,
        "summary": {"errors": len(errors), "findings": len(findings), "production_files": len(selected_index)},
    }


def _tool_path(root: Path, name: str) -> str | None:
    local = root / ".venv" / "Scripts" / (name + ".exe")
    if local.is_file():
        return str(local)
    return shutil.which(name)


def _run_external(root: Path, command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return {"command": command, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    except OSError as exc:
        return {"command": command, "returncode": 127, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def run_gate(root: Path = ROOT, registry_path: Path = DEFAULT_REGISTRY, module_filter: str | None = None, run_import_linter: bool = True, run_ruff: bool = True, check_graph: bool = True) -> dict[str, Any]:
    static = check_static(root, registry_path, module_filter=module_filter, check_graph=check_graph)
    external: list[dict[str, Any]] = []
    if run_import_linter:
        tool = _tool_path(root, "lint-imports")
        external.append(_run_external(root, [tool] if tool else ["lint-imports"]))
        external[-1]["name"] = "import-linter"
    if run_ruff:
        tool = _tool_path(root, "ruff")
        command = [tool or "ruff", "check", "."]
        external.append(_run_external(root, command))
        external[-1]["name"] = "ruff"
    failures = int(static["summary"]["errors"]) + sum(1 for result in external if int(result.get("returncode", 1)) != 0)
    return {**static, "external": external, "status": "failed" if failures else "passed", "summary": {**static["summary"], "external_failures": sum(1 for result in external if int(result.get("returncode", 1)) != 0), "total_failures": failures}}


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_human(report: Mapping[str, Any]) -> None:
    print(f"治理检查: {str(report.get('status', 'failed')).upper()}")
    summary = report.get("summary", {})
    print(f"生产文件 {summary.get('production_files', 0)} · 静态发现 {summary.get('findings', 0)} · 外部工具失败 {summary.get('external_failures', 0)}")
    for finding in report.get("findings", []):
        if not isinstance(finding, Mapping):
            continue
        location = str(finding.get("path") or "")
        line = f":{finding['line']}" if finding.get("line") else ""
        print(f"[{finding.get('severity', 'error')}] {finding.get('code')} {location}{line} {finding.get('message')}")
    for result in report.get("external", []):
        if not isinstance(result, Mapping):
            continue
        print(f"[{result.get('name', 'tool')}] exit={result.get('returncode')}")
        output = str(result.get("stdout") or result.get("stderr") or "").strip()
        if output:
            print(output[:6000])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查项目文件归属、模块依赖、路由、边界、Graphify 新鲜度、Import Linter 和 Ruff")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--module", default=None, help="只检查一个已登记模块；Import Linter/Ruff 仍按全局门禁执行")
    parser.add_argument("--skip-import-linter", action="store_true")
    parser.add_argument("--skip-ruff", action="store_true")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    registry_path = (args.registry or root / "governance" / "module_registry.json").resolve()
    report_path = (args.report or root / "governance" / "reports" / "latest.json").resolve()
    try:
        report = run_gate(root, registry_path, module_filter=args.module, run_import_linter=not args.skip_import_linter, run_ruff=not args.skip_ruff, check_graph=not args.skip_graph)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "checked_at": _now()}
    _write_report(report_path, report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
        print(f"报告: {_relative(root, report_path)}")
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
