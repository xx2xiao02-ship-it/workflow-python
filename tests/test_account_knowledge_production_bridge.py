import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workflow_1256.account_knowledge.contracts import build_work_id, content_sha256
from workflow_1256.account_knowledge.distillation import build_work_card_from_distillation
from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository
from workflow_1256.account_knowledge.production_bridge import sync_collection_to_obsidian


class _Provider:
    def __init__(self):
        self.work_calls = 0
        self.account_calls = 0

    def distill_work_card(self, **kwargs):
        self.work_calls += 1
        return build_work_card_from_distillation(
            **kwargs,
            result={
                "facts": ["可由作品核对的事实"],
                "core_thesis": "事实经过解释形成可复用观点",
                "angle": "从结构变化解释影响",
                "reasoning_steps": ["事实→解释→影响"],
                "evidence_types": ["公开资料"],
                "counterpoints": [],
                "conclusion_boundary": "仅适用于本案例",
                "content_grade": "B",
                "performance_grade": "UNRATED",
            },
            distilled_by="test-provider",
        )

    def distill_account(self, *, account_id, works, batch_size=8):
        self.account_calls += 1
        evidence_id = works[0]["work_id"]
        return {
            "language_dna": {"lexical_patterns": ["短句推进"]},
            "structure_templates": [{"content_type": "口播", "template": ["事实→解释→结论"]}],
            "cognition_framework": [{
                "title": "先拆事实再解释",
                "claim": "先核对事实，再解释影响",
                "reasoning_pattern": "事实→解释→影响",
                "suitable_events": ["公共事件"],
                "evidence_work_ids": [evidence_id],
                "confidence": 0.8,
            }],
            "material_strategy": {"preferred_sources": ["公开资料"]},
            "visual_style": {"image_roles": ["数据卡片"]},
            "writing_dna": "先事实，后解释，结尾收束。",
        }


def _report():
    return {
        "collection_status": "completed",
        "platform": "douyin",
        "creator_id": "sec_uid_demo_123",
        "creator_url": "https://www.douyin.com/user/sec_uid_demo_123",
        "creator_name": "测试账号",
        "items": [
            {
                "video_id": str(index),
                "title": f"作品 {index}",
                "source_url": f"https://www.douyin.com/video/{index}",
                "published_at": "2026-08-28T00:00:00+00:00",
                "transcript_status": "local_whisper",
                "duration_seconds": 20,
                "transcript": (f"这是第 {index} 条足够长的完整转写内容，用于验证作品卡、原文和账号级 DNA 的接线。" * 3),
            }
            for index in range(3)
        ],
    }


def test_sync_collection_writes_obsidian_assets_and_five_dna_notes(tmp_path: Path):
    repository = ObsidianRepository(tmp_path / "vault")
    provider = _Provider()

    result = sync_collection_to_obsidian(_report(), repository=repository, provider=provider)

    assert result["status"] == "review_required"
    assert result["account_id"] == "creator_sec_uid_demo_123"
    assert result["sample_count"] == 3
    assert provider.work_calls == 3
    assert provider.account_calls == 1
    account_dir = repository.account_dir(result["account_id"])
    assert len(list((account_dir / "raw_works").glob("*.md"))) == 3
    assert len(repository.list_work_cards(result["account_id"])) == 3
    assert len(list((account_dir / "_meta").glob("*.json"))) == 3
    assert set(result["dna_note_files"]) == set(ObsidianRepository.ACCOUNT_DNA_NOTE_FILENAMES)
    context = repository.load_account_dna_context(result["account_id"], include_pending=True)
    assert context["available"] is True
    assert context["knowledge_status"] == "REVIEW_PENDING"


def test_sync_collection_reuses_same_work_cards_without_duplicate_work_distillation(tmp_path: Path):
    repository = ObsidianRepository(tmp_path / "vault")
    provider = _Provider()

    first = sync_collection_to_obsidian(_report(), repository=repository, provider=provider)
    second = sync_collection_to_obsidian(_report(), repository=repository, provider=provider)

    assert first["account_id"] == second["account_id"]
    assert provider.work_calls == 3
    assert provider.account_calls == 2
    assert second["reused_work_card_count"] == 3


def test_sync_collection_reuses_existing_raw_work_when_transcript_changes(tmp_path: Path):
    repository = ObsidianRepository(tmp_path / "vault")
    provider = _Provider()

    first = sync_collection_to_obsidian(_report(), repository=repository, provider=provider)
    account_dir = repository.account_dir(first["account_id"])
    work_id = build_work_id(
        account_id=first["account_id"],
        title="作品 0",
        source_url="https://www.douyin.com/video/0",
        published_at="2026-08-28T00:00:00+00:00",
        content=_report()["items"][0]["transcript"],
    )
    raw_path = account_dir / "raw_works" / f"{work_id}.md"
    before = raw_path.read_bytes()

    changed = _report()
    changed["items"][0]["transcript"] += "\n这是同一作品的一次不同转写结果。"
    second = sync_collection_to_obsidian(changed, repository=repository, provider=provider)

    assert second["sample_count"] == 3
    assert second["conflict_reused_work_count"] == 1
    assert second["reused_work_card_count"] == 3
    assert provider.work_calls == 3
    assert provider.account_calls == 2
    assert raw_path.read_bytes() == before
