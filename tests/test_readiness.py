from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "skills" / "daily-update-te" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_readiness  # noqa: E402


def test_status_matrix_covers_top_level_and_internal_nodes() -> None:
    statuses = check_readiness.build_node_status()
    ids = {item["node_id"] for item in statuses}

    assert len(statuses) == 61
    assert len(ids) == 61
    assert {
        "197742", "127095", "142186", "106538", "159953",
        "181639", "186546", "139653", "106157",
    } <= ids
    assert sum(item["scope"] == "internal" for item in statuses) == 9


def test_8364_archive_matches_status_matrix() -> None:
    archive = Path.home() / "Downloads" / check_readiness.WORKFLOW_ARCHIVE
    report = check_readiness.inspect_workflow_archive(archive)

    assert report["parse_status"] == "ok"
    assert report["top_level_node_count"] == 52
    assert report["full_node_count"] == 61
    assert report["edge_count"] == 56
    assert report["node_ids_unique"] is True
    assert set(report["node_ids"]) == {
        item["node_id"] for item in check_readiness.build_node_status()
    }


def test_readiness_report_keeps_capcut_probe_opt_in() -> None:
    archive = Path.home() / "Downloads" / check_readiness.WORKFLOW_ARCHIVE
    report = check_readiness.build_report(PROJECT_ROOT, archive=archive, probe=False)

    assert report["local_ready"] is True
    assert report["node_map_valid"] is True
    assert report["capcut_mate"]["probed"] is False
    assert report["workflow_ready"] is False
    assert "未探测" in report["interpretation"]
    assert "地址可达" not in report["interpretation"]
    assert report["local_checks"]["host_shot_recognition_contract"]["exists"] is True
    statuses = {item["node_id"]: item["status"] for item in report["node_status"]}
    assert statuses["174651"] == "verified_live_route"
    assert statuses["135313"] == "verified_live_route"
    assert statuses["165901"] == "verified_live_route"
    assert statuses["115137"] == "verified_live_route"
    assert statuses["187358"] == "verified_live_route"
    assert statuses["191914"] == "contract_inconsistent"
    assert statuses["118959"] == "offline_verified"
    assert statuses["165818"] == "transport_pending"
    assert statuses["121815"] == "contract_implemented_orchestration_pending"
    assert statuses["173596"] == "transport_implemented_live_pending"
    assert statuses["137386"] == "transport_implemented_live_pending"
    assert statuses["1178381"] == "transport_implemented_live_pending"
    assert statuses["1693143"] == "transport_implemented_live_pending"
    assert statuses["900001"] == "contract_implemented_orchestration_pending"
    assert report["local_checks"]["workflow_end_contract"]["exists"] is True
    assert report["local_checks"]["time_sleep_contract"]["exists"] is True
    assert report["local_checks"]["add_keyframes_contract"]["exists"] is True
    assert not any(action["kind"] == "live_fixture" for action in report["user_actions"])


def test_readiness_does_not_claim_configured_capcut_was_probed() -> None:
    archive = Path.home() / "Downloads" / check_readiness.WORKFLOW_ARCHIVE
    report = check_readiness.build_report(
        PROJECT_ROOT,
        archive=archive,
        probe=False,
        base_url="http://127.0.0.1:30000",
    )

    assert report["capcut_mate"] == {"configured": True, "probed": False}
    assert "已配置但本轮未探测" in report["interpretation"]


def test_capcut_probe_timeout_avoids_proxy_wait_for_local_service(monkeypatch) -> None:
    monkeypatch.delenv("CAPCUT_MATE_PROBE_TIMEOUT", raising=False)

    assert check_readiness._capcut_probe_timeout("http://127.0.0.1:30000") == 1.5
    assert check_readiness._capcut_probe_timeout("https://mate.example.com") == 5.0


def test_capcut_probe_timeout_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("CAPCUT_MATE_PROBE_TIMEOUT", "0.8")

    assert check_readiness._capcut_probe_timeout("http://127.0.0.1:30000") == 0.8


def test_capcut_probe_fast_fails_before_http_when_local_port_is_down(monkeypatch) -> None:
    monkeypatch.setattr(check_readiness, "_loopback_port_is_reachable", lambda _url: False)

    result = check_readiness.probe_capcut("http://127.0.0.1:30000")

    assert result == {
        "configured": True,
        "reachable": False,
        "error_type": "ConnectionRefusedError",
    }


def test_tts_readiness_uses_catalog_binding_instead_of_global_speaker_env(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "voice_catalog.json"
    catalog_path.write_text(json.dumps({
        "schema_version": "voice-catalog-v1",
        "version": "test",
        "defaults": {"male": "m", "female": "", "neutral": ""},
        "voices": [{
            "voice_key": "m", "display_name": "已验收男声", "provider": "volcengine",
            "voice_type": "official", "speaker_id": "S_M", "resource_id": "seed-tts-2.0",
            "model": "seed-tts-2.0-standard", "gender": "male", "languages": ["zh-CN"],
            "style_tags": [], "enabled": True, "verified": True,
            "training_status": "ready", "authorization_status": "verified", "preview_url": "",
            "catalog_version": "test", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("VOICE_CATALOG_PATH", str(catalog_path))
    monkeypatch.delenv("ARK_TTS_SPEAKER_ID", raising=False)

    result = check_readiness.inspect_tts_voice_catalog(PROJECT_ROOT)

    assert result["status"] == "ready"
    assert result["selectable_count"] == 1
