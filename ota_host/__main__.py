"""Run the OTA Console desktop application."""

from __future__ import annotations

import argparse

from .desktop import run_desktop
from .web_server import run as run_web


def main() -> int:
    parser = argparse.ArgumentParser(description="OTA Console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--desktop", action="store_true", help="使用 Python 原生桌面窗口（默认）")
    mode.add_argument("--browser", action="store_true", help="使用系统浏览器打开界面")
    mode.add_argument("--no-browser", action="store_true", help="仅运行本地服务，不打开界面")
    args = parser.parse_args()

    if args.browser:
        return run_web(args.host, args.port, open_browser=True)
    if args.no_browser:
        return run_web(args.host, args.port, open_browser=False)
    return run_desktop(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
