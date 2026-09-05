#!/opt/python/3.12/bin/python3.12
"""Exercise the only permitted network path from the disposable runner network."""

from __future__ import annotations

import socket
import sys


def connect_status(proxy: str, target: str) -> int:
    authority = f"[{target}]:443" if ":" in target else f"{target}:443"
    with socket.create_connection((proxy, 3128), timeout=10) as connection:
        request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n"
        connection.sendall(request.encode("ascii"))
        response = connection.recv(512).splitlines()[0].decode("ascii", "replace")
    parts = response.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError("proxy returned an invalid response")
    return int(parts[1])


def assert_direct_blocked(target: str) -> None:
    try:
        with socket.create_connection((target, 443), timeout=2):
            pass
    except OSError:
        return
    raise RuntimeError("a forbidden direct connection succeeded")


def main() -> None:
    proxy = sys.argv[1]
    if connect_status(proxy, "api.github.com") != 200:
        raise RuntimeError("allowlisted GitHub egress failed")
    if connect_status(proxy, "api.osv.dev") != 200:
        raise RuntimeError("allowlisted OSV egress failed")
    for target in (
        "example.com",
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "192.168.0.1",
        "::1",
        "fc00::1",
    ):
        if connect_status(proxy, target) != 403:
            raise RuntimeError("a forbidden proxy destination was not denied")
    for target in (
        "1.1.1.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
    ):
        assert_direct_blocked(target)


if __name__ == "__main__":
    main()
