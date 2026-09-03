import pytest

from workflow_1256.governed_video_request import (
    GovernedVideoRequestError,
    build_governed_video_request,
)


def test_binds_locked_timeline_director_prompt_and_sequential_frame_without_mutation():
    lock = {"shots": [{"shot_id": "s1", "timeline": {"start_us": 0, "end_us": 4_100_000}}]}
    material = {"prompt": ["逐镜头导演约束：走位=抬头；机位动机=推近"]}
    request = build_governed_video_request(
        director_lock=lock, material_output=material,
        sequential_records=[{"shot_id": "s1", "image_url": "https://frame"}], shot_id="s1",
    )
    assert request["duration"] == 4
    assert request["first_frame_url"] == "https://frame"
    assert request["timeline"] == {"start_us": 0, "end_us": 4_100_000}
    assert request["lock_mutated"] is False
    assert "主角从头到尾清晰可见" in request["prompt"]


def test_rejects_prompt_that_did_not_consume_director_context():
    with pytest.raises(GovernedVideoRequestError, match="未消费"):
        build_governed_video_request(
            director_lock={"shots": [{"shot_id": "s1", "timeline": {"start_us": 0, "end_us": 3_000_000}}]},
            material_output={"prompt": ["普通提示词"]},
            sequential_records=[{"shot_id": "s1", "image_url": "https://frame"}], shot_id="s1",
        )


def test_rejects_subthree_second_shot_in_video_request():
    with pytest.raises(GovernedVideoRequestError, match="低于 3 秒"):
        build_governed_video_request(
            director_lock={"shots": [{"shot_id": "s1", "timeline": {"start_us": 0, "end_us": 2_500_000}}]},
            material_output={"prompt": ["逐镜头导演约束：固定机位"]},
            sequential_records=[{"shot_id": "s1", "image_url": "https://frame"}], shot_id="s1",
        )
