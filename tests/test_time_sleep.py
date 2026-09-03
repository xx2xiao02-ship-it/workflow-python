from __future__ import annotations

import pytest

from workflow_1256.time_sleep import TimeSleepValidationError, run_time_sleep


def test_time_sleep_passes_yaml_seconds_to_injected_sleeper() -> None:
    observed: list[float] = []

    result = run_time_sleep({"seconds": 175}, sleeper=observed.append)

    assert result == {"seconds": 175}
    assert observed == [175]


def test_time_sleep_zero_uses_real_sleeper_without_delay() -> None:
    assert run_time_sleep({"seconds": 0}) == {"seconds": 0}


@pytest.mark.parametrize("seconds", [True, 1.5, "175", -1])
def test_time_sleep_rejects_non_yaml_integer(seconds) -> None:
    with pytest.raises(TimeSleepValidationError, match="seconds"):
        run_time_sleep({"seconds": seconds}, sleeper=lambda _seconds: None)


def test_time_sleep_accepts_nested_json_input() -> None:
    observed: list[float] = []
    result = run_time_sleep(
        '{"params":{"_input":{"seconds":2}}}',
        sleeper=observed.append,
    )

    assert result == {"seconds": 2}
    assert observed == [2]
