from workflow_1256.style_package_store import (
    APPROVED_PROFILE_OVERVIEW,
    PENDING_PROFILE_OVERVIEW,
    StylePackageStore,
)


def test_profile_requires_explicit_approval(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "bilibili", "items": []})
    record = store.save_profile(
        profile={"profile_name": "测试风格", "overview": "说明"}, creator_url="https://example.com", collection_id=collection_id, sample_count=30
    )
    assert record["review_status"] == "PENDING_USER_REVIEW"
    assert store.approve_profile(record["style_profile_id"])["review_status"] == "APPROVED"


def test_creator_name_becomes_visible_style_package_name(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    record = store.save_profile(
        profile={"profile_name": "模型自动名称", "overview": "说明"},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=30,
        creator_name="测试博主",
    )
    assert record["creator_name"] == "测试博主"
    assert record["style_profile"]["profile_name"] == "测试博主"

    approved = store.approve_profile(record["style_profile_id"], creator_name="更新后的博主名称")
    assert approved["creator_name"] == "更新后的博主名称"
    assert approved["style_profile"]["profile_name"] == "更新后的博主名称"


def test_approved_profile_overview_no_longer_says_pending_review(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    record = store.save_profile(
        profile={"profile_name": "审核文案", "overview": PENDING_PROFILE_OVERVIEW},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=16,
    )

    approved = store.approve_profile(record["style_profile_id"])

    assert approved["style_profile"]["overview"] == APPROVED_PROFILE_OVERVIEW
    assert store.get_profile(record["style_profile_id"])["style_profile"]["overview"] == APPROVED_PROFILE_OVERVIEW


def test_legacy_approved_profile_overview_is_normalized_on_read(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    record = store.save_profile(
        profile={"profile_name": "历史审核文案", "overview": PENDING_PROFILE_OVERVIEW},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=16,
    )
    path = tmp_path / "profiles" / f"{record['style_profile_id']}.json"
    legacy = store._read(path)
    legacy["review_status"] = "APPROVED"
    store._write(path, legacy)

    assert store.get_profile(record["style_profile_id"])["style_profile"]["overview"] == APPROVED_PROFILE_OVERVIEW


def test_recently_used_profile_is_listed_before_newer_unused_profile(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    first = store.save_profile(
        profile={"profile_name": "先创建", "overview": "说明"},
        creator_url="https://www.douyin.com/user/first",
        collection_id=collection_id,
        sample_count=16,
    )
    second = store.save_profile(
        profile={"profile_name": "后创建", "overview": "说明"},
        creator_url="https://www.douyin.com/user/second",
        collection_id=collection_id,
        sample_count=16,
    )

    store.mark_profile_used(first["style_profile_id"], used_at="2099-01-01T00:00:00+00:00")

    assert [item["style_profile_id"] for item in store.list_profiles()] == [
        first["style_profile_id"], second["style_profile_id"]
    ]


def test_saving_rewrite_marks_existing_profile_as_recently_used(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    profile = store.save_profile(
        profile={"profile_name": "使用记录", "overview": "说明"},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=16,
    )

    saved = store.save_rewrite(
        style_profile_id=profile["style_profile_id"],
        case_item={"source_url": "https://www.douyin.com/video/example", "title": "案例"},
        result={"rewritten_copy": "待审核文案"},
    )

    updated = store.get_profile(profile["style_profile_id"])
    assert updated["last_used_at"] == saved["created_at"]
    assert updated["usage_count"] == 1


def test_existing_profile_can_be_renamed_to_creator_name(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    record = store.save_profile(
        profile={"profile_name": "旧模型标签", "overview": "说明"},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=30,
    )
    updated = store.rename_profile(record["style_profile_id"], creator_name="真实博主名称")
    assert updated["creator_name"] == "真实博主名称"
    assert updated["style_profile"]["profile_name"] == "真实博主名称"


def test_expression_habit_library_can_be_backfilled_without_replacing_collection(tmp_path):
    store = StylePackageStore(tmp_path)
    items = [
        {"source_url": f"https://example.com/{index}", "transcript": "为什么？比如这个例子。但是关键在于事实，所以最后回到结论。你先别急。"}
        for index in range(16)
    ]
    collection_id = store.save_collection({"platform": "douyin", "items": items})
    record = store.save_profile(
        profile={"profile_name": "表达习惯测试", "overview": "说明", "expression_habit_library": {"habits": []}},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=16,
    )

    updated = store.update_expression_habit_library(
        record["style_profile_id"],
        library={"schema_version": "expression-habit-library-v1", "habits": [{"category": "反问/问题开场"}], "selection_policy": []},
        warning="已补充",
    )

    assert updated["style_profile"]["expression_habit_library"]["habits"][0]["category"] == "反问/问题开场"
    assert updated["collection_id"] == collection_id
    assert len(store.get_collection(collection_id)["items"]) == 16


def test_profile_can_be_archived_without_destroying_the_original_record(tmp_path):
    store = StylePackageStore(tmp_path)
    collection_id = store.save_collection({"platform": "douyin", "items": []})
    record = store.save_profile(
        profile={"profile_name": "待归档风格", "overview": "说明"},
        creator_url="https://www.douyin.com/user/example",
        collection_id=collection_id,
        sample_count=30,
    )

    archived = store.archive_profile(record["style_profile_id"])

    assert archived["style_profile_id"] == record["style_profile_id"]
    assert store.list_profiles() == []
    archived_files = list((tmp_path / "archived_profiles").glob("*/" + record["style_profile_id"] + ".json"))
    assert len(archived_files) == 1


def test_rewrite_is_persisted_and_available_after_refresh(tmp_path):
    store = StylePackageStore(tmp_path)
    saved = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://www.douyin.com/video/example", "title": "案例"},
        result={"rewritten_copy": "这是一篇待审核的二创文案。"},
    )

    assert store.get_rewrite(saved["rewrite_id"])["result"]["rewritten_copy"] == "这是一篇待审核的二创文案。"
    assert [item["rewrite_id"] for item in store.list_rewrites()] == [saved["rewrite_id"]]


def test_rewrite_can_be_approved_and_exposes_locked_copy(tmp_path):
    store = StylePackageStore(tmp_path)
    saved = store.save_rewrite(
        style_profile_id="style_example",
        case_item={"source_url": "https://www.douyin.com/video/example", "title": "案例"},
        result={"rewritten_copy": "这是确认后的二创文案。"},
    )

    approved = store.approve_rewrite(saved["rewrite_id"])

    assert approved["review_status"] == "APPROVED"
    assert approved["approved_copy"] == "这是确认后的二创文案。"
    assert approved["approved_payload"] == {
        "approved_copy": "这是确认后的二创文案。",
        "style_profile_id": "style_example",
    }
