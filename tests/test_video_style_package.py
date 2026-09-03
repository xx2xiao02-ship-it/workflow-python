import pytest

from workflow_1256.video_style_package import (
    FIXED_CATEGORIES,
    VideoStylePackageError,
    bind_packaging_manifest,
    build_video_style_package,
    validate_video_style_package,
)


def test_video_style_package_builds_x_plus_two_structure() -> None:
    package = build_video_style_package("测试结构", "1.0", ["图片", "数字人", "AIGC"])

    assert package["script_categories"] == ["图片", "数字人", "AIGC"]
    assert package["category_order"] == ["图片", "数字人", "AIGC", *FIXED_CATEGORIES]
    assert package["category_count"] == 5
    assert validate_video_style_package(package)["structure"] == "3+2"


def test_video_style_package_deduplicates_and_forces_fixed_categories() -> None:
    package = build_video_style_package("测试结构", "1.0", ["image", "opening", "image", "ending"])

    assert package["category_order"] == ["image", "opening", "ending"]
    assert package["script_categories"] == ["image"]


def test_packaging_manifest_must_match_upstream_style_package() -> None:
    package = build_video_style_package("测试结构", "1.0", ["image", "aigc"])
    manifest = {"categories": [{"category": "image"}, {"category": "aigc"}, {"category": "opening"}, {"category": "ending"}]}

    bound = bind_packaging_manifest(manifest, package)

    assert bound["upstream_video_style_package"]["video_style_package_id"] == package["video_style_package_id"]
    assert bound["validation"]["structure"] == "2+2"

    partial = bind_packaging_manifest({"categories": [{"category": "image"}]}, package)
    assert partial["validation"]["missing_categories"] == ["aigc", "opening", "ending"]
