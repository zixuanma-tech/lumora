"""Show the current LAN URL after a network or IP change."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from lan_network import get_lan_ipv4_addresses  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="检测当前电脑可供局域网访问的网址")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")

    addresses = get_lan_ipv4_addresses()
    if not addresses:
        print("未检测到可用的局域网 IPv4 地址。请确认电脑已连接校园网后重试。")
        return 1

    print("当前局域网网址（需先启动寻迹服务）：")
    for address in addresses:
        print(f"  http://{address}:{args.port}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
