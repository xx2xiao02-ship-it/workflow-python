import sys
from pathlib import Path


MONITOR_ROOT = Path(__file__).resolve().parents[1] / "services" / "douyin-monitor"
if str(MONITOR_ROOT) not in sys.path:
    sys.path.insert(0, str(MONITOR_ROOT))

import browser_runtime
from browser_runtime import browser_launch_options, resolve_browser_executable


def test_explicit_douyin_browser_path_has_priority(monkeypatch):
    executable = Path(sys.executable)
    monkeypatch.setenv("DOUYIN_BROWSER_EXECUTABLE", str(executable))

    assert resolve_browser_executable() == executable.resolve()
    assert browser_launch_options() == {"executable_path": str(executable.resolve())}


def test_missing_explicit_path_falls_back_to_installed_browser(monkeypatch):
    monkeypatch.setenv("DOUYIN_BROWSER_EXECUTABLE", "C:\\missing\\browser.exe")
    monkeypatch.setattr(browser_runtime, "_installed_browser_candidates", lambda: [Path(sys.executable)])

    assert resolve_browser_executable() == Path(sys.executable).resolve()
