"""Detect the IPv4 address selected by the default local network route."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess


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
    """Return a usable IPv4 address without sending a network packet."""
    try:
        # UDP connect selects a route in the local OS; it sends no datagram.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            selected = probe.getsockname()[0]
            if _is_lan_ipv4(selected):
                return [selected]
    except OSError:
        pass

    addresses: set[str] = set()
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
