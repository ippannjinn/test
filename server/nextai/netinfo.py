from __future__ import annotations

import ipaddress
import socket

import psutil

_VIRTUAL = ("vethernet", "virtualbox", "vmware", "docker", "br-", "veth", "loopback", "wsl", "hyper-v", "vbox", "npcap")


def lan_addresses() -> list[dict]:
    out = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception:  # noqa: BLE001
        return out
    for name, entries in addrs.items():
        if any(v in name.lower() for v in _VIRTUAL):
            continue
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        for e in entries:
            if e.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.ip_address(e.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local:
                continue
            kind = "tailscale" if ip in ipaddress.ip_network("100.64.0.0/10") else "lan" if ip.is_private else "public"
            out.append({"ip": str(ip), "interface": name, "kind": kind})
    order = {"lan": 0, "tailscale": 1, "public": 2}
    return sorted(out, key=lambda x: (order[x["kind"]], x["ip"]))


def connection_urls(port: int, tls: bool, public_url: str = "") -> list[dict]:
    scheme = "https" if tls else "http"
    suffix = "" if (tls and port == 443) or (not tls and port == 80) else f":{port}"
    urls = []
    if public_url:
        urls.append({"url": public_url.rstrip("/") + "/", "kind": "public", "label": "外部アクセスURL"})
    for a in lan_addresses():
        label = {"lan": "LAN", "tailscale": "Tailscale", "public": "グローバルIP"}[a["kind"]]
        urls.append({"url": f"{scheme}://{a['ip']}{suffix}/", "kind": a["kind"], "label": f"{label} ({a['interface']})"})
    host = socket.gethostname().lower()
    urls.append({"url": f"{scheme}://{host}.local{suffix}/", "kind": "mdns", "label": "ホスト名 (.local)"})
    urls.append({"url": f"{scheme}://localhost{suffix}/", "kind": "local", "label": "このPC"})
    return urls
