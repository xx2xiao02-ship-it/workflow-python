from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import console_entry_registry as registry


def test_director_resolves_to_main_service() -> None:
    entry = registry.resolve_page("director")

    assert entry.url == "http://127.0.0.1:8768/director"
    assert entry.service_name == "video_production_console"
    assert entry.port == 8768
    assert entry.entry_path.endswith("tools\\video_production_console.py")
    assert entry.role == "main_production"


def test_all_production_pages_share_the_registered_main_entry() -> None:
    assert set(registry.PRODUCTION_PAGES) == {"topic-center", "writing", "director", "assets", "editing"}
    assert {entry.port for entry in registry.PRODUCTION_PAGES.values()} == {8768}
    assert {entry.entry_path for entry in registry.PRODUCTION_PAGES.values()} == {
        registry.MAIN_ENTRY_PATH
    }


def test_parallel_site_is_not_a_production_entry() -> None:
    surface = registry.NON_PRODUCTION_SURFACES["site-video-production-console"]

    assert surface["production"] is False
    assert surface["status"] == "unmanaged_prototype"
    assert surface["url"] == "http://localhost:3000"
    assert "自动打开" in surface["reason"]
    assert "director" not in registry.PRODUCTION_PAGES or "3002" not in registry.PRODUCTION_PAGES["director"].url


def test_preflight_rejects_wrong_service(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "status": "ready",
                    "service_name": "wrong_service",
                    "port": 8768,
                    "entry_path": registry.MAIN_ENTRY_PATH,
                }
            ).encode("utf-8")

    monkeypatch.setattr(registry, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(registry.ConsoleEntryError, match="服务归属不匹配"):
        registry.preflight("director")


def test_preflight_accepts_matching_service(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "status": "ready",
                    "service_name": "video_production_console",
                    "port": 8768,
                    "entry_path": registry.MAIN_ENTRY_PATH,
                }
            ).encode("utf-8")

    monkeypatch.setattr(registry, "urlopen", lambda *_args, **_kwargs: Response())

    result = registry.preflight("director")

    assert result["status"] == "ready"
    assert result["page"]["url"] == "http://127.0.0.1:8768/director"


def test_preflight_rejects_stale_code_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "status": "ready",
                    "service_name": "video_production_console",
                    "port": 8768,
                    "entry_path": registry.MAIN_ENTRY_PATH,
                    "code_fingerprint": "stale-process",
                }
            ).encode("utf-8")

    monkeypatch.setattr(registry, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(registry, "_code_fingerprint", lambda *_args, **_kwargs: "current-source")

    with pytest.raises(registry.ConsoleEntryError, match="代码版本过旧"):
        registry.preflight("director")
