from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tools.governance_check import check_static, compute_scope_hash

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "governance" / "module_registry.json"


def _minimal_registry() -> dict[str, object]:
    return {
        "schema_version": "test",
        "project": {
            "source_roots": ["app"],
            "production_file_globs": ["app/**/*.py"],
            "exclude_globs": ["**/__pycache__/**"],
            "external_imports": ["stdlib"],
            "forbidden_import_roots": ["old_console"],
        },
        "modules": [
            {
                "id": "app.server",
                "python_module": "app.server",
                "production_files": ["app/server.py"],
                "allowed_dependencies": ["stdlib"],
                "internal_interfaces": [],
                "boundary_capabilities": {"storage": [], "config": [], "network": []},
            },
            {
                "id": "app.other",
                "python_module": "app.other",
                "production_files": ["app/other.py"],
                "allowed_dependencies": ["stdlib"],
                "internal_interfaces": ["_private"],
                "boundary_capabilities": {"storage": [], "config": [], "network": []},
            },
        ],
        "routes": [{"method": "GET", "path": "/ok", "owner": "app.server"}],
    }


def _write_project(root: Path, files: dict[str, str], registry: dict[str, object] | None = None) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    registry_path = root / "registry.json"
    registry_path.write_text(json.dumps(registry or _minimal_registry(), ensure_ascii=False), encoding="utf-8")
    return registry_path


def _codes(result: dict[str, object]) -> set[str]:
    return {str(item["code"]) for item in result["findings"] if isinstance(item, dict)}


def _run_import_linter_sample(tmp_path: Path, low_source: str) -> subprocess.CompletedProcess[str]:
    sample_root = tmp_path / "import-linter-sample"
    package_root = sample_root / "sample_app"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "high.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package_root / "low.py").write_text(low_source, encoding="utf-8")
    (sample_root / ".importlinter").write_text(
        "[importlinter]\n"
        "root_package = sample_app\n\n"
        "[importlinter:layers]\n"
        "name = sample layers\n"
        "type = layers\n"
        "layers =\n"
        "    sample_app.high\n"
        "    sample_app.low\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(sample_root)
    executable = ROOT / ".venv" / "Scripts" / "lint-imports.exe"
    return subprocess.run(
        [str(executable), "--config", str(sample_root / ".importlinter"), "--no-cache", "--no-logo"],
        cwd=sample_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_current_registry_and_dependencies_pass_without_graph(tmp_path: Path) -> None:
    result = check_static(ROOT, REGISTRY, check_graph=False)
    assert result["status"] == "passed", result


def test_import_linter_rejects_reverse_layer_import(tmp_path: Path) -> None:
    result = _run_import_linter_sample(tmp_path, "from sample_app.high import VALUE\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "BROKEN" in result.stdout


def test_import_linter_accepts_layered_import(tmp_path: Path) -> None:
    result = _run_import_linter_sample(tmp_path, "VALUE = 2\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "KEPT" in result.stdout


def test_duplicate_route_is_rejected(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/server.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(tmp_path, {"app/server.py": "def do_GET(self):\n    if path == '/ok':\n        return\n    if path == '/ok':\n        return\n"}, registry)
    result = check_static(tmp_path, registry_path, check_graph=False)
    assert "ROUTE003" in _codes(result)


def test_unregistered_production_file_is_rejected(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/**/*.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(tmp_path, {"app/server.py": "", "app/other.py": ""}, registry)
    result = check_static(tmp_path, registry_path, check_graph=False)
    assert "FILE003" in _codes(result)


def test_unregistered_migration_file_is_rejected(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/server.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(tmp_path, {"app/server.py": "VALUE = 1\n"}, registry)
    migration_dir = tmp_path / "governance"
    migration_dir.mkdir()
    (migration_dir / "migration_file_register.json").write_text(json.dumps({"entries": []}), encoding="utf-8")
    result = check_static(tmp_path, registry_path, check_graph=False)
    assert "MIG009" in _codes(result)


def test_cross_module_dependency_is_rejected(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry_path = _write_project(tmp_path, {"app/server.py": "from .other import public\n", "app/other.py": "public = 1\n"}, registry)
    result = check_static(tmp_path, registry_path, check_graph=False)
    assert "DEP001" in _codes(result)


def test_old_local_runtime_import_is_rejected(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/server.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(tmp_path, {"app/server.py": "import legacy_pkg\n", "legacy_pkg/__init__.py": ""}, registry)
    result = check_static(tmp_path, registry_path, check_graph=False)
    assert "OLD001" in _codes(result)


def test_dynamic_storage_and_config_boundaries_are_rejected(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/server.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(
        tmp_path,
        {
            "app/server.py": "import importlib\nimport os\nfrom pathlib import Path\n\ndef do_GET(self):\n    importlib.import_module('legacy_pkg')\n    os.getenv('SECRET')\n    Path('data.txt').write_text('x', encoding='utf-8')\n",
        },
        registry,
    )
    result = check_static(tmp_path, registry_path, check_graph=False)
    codes = _codes(result)
    assert {"DYN001", "BND001", "CFG001"} <= codes


def test_legal_isolated_sample_passes(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/server.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(tmp_path, {"app/server.py": "from pathlib import Path\n\nVALUE = Path('README.md').name\n"}, registry)
    result = check_static(tmp_path, registry_path, check_graph=False)
    assert result["status"] == "passed", result


def test_graph_freshness_changes_after_source_edit(tmp_path: Path) -> None:
    registry = _minimal_registry()
    registry["project"]["production_file_globs"] = ["app/server.py"]
    registry["modules"] = registry["modules"][:1]
    registry_path = _write_project(tmp_path, {"app/server.py": "VALUE = 1\n"}, registry)
    scope_dir = tmp_path / "governance"
    graph_dir = tmp_path / "graphify-out"
    graph_dir.mkdir()
    (graph_dir / "graph.json").write_text('{"nodes": [], "edges": []}', encoding="utf-8")
    (graph_dir / "GRAPH_REPORT.md").write_text("# report\n", encoding="utf-8")
    scope = {
        "included_globs": ["app/server.py"],
        "excluded_globs": [],
        "graph_output": "graphify-out/graph.json",
        "report_output": "graphify-out/GRAPH_REPORT.md",
        "source_hash": "",
        "built_at": "2999-01-01T00:00:00+00:00",
        "max_age_hours": 24,
    }
    scope["source_hash"], _files = compute_scope_hash(tmp_path, scope)
    scope_dir.mkdir()
    scope_path = scope_dir / "graphify_scope.json"
    scope_path.write_text(json.dumps(scope), encoding="utf-8")
    fresh = check_static(tmp_path, registry_path, check_graph=True)
    assert fresh["graph"]["status"] == "fresh", fresh
    (tmp_path / "app/server.py").write_text("VALUE = 2\n", encoding="utf-8")
    stale = check_static(tmp_path, registry_path, check_graph=True)
    assert "GRAPH004" in _codes(stale)
