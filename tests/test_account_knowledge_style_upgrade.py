from __future__ import annotations

import json

from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository
from workflow_1256.account_knowledge.style_package_upgrade import (
    DERIVED_EVIDENCE_QUALITY,
    DERIVED_SOURCE_KIND,
    run_style_package_upgrade,
)
from tools.account_knowledge_lab_server import LabService


def _write_style_fixture(root) -> None:
    (root / "profiles").mkdir(parents=True)
    (root / "rewrites").mkdir(parents=True)
    (root / "profiles" / "style_demo.json").write_text(
        json.dumps(
            {
                "style_profile_id": "style_demo",
                "review_status": "APPROVED",
                "sample_count": 16,
                "style_profile": {
                    "profile_name": "测试风格",
                    "overview": "可复用表达规则",
                    "hook_patterns": ["先提出具体问题"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "rewrites" / "rewrite_demo.json").write_text(
        json.dumps(
            {
                "rewrite_id": "rewrite_demo",
                "created_at": "2026-08-27T00:00:00+00:00",
                "review_status": "APPROVED",
                "style_profile_id": "style_demo",
                "case_source": {
                    "platform": "douyin",
                    "source_url": "https://example.com/video/1",
                    "title": "一个关于选择的案例",
                    "published_at": "2026-08-20T00:00:00+00:00",
                    "transcript": "这是足够完整的作品转录，用来验证派生作品不会被伪装成原始作品。",
                },
                "result": {
                    "content_blueprint": {
                        "topic": "选择与代价",
                        "core_claim": "选择需要同时看到收益和代价。",
                        "fact_units": [{"statement": "选择存在代价。"}],
                        "argument_chain": [{"point": "先提出问题，再解释代价。"}],
                        "structure": [{"part": "hook"}, {"part": "conclusion"}],
                    },
                    "rewritten_copy": "这是经过风格适配的派生文案，用于案例检索。",
                    "reviewed_copy": "这是人工审核过的派生文案，用于升级信号。",
                    "expression_habits_applied": [{"category": "反问/问题开场"}],
                    "audit_report": {"style_rules_applied": ["短句推进"]},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_style_package_upgrade_imports_derived_work_and_candidate_without_model(tmp_path) -> None:
    style_root = tmp_path / "style_packages"
    _write_style_fixture(style_root)
    repository = ObsidianRepository(tmp_path / "vault")

    result = run_style_package_upgrade(
        repository,
        account_id="creator_demo",
        style_root=style_root,
        style_profile_id="style_demo",
        output_root=tmp_path / "lab",
    )

    assert result["status"] == "review_required"
    assert result["provider_mode"] == "offline_no_model_call"
    assert result["import_report"]["imported"] == 1
    assert result["candidate"]["publish_ready"] is False
    assert result["candidate"]["gates"]["runtime_publish_allowed"] is False
    assert result["candidate"]["evaluation"]["status"] == "PASS"
    assert result["candidate"]["evaluation"]["source_trace_rate"] == 1.0
    assert result["candidate"]["delta"]["signal_library"]["expression_habits"] == ["反问/问题开场"]
    cards = repository.list_work_cards("creator_demo")
    assert len(cards) == 1
    assert cards[0].knowledge_status == "REVIEW_PENDING"
    metadata = repository.load_work_metadata("creator_demo", cards[0].work_id)
    assert metadata["source_kind"] == DERIVED_SOURCE_KIND
    assert metadata["evidence_quality"] == DERIVED_EVIDENCE_QUALITY
    assert metadata["rewrite_id"] == "rewrite_demo"
    assert not metadata["source_record_file"].startswith("C:")
    assert cards[0].source_ref.startswith("derived_works/")
    assert repository.load_derived_source("creator_demo", cards[0].source_ref).startswith("这是足够完整")
    assert result["candidate_path"]


def test_style_package_upgrade_is_idempotent_and_does_not_mutate_base_profile(tmp_path) -> None:
    style_root = tmp_path / "style_packages"
    _write_style_fixture(style_root)
    repository = ObsidianRepository(tmp_path / "vault")
    first = run_style_package_upgrade(
        repository,
        account_id="creator_demo",
        style_root=style_root,
        style_profile_id="style_demo",
        output_root=tmp_path / "lab",
    )
    second = run_style_package_upgrade(
        repository,
        account_id="creator_demo",
        style_root=style_root,
        style_profile_id="style_demo",
        output_root=tmp_path / "lab",
    )

    assert first["candidate"]["candidate_id"] == second["candidate"]["candidate_id"]
    assert second["import_report"]["skipped"] == 0
    assert len(repository.list_work_cards("creator_demo")) == 1
    saved_profile = json.loads((style_root / "profiles" / "style_demo.json").read_text(encoding="utf-8"))
    assert "candidate_signals" not in saved_profile["style_profile"]


def test_lab_profile_upgrade_route_is_offline_and_versioned(tmp_path) -> None:
    style_root = tmp_path / "style_packages"
    _write_style_fixture(style_root)
    service = LabService(vault_path=tmp_path / "vault", output_path=tmp_path / "lab")

    status, response = service.dispatch(
        "POST",
        "/v1/profile-upgrade",
        {
            "account_id": "creator_demo",
            "style_profile_id": "style_demo",
            "style_root": str(style_root),
            "output_root": str(tmp_path / "lab"),
        },
    )

    assert status == 200
    assert response["status"] == "review_required"
    assert response["provider_mode"] == "offline_no_model_call"
    assert response["candidate"]["candidate_id"].startswith("upgrade_")
