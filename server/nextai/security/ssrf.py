"""Outbound URL safety: blocks loopback, LAN, link-local, metadata and other internal targets.

Checks happen (1) before connecting, on every DNS answer, and (2) after connecting, on the actual
peer address, which defeats DNS rebinding between the check and the connection.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata"}


class SSRFError(ValueError):
    pass


def ip_is_public(ip: str | ipaddress._BaseAddress) -> bool:
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return ip_is_public(addr.ipv4_mapped)
        if addr.sixtofour is not None:
            return ip_is_public(addr.sixtofour)
        if addr.teredo is not None:
            return False
    if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified or not addr.is_global):
        return False
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return True


def validate_url(url: str, allowed_ports: list[int] | None = None) -> tuple[str, str, int]:
    """Syntactic validation. Returns (scheme, host, port)."""
    if not isinstance(url, str) or len(url) > 4096:
        raise SSRFError("URLが不正です")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFError("http/https以外のURLは許可されていません")
    if parts.username or parts.password:
        raise SSRFError("認証情報付きURLは許可されていません")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise SSRFError("ホスト名がありません")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as e:
        raise SSRFError("ポート番号が不正です") from e
    if allowed_ports and port not in allowed_ports:
        raise SSRFError(f"ポート{port}へのアクセスは許可されていません")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise SSRFError("内部ホストへのアクセスは許可されていません")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and not ip_is_public(literal):
        raise SSRFError("プライベート/ローカルアドレスへのアクセスは許可されていません")
    if literal is None and ("." not in host or host.replace(".", "").isdigit()):
        # single-label names resolve via LAN search domains; dotted-decimal variants are ambiguous
        raise SSRFError("ホスト名が不正です")
    return scheme, host, port


async def resolve_public(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"名前解決に失敗しました: {host}") from e
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise SSRFError("名前解決結果が空です")
    bad = [ip for ip in ips if not ip_is_public(ip)]
    if bad:
        raise SSRFError("内部アドレスに解決されるホストへのアクセスは許可されていません")
    return ips


async def check_url(url: str, allowed_ports: list[int] | None = None) -> str:
    scheme, host, port = validate_url(url, allowed_ports)
    try:
        ipaddress.ip_address(host.strip("[]"))
        return url
    except ValueError:
        await resolve_public(host, port)
    return url
