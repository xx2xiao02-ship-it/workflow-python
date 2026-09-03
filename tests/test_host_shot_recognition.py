from __future__ import annotations

import pytest

from workflow_1256.host_shot_recognition import (
    HostShotRecognitionTransportRequired,
    HostShotRecognitionValidationError,
    run_host_shot_recognition,
)


PARAMS = {
    "host_llm_input": {
        "director_plan": {"core": "核心"},
        "target_sec": 15,
        "candidates": [{"idx": 0}, {"idx": 1}],
    }
}


def test_host_shot_recognition_preserves_output_order() -> None:
    result = run_host_shot_recognition(
        PARAMS,
        transport=lambda request: {
            "host_idxs": [0, 1],
            "reasoning_content": str(request.host_llm_input["target_sec"]),
        },
    )

    assert list(result) == ["host_idxs", "reasoning_content"]
    assert result == {"host_idxs": [0, 1], "reasoning_content": "15"}


def test_host_shot_recognition_requires_transport() -> None:
    with pytest.raises(HostShotRecognitionTransportRequired):
        run_host_shot_recognition(PARAMS)


def test_host_shot_recognition_rejects_invalid_index_type() -> None:
    with pytest.raises(HostShotRecognitionValidationError, match="host_idxs"):
        run_host_shot_recognition(
            PARAMS,
            transport=lambda _request: {"host_idxs": [True], "reasoning_content": "x"},
        )


def test_host_shot_recognition_rejects_missing_input_object() -> None:
    with pytest.raises(HostShotRecognitionValidationError, match="host_llm_input"):
        run_host_shot_recognition({"host_llm_input": "[]"}, transport=lambda _request: {})


@pytest.mark.parametrize("host_idxs", [[1, 0], [0, 0], [2]])
def test_host_shot_recognition_rejects_prompt_contract_violations(host_idxs) -> None:
    with pytest.raises(HostShotRecognitionValidationError, match="host_idxs"):
        run_host_shot_recognition(
            PARAMS,
            transport=lambda _request: {
                "host_idxs": host_idxs,
                "reasoning_content": "x",
            },
        )
