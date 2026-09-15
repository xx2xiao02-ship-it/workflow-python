"""Build and stamp the independent local-AST Graphify production graph."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _compute_scope_hash(root: Path, scope: dict[str, object]) -> tuple[str, list[str]]:
    from tools.governance_check import compute_scope_hash

    return compute_scope_hash(root, scope)
SCOPE_PATH = ROOT / "governance" / "graphify_scope.json"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(command))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def main() -> int:
    graphify = shutil.which("graphify")
    if not graphify:
        print("ERROR: graphify CLI 不在 PATH；请先安装本地 graphifyy，不能用付费语义服务替代。")
        return 127
    scope = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    extract = _run([graphify, "extract", str(ROOT / "topic_migration"), "--code-only", "--no-cluster", "--out", str(ROOT)])
    if extract.returncode:
        print(extract.stdout)
        print(extract.stderr)
        return extract.returncode
    cluster = _run([graphify, "cluster-only", str(ROOT), "--no-label", "--no-viz"])
    if cluster.returncode:
        print(cluster.stdout)
        print(cluster.stderr)
        return cluster.returncode
    graph_path = ROOT / str(scope.get("graph_output") or "graphify-out/graph.json")
    report_path = ROOT / str(scope.get("report_output") or "graphify-out/GRAPH_REPORT.md")
    if not graph_path.is_file() or not report_path.is_file():
        print("ERROR: Graphify 未生成 graph.json 或 GRAPH_REPORT.md")
        return 1
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(f"ERROR: graph.json 不可读：{type(exc).__name__}: {exc}")
        return 1
    source_hash, source_files = _compute_scope_hash(ROOT, scope)
    version = _run([graphify, "--version"])
    scope.update({
        "status": "fresh",
        "source_hash": source_hash,
        "source_files": source_files,
        "graphify_version": (version.stdout or version.stderr).strip(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_check": {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "fresh"},
        "graph_summary": {
            "nodes": len(graph.get("nodes", [])) if isinstance(graph, dict) else 0,
            "edges": len(graph.get("links", graph.get("edges", []))) if isinstance(graph, dict) else 0,
        },
    })
    SCOPE_PATH.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    diagnose = _run([graphify, "diagnose", "multigraph", "--json", "--graph", str(graph_path)])
    (ROOT / "governance" / "graphify_diagnose.json").write_text(diagnose.stdout or diagnose.stderr, encoding="utf-8")
    print(f"Graphify: {scope['graph_summary']['nodes']} nodes, {scope['graph_summary']['edges']} edges")
    print(f"Scope hash: {source_hash}")
    print(f"Diagnose exit: {diagnose.returncode}")
    return 0 if diagnose.returncode == 0 else diagnose.returncode


if __name__ == "__main__":
    raise SystemExit(main())
