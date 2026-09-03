from __future__ import annotations

import pytest

from workflow_1256.merge_bgm_timeline import (
    BgmMergeTransportRequired,
    BgmMergeValidationError,
    build_request,
    run_bgm_merge,
)


PARAMS = {
    "audio_urls": [
        "https://example.invalid/bgm-1.mp3",
        "https://example.invalid/bgm-2.mp3",
    ],
    "timelines": [
        {"start": 0, "end": 40_000_000},
        {"start": 36_000_000, "end": 80_000_000},
    ],
    "transition_schemes": ["crossfade"],
}


def test_merge_preserves_input_order_and_output_shape() -> None:
    request = build_request(PARAMS)
    result = run_bgm_merge(
        request,
        transport=lambda value: {
            "audio_url": "https://example.invalid/merged.mp3",
            "audio_url_list": list(value["audio_urls"]),
            "duration": 80_000_000,
        },
    )
    assert request["audio_urls"] == PARAMS["audio_urls"]
    assert result == {
        "audio_url": "https://example.invalid/merged.mp3",
        "audio_url_list": PARAMS["audio_urls"],
        "duration": 80_000_000,
    }


def test_merge_requires_explicit_transport() -> None:
    with pytest.raises(BgmMergeTransportRequired):
        run_bgm_merge(PARAMS)


@pytest.mark.parametrize(
    "params",
    [
        {**PARAMS, "audio_urls": []},
        {**PARAMS, "timelines": [{"start": 1, "end": 1}, PARAMS["timelines"][1]]},
        {**PARAMS, "transition_schemes": []},
        {**PARAMS, "audio_urls": ["synthetic://not-http.mp3"]},
    ],
)
def test_merge_rejects_invalid_contract(params) -> None:
    with pytest.raises(BgmMergeValidationError):
        build_request(params)


def test_merge_rejects_missing_output_fields() -> None:
    with pytest.raises(BgmMergeValidationError):
        run_bgm_merge(PARAMS, transport=lambda _value: {"audio_url": ""})
