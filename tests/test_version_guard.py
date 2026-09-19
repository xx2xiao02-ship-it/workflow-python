from __future__ import annotations

from tools.version_guard import SEMVER_RE, check, read_version


def test_version_file_is_semver() -> None:
    version = read_version()
    assert SEMVER_RE.fullmatch(version)


def test_version_guard_reports_current_revision() -> None:
    result = check()
    assert result["status"] == "passed", result
    assert len(str(result["commit_sha"])) == 40


def test_version_guard_detects_wrong_expected_revision() -> None:
    result = check(expected_sha="0" * 40)
    assert result["status"] == "failed"
    assert any("不等于期望 SHA" in error for error in result["errors"])
