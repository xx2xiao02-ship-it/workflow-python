"""以备用端口启动视频制作控制台，仅用于只读页面冒烟验证。"""

from __future__ import annotations

from dataclasses import replace
import os
from http.server import ThreadingHTTPServer
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))


def _dev_localappdata(port: int) -> Path:
    configured = os.environ.get("VIDEO_CONSOLE_DEV_LOCALAPPDATA", "").strip()
    if configured:
        return Path(configured).resolve()
    return (ROOT / "outputs" / f"dev-runtime-localappdata-{port}").resolve()


def configure_dev_runtime(console_module, *, host: str, port: int) -> dict[str, object]:
    """Give the smoke instance a truthful identity and make it read-only."""

    if port == 8768:
        raise ValueError("备用开发实例不能占用 8768 主服务端口")
    from tools.local_service_governance import runtime_identity

    base_spec = console_module.SERVICE_SPEC
    dev_spec = replace(
        base_spec,
        service_name=f"{base_spec.service_name}_dev",
        port=port,
        runtime_dir_name=f"{base_spec.runtime_dir_name}_dev_{port}",
    )
    console_module.HOST = host
    console_module.PORT = port
    console_module.SERVICE_SPEC = dev_spec
    console_module.DEV_READ_ONLY = True
    identity = runtime_identity(dev_spec)
    identity.update({"runtime_role": "dev_read_only", "read_only": True})
    console_module.SERVICE_IDENTITY = identity
    return identity


def main() -> int:
    host = os.environ.get("VIDEO_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("VIDEO_CONSOLE_PORT", "8770"))
    os.environ["LOCALAPPDATA"] = str(_dev_localappdata(port))
    import video_production_console as console

    configure_dev_runtime(console, host=host, port=port)
    server = ThreadingHTTPServer((host, port), console.Handler)
    print(f"video production console dev (read-only): http://{host}:{port}/editing", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
