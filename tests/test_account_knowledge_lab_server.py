from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from tools.account_knowledge_lab_server import LabService, _RequestHandler
from workflow_1256.account_knowledge.contracts import WorkCard
from workflow_1256.account_knowledge.offline_candidates import build_offline_candidate_bundle, write_offline_candidate_notes
from workflow_1256.account_knowledge.indexer import LocalKnowledgeIndex
from workflow_1256.topic_knowledge_v2 import EventCard, build_topic_plan


class _RuntimeKnowledgeProvider:
    def distill_work_card(self, *, account_id, title, source_content, source_url="", platform="", published_at="", source_ref="", prompt_version=""):
        return WorkCard(
            work_id="work-live",
            account_id=account_id,
            title=title,
            source_url=source_url,
            platform=platform,
            published_at=published_at,
            source_ref=source_ref,
            facts=["已核验事实"],
            core_thesis="核心命题",
            angle="结构角度",
            reasoning_steps=["事实→解释"],
            content_grade="B",
            knowledge_status="REVIEW_PENDING",
        ).validate()

    def distill_account(self, *, account_id, works, surface_analysis=None, batch_size=8):
        return {
            "language_dna": {"sentence_patterns": ["短句推进"]},
            "structure_templates": ["事实→解释→结论"],
            "cognition_framework": [
                {
                    "title": "结构角度",
                    "claim": "先看结构再看情绪",
                    "evidence_work_ids": [works[0]["work_id"]],
                }
            ],
            "writing_dna": "先看事实，再解释结构。",
        }

    def synthesize_topic(self, *, topic_plan):
        return topic_plan


class _RuntimeCopywritingProvider(_RuntimeKnowledgeProvider):
    def draft(self, *, topic_plan, style_profile_id, requirements):
        evidence_refs = list(topic_plan.get("evidence_refs") or [])
        return {
            "draft_copy": "先核验新闻事实，再用目标账号的历史认知解释它。",
            "claim_trace": [
                {
                    "claim": "新闻事实",
                    "source_type": "news",
                    "source_refs": list(topic_plan["event_card"].get("source_refs") or []),
                },
                {
                    "claim": "账号解读角度",
                    "source_type": "knowledge",
                    "knowledge_refs": evidence_refs,
                },
            ],
            "knowledge_refs": evidence_refs,
            "style_application": ["hook：问题开场：保持事实优先"],
        }


class _CapturingCopywritingProvider(_RuntimeCopywritingProvider):
    def __init__(self):
        self.last_requirements = None

    def draft(self, *, topic_plan, style_profile_id, requirements):
        self.last_requirements = dict(requirements)
        return super().draft(topic_plan=topic_plan, style_profile_id=style_profile_id, requirements=requirements)


def _setup(tmp_path) -> LabService:
    service = LabService(vault_path=tmp_path / "vault", output_path=tmp_path / "lab")
    service.repository.initialize_account("creator_a")
    service.repository.save_work_card(
        WorkCard(
            work_id="work-a",
            account_id="creator_a",
            title="监管与利益结构",
            angle="从利益结构解释监管变化",
            core_thesis="规则变化会重新分配利益",
            facts=["监管新规"],
            content_grade="S",
            knowledge_status="APPROVED",
        ),
        body="人工备注：保留这段上下文。",
    )
    return service


def _event() -> dict:
    return {
        "event_id": "event-1",
        "title": "监管政策变化",
        "verified_facts": ["监管部门发布新规"],
        "source_refs": [{"url": "https://news.example.com/1"}],
    }


def test_service_dispatch_rebuilds_index_and_persists_topic_plan(tmp_path) -> None:
    service = _setup(tmp_path)
    assert service.health()["production_chain_touched"] is False
    status, response = service.dispatch("POST", "/v1/index/rebuild", {})
    assert status == 200
    assert response["documents"] == 1
    status, response = service.dispatch(
        "POST",
        "/v1/topic-plan",
        {"event_card": _event(), "target_account_id": "creator_a"},
    )
    assert status == 200
    assert response["topic_plan"]["status"] == "REVIEW_REQUIRED"
    assert response["artifact_path"]


def test_service_draft_requires_provider_and_allows_explicit_injected_result(tmp_path) -> None:
    service = _setup(tmp_path)
    service.rebuild_index()
    _, topic_response = service.dispatch(
        "POST",
        "/v1/topic-plan",
        {"event_card": _event(), "target_account_id": "creator_a"},
    )
    topic_plan = topic_response["topic_plan"]
    status, blocked = service.dispatch(
        "POST",
        "/v1/draft",
        {"topic_plan": topic_plan, "style_profile_id": "style-a"},
    )
    assert status == 424
    assert blocked["status"] == "blocked"
    status, response = service.dispatch(
        "POST",
        "/v1/draft",
        {
            "topic_plan": topic_plan,
            "style_profile_id": "style-a",
            "provider_result": {
                "draft_copy": "先确认事实，再解释规则变化。",
                "knowledge_refs": [{"account_id": "creator_a", "work_id": "work-a"}],
            },
        },
    )
    assert status == 200
    assert response["draft"]["review_status"] == "REVIEW_PENDING"
    assert response["provider_mode"] == "injected_test"


def test_service_news_to_copy_is_single_entrypoint_with_injected_provider(tmp_path) -> None:
    service = _setup(tmp_path)
    status, response = service.dispatch(
        "POST",
        "/v1/news-to-copy",
        {
            "event_card": _event(),
            "target_account_id": "creator_a",
            "style_profile_id": "style-a",
            "provider_mode": "injected_test",
            "provider_result": {
                "draft_copy": "先确认新闻事实，再用账号视角解释变化。",
                "knowledge_refs": [{"account_id": "creator_a", "work_id": "work-a"}],
            },
        },
    )
    assert status == 200
    assert response["entrypoint"] == "/v1/news-to-copy"
    assert response["draft"]["draft_copy"].startswith("先确认新闻事实")
    assert response["draft"]["knowledge_refs"][0]["work_id"] == "work-a"
    assert response["topic_artifact_path"]
    assert response["draft"]["draft_id"]


def test_service_news_to_copy_runtime_path_keeps_account_evidence_and_review_gate(tmp_path) -> None:
    service = _setup(tmp_path)
    service._model_provider = _RuntimeCopywritingProvider()
    status, response = service.dispatch(
        "POST",
        "/v1/news-to-copy",
        {
            "event_card": _event(),
            "target_account_id": "creator_a",
            "style_profile_id": "style-a",
            "provider_mode": "runtime",
        },
    )
    assert status == 200
    assert response["provider_mode"] == "runtime"
    assert response["draft"]["review_status"] == "REVIEW_PENDING"
    assert response["draft"]["knowledge_refs"][0]["work_id"] == "work-a"
    assert response["topic_artifact_path"]


def test_service_only_uses_pending_account_dna_when_explicitly_requested(tmp_path) -> None:
    service = _setup(tmp_path)
    bundle = build_offline_candidate_bundle(
        account_id="creator_a",
        works=[
            WorkCard(
                work_id="work-a",
                account_id="creator_a",
                title="监管与利益结构",
                angle="从利益结构解释监管变化",
                core_thesis="规则变化会重新分配利益",
                facts=["监管新规"],
                source_ref="raw_works/work-a.md",
                knowledge_status="APPROVED",
            )
        ],
        source_texts={"work-a": "这是一篇足够的本地原文，用于生成候选账号笔记。"},
    )
    write_offline_candidate_notes(service.repository, bundle)
    provider = _CapturingCopywritingProvider()
    service._model_provider = provider
    base_payload = {
        "event_card": _event(),
        "target_account_id": "creator_a",
        "style_profile_id": "style-a",
        "provider_mode": "runtime",
    }
    status, response = service.dispatch("POST", "/v1/news-to-copy", base_payload)
    assert status == 200
    assert response["account_knowledge_status"] == "REVIEW_PENDING"
    assert response["account_knowledge_context_used"] is False
    assert "account_knowledge_context" not in provider.last_requirements

    status, response = service.dispatch(
        "POST", "/v1/news-to-copy", {**base_payload, "include_pending_account_dna": True}
    )
    assert status == 200
    assert response["account_knowledge_status"] == "REVIEW_PENDING"
    assert response["account_knowledge_context_used"] is True
    assert provider.last_requirements["account_knowledge_context"]["available"] is True


def test_service_promote_requires_manual_approval_and_writes_separate_note(tmp_path) -> None:
    service = _setup(tmp_path)
    plan = build_topic_plan(
        event_card=EventCard(**_event()),
        target_account_id="creator_a",
        recommended_angle="从结构解释变化",
        evidence_refs=[{"account_id": "creator_a", "work_id": "work-a"}],
    )
    draft_status, draft_response = service.dispatch(
        "POST",
        "/v1/draft",
        {
            "topic_plan": plan,
            "style_profile_id": "style-a",
            "provider_result": {"draft_copy": "人工审核前的文案"},
        },
    )
    assert draft_status == 200
    draft = draft_response["draft"]
    status, _ = service.dispatch("POST", "/v1/promote", {"draft": draft, "manual_approval": False})
    assert status == 400
    status, response = service.dispatch("POST", "/v1/promote", {"draft": draft, "manual_approval": True})
    assert status == 200
    assert response["handoff"]["approved_copy"] == "人工审核前的文案"
    assert response["approved_note_path"]


def test_service_runtime_distillation_routes_explicitly_and_writes_vault(tmp_path) -> None:
    service = LabService(vault_path=tmp_path / "vault", output_path=tmp_path / "lab")
    service._model_provider = _RuntimeKnowledgeProvider()
    status, response = service.dispatch(
        "POST",
        "/v1/work-distill",
        {
            "account_id": "creator_a",
            "title": "实时作品",
            "source_content": "这是一篇足够长的作品正文，用于验证模型蒸馏结果会写入原始来源和作品卡。" * 2,
        },
    )
    assert status == 200
    assert response["work_card"]["knowledge_status"] == "REVIEW_PENDING"
    assert service.repository.list_work_cards("creator_a")[0].work_id == "work-live"
    metadata = service.repository.load_work_metadata("creator_a", "work-live")
    assert metadata["article_type"] == "模型蒸馏待审核"

    status, response = service.dispatch("POST", "/v1/account-distill", {"account_id": "creator_a"})
    assert status == 200
    assert response["bundle"]["account_id"] == "creator_a"
    assert any(path.endswith("Writing-DNA.md") for path in response["paths"])


def test_service_account_distill_blocks_missing_raw_source(tmp_path) -> None:
    service = _setup(tmp_path)
    service._model_provider = _RuntimeKnowledgeProvider()
    status, response = service.dispatch("POST", "/v1/account-distill", {"account_id": "creator_a"})
    assert status == 409
    assert response["status"] == "blocked"
    assert "完整原文" in response["message"]


def test_service_negative_search_is_separate_from_normal_retrieval(tmp_path) -> None:
    service = _setup(tmp_path)
    service.repository.save_work_card(
        WorkCard(
            work_id="work-c",
            account_id="creator_a",
            title="监管失败案例",
            angle="错误归因",
            core_thesis="失败案例只用于避错",
            facts=["监管新规"],
            content_grade="C",
            knowledge_status="APPROVED",
        )
    )
    status, response = service.dispatch(
        "POST",
        "/v1/negative-cases/search",
        {"account_id": "creator_a", "query": "监管 失败", "top_k": 5},
    )
    assert status == 200
    assert [item["record"]["work_id"] for item in response["results"]] == ["work-c"]
    assert response["provider_mode"] == "local_retrieval"


def test_service_manual_grade_writes_current_card_and_history(tmp_path) -> None:
    service = _setup(tmp_path)
    status, response = service.dispatch(
        "POST",
        "/v1/grade-work",
        {
            "account_id": "creator_a",
            "work_id": "work-a",
            "content_grade": "S",
            "performance_grade": "A",
            "reviewer": "editor",
            "notes": "代表作，事实和推理均可回溯",
        },
    )
    assert status == 200
    assert response["work_card"]["knowledge_status"] == "APPROVED"
    assert response["work_card"]["retrieval_weight"] == 1.0
    assert response["grade_card"]["reviewer"] == "editor"
    assert response["grade_card_path"]
    _card, body = service.repository.load_work_card("creator_a", "work-a")
    assert "人工备注" in body


def test_http_handler_exposes_health_and_rejects_unknown_route(tmp_path) -> None:
    service = _setup(tmp_path)

    class Handler(_RequestHandler):
        pass

    Handler.service = service
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        root = response.read().decode("utf-8")
        assert response.status == 200
        assert "显示高级工具" in root
        assert 'id="lab-tools" hidden' in root
        assert "风格包升级支线" in root
        connection.request("GET", "/status")
        response = connection.getresponse()
        status_payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert status_payload["entrypoint"] == "POST /v1/news-to-copy"
        assert status_payload["diagnostic_ui"] == "/ui"
        connection.request("GET", "/ui")
        response = connection.getresponse()
        ui = response.read().decode("utf-8")
        assert response.status == 200
        assert "账号认知知识库" in ui
        assert 'id="lab-tools" hidden' in ui
        assert "127.0.0.1:8790" in ui
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["mode"] == "offline_lab"
        connection.request("POST", "/v1/unknown", body="{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 404
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
