"""Local IPv4 address discovery for the optional LAN development server."""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import shutil


def _is_lan_ipv4(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return (
        (address.is_private or address.is_global)
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def get_lan_ipv4_addresses() -> list[str]:
    """Return the IPv4 address chosen by the system's default route."""
    addresses: set[str] = set()

    # A UDP connect only asks the local OS which interface it would use; no
    # packet is sent. This avoids returning virtual adapters such as WSL.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            selected = probe.getsockname()[0]
            if _is_lan_ipv4(selected):
                return [selected]
    except OSError:
        pass

    # Windows exposes adapter addresses directly through this built-in cmdlet.
    # It avoids selecting a stale address when campus Wi-Fi changes.
    powershell = shutil.which("powershell.exe")
    if powershell:
        try:
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference='SilentlyContinue'; "
                    "Get-NetIPConfiguration | "
                    "Where-Object { $_.NetAdapter.Status -eq 'Up' -and $_.IPv4DefaultGateway } | "
                    "ForEach-Object { $_.IPv4Address.IPAddress }",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            addresses.update(
                line.strip()
                for line in result.stdout.splitlines()
                if _is_lan_ipv4(line.strip())
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # On Unix, hostname resolution is a last resort if route selection fails.
    if os.name != "nt":
        try:
            for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
            ):
                candidate = sockaddr[0]
                if _is_lan_ipv4(candidate):
                    addresses.add(candidate)
        except OSError:
            pass

    return sorted(addresses, key=lambda value: tuple(int(part) for part in value.split(".")))
