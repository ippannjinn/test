"""`python -m nextai <command>` — server, setup and maintenance entry points used by the installer."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import __version__
from .config import load_settings, write_config

log = logging.getLogger("nextai")


def setup_logging(log_dir: Path | None, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "server.log", maxBytes=10 * 2**20, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _settings(args):
    return load_settings(args.data_dir, args.config)


def _emit_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _read_password(args) -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    if os.environ.get("NEXTAI_PASSWORD"):
        return os.environ["NEXTAI_PASSWORD"]
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Password (again): "):
        raise SystemExit("passwords do not match")
    return pw


# ------------------------------------------------------------------ commands
def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app
    from .backup import apply_pending_restore
    from .netinfo import lan_addresses
    from .platform import Platform
    from .security.tls import ensure_server_cert

    s = _settings(args)
    s.paths.ensure()
    setup_logging(s.paths.logs, args.verbose)
    if apply_pending_restore(s):
        s = _settings(args)
    platform = Platform(s)
    app = create_app(platform)
    srv = s.server
    host = args.host or srv.host
    port = args.port or srv.port
    ssl = {}
    if srv.tls:
        if srv.cert_file and srv.key_file:
            ssl = {"ssl_certfile": srv.cert_file, "ssl_keyfile": srv.key_file}
        else:
            crt, key = ensure_server_cert(s.paths.certs, [a["ip"] for a in lan_addresses()])
            ssl = {"ssl_certfile": str(crt), "ssl_keyfile": str(key)}
    config = uvicorn.Config(app, host=host, port=port, log_config=None, proxy_headers=False, server_header=False,
                            timeout_keep_alive=30, limit_concurrency=1000, loop="asyncio", **ssl)
    server = uvicorn.Server(config)
    servers = [server]
    # Loopback-only plain-HTTP listener for Tailscale Funnel / tunnels (TLS is terminated by the tunnel).
    # Requests on it are always treated as remote (see SecurityMiddleware / auth.deps.via_tunnel).
    if srv.tunnel_port and srv.tunnel_port != port and _port_free("127.0.0.1", srv.tunnel_port):
        tconf = uvicorn.Config(app, host="127.0.0.1", port=srv.tunnel_port, log_config=None, proxy_headers=False,
                               server_header=False, timeout_keep_alive=30, limit_concurrency=1000, loop="asyncio",
                               lifespan="off")
        servers.append(uvicorn.Server(tconf))

    def _stop() -> None:
        for x in servers:
            x.should_exit = True

    platform.shutdown_cb = _stop
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    log.info("NextAI Platform %s listening on %s://%s:%d", __version__, "https" if ssl else "http", host, port)
    if len(servers) > 1:
        log.info("tunnel listener on http://127.0.0.1:%d (for Tailscale Funnel)", srv.tunnel_port)
    elif srv.tunnel_port:
        log.warning("tunnel port %d is not available; external access via Tailscale Funnel is disabled", srv.tunnel_port)

    async def _main() -> None:
        extra = [asyncio.create_task(x.serve()) for x in servers[1:]]
        await server.serve()
        _stop()
        await asyncio.gather(*extra, return_exceptions=True)

    asyncio.run(_main())
    return platform.exit_code


def cmd_init(args) -> int:
    from .db import Database
    from .netinfo import lan_addresses
    from .security.tls import ensure_server_cert, fingerprint_sha256

    s = load_settings(args.data_dir, None)
    s.paths.ensure()
    cfg = s.paths.config_file
    values = {"server": {"port": args.port, "host": "0.0.0.0" if args.lan else "127.0.0.1", "name": args.name}}
    if args.public_url:
        values["server"]["public_url"] = args.public_url
    if not cfg.exists() or args.force:
        write_config(cfg, values)
    Database(s.paths.db).migrate()
    crt, _ = ensure_server_cert(s.paths.certs, [a["ip"] for a in lan_addresses()])
    out = {"data_dir": str(s.paths.data_dir), "config": str(cfg), "ca_cert": str(s.paths.certs / "ca.crt"),
           "ca_fingerprint": fingerprint_sha256(s.paths.certs / "ca.crt")}
    _emit_json(out) if args.json else print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _auth(s):
    from .audit import AuditLog
    from .auth.service import AuthService
    from .db import Database

    db = Database(s.paths.db)
    db.migrate()
    return AuthService(db, s, AuditLog(db))


def cmd_create_admin(args) -> int:
    from .auth.service import AuthError

    s = _settings(args)
    auth = _auth(s)
    if auth.has_admin() and not args.force:
        print("an active admin already exists (use --force to add another)", file=sys.stderr)
        return 2
    try:
        u, _ = auth.create_user(username=args.username, password=_read_password(args), role="admin",
                                display_name=args.display_name or args.username, must_change_password=False)
    except AuthError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1
    _emit_json({"ok": True, "user_id": u["id"], "username": u["username"]})
    return 0


def cmd_reset_password(args) -> int:
    from .auth.service import AuthError

    s = _settings(args)
    auth = _auth(s)
    u = auth.get_user_by_name(args.username)
    if not u:
        print("user not found", file=sys.stderr)
        return 1
    try:
        auth.reset_password(u["id"], _read_password(args), must_change=args.must_change, actor=None, ip="cli")
        if u["state"] != "active" and args.activate:
            auth.set_state(u["id"], "active", actor=None, ip="cli")
    except AuthError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1
    _emit_json({"ok": True})
    return 0


def cmd_agent_account(args) -> int:
    from .api.admin import agent_env
    from .auth.service import AuthError

    s = _settings(args)
    auth = _auth(s)
    if args.revoke:
        u = auth.get_user_by_name("claude")
        n = auth.revoke_user_tokens(u["id"], "cli_revoked") if u else 0
        _emit_json({"revoked": n})
        return 0
    try:
        res = auth.ensure_agent_account(days=args.days, debug=not args.no_debug, actor=None, ip="cli")
    except AuthError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1

    class _P:
        settings = s

    env = agent_env(_P, res)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(env, encoding="utf-8")
        _emit_json({"ok": True, "written": args.out, "expires_at": res["expires_at"], "scopes": res["scopes"]})
    else:
        print(env)
    return 0


def cmd_migrate(args) -> int:
    from .db import Database

    s = _settings(args)
    applied = Database(s.paths.db).migrate()
    _emit_json({"applied": applied})
    return 0


def cmd_hw(args) -> int:
    from .resources.monitor import ResourceMonitor, detect_gpu_provider

    s = load_settings(args.data_dir, args.config)
    s.paths.ensure()
    snap = ResourceMonitor(s.paths.data_dir, detect_gpu_provider("auto")).sample()
    _emit_json(snap.to_dict())
    return 0


def cmd_models(args) -> int:
    from .db import Database
    from .install.models import install_models
    from .models.catalog import Catalog
    from .resources.monitor import ResourceMonitor, detect_gpu_provider

    s = _settings(args)
    s.paths.ensure()
    catalog = Catalog.load()
    if args.action == "sets":
        snap = ResourceMonitor(s.paths.data_dir, detect_gpu_provider("auto")).sample()
        vram = snap.gpu.vram_total_mb / 1024 if snap.gpu else 0
        sel = catalog.select_set(vram_gb=vram, ram_gb=snap.ram_total_mb / 1024, disk_free_gb=snap.disk_free_gb)
        sel["hardware"] = {"vram_gb": round(vram, 1), "ram_gb": round(snap.ram_total_mb / 1024, 1),
                           "disk_free_gb": snap.disk_free_gb, "gpu": snap.gpu.name if snap.gpu else None}
        _emit_json(sel)
        return 0
    if args.action == "list":
        db = Database(s.paths.db)
        db.migrate()
        rows = {r["model_id"]: r for r in db.query("SELECT model_id, installed, enabled, bytes FROM model_state")}
        _emit_json({"models": [{"id": m.id, "name": m.display_name, "kind": m.kind, "size_gb": m.size_gb,
                                "installed": bool(rows.get(m.id, {}).get("installed")),
                                "enabled": bool(rows.get(m.id, {}).get("enabled", 1))} for m in catalog.models.values()]})
        return 0
    if args.action == "install":
        ids = list(args.id or [])
        if args.set:
            ids += catalog.set_by_id(args.set)["models"]
        if not ids:
            print("specify --set or --id", file=sys.stderr)
            return 2
        db = Database(s.paths.db)
        db.migrate()
        emit = _emit_json if args.json_progress else (lambda ev: print(ev.get("event"), ev.get("model", ""),
                                                                       ev.get("file", ""), ev.get("error", ""), flush=True))
        try:
            res = install_models(s, db, catalog, list(dict.fromkeys(ids)), emit)
        except Exception as e:  # noqa: BLE001
            _emit_json({"event": "fatal", "error": str(e)})
            return 1
        _emit_json({"event": "summary", "results": res})
        return 0 if all(v == "ok" for v in res.values()) else 3
    return 2


def cmd_runtime(args) -> int:
    from .install.runtime import RuntimeInstaller

    s = _settings(args)
    s.paths.ensure()
    if args.action == "status":
        man = s.paths.runtime / "manifest.json"
        _emit_json(json.loads(man.read_text("utf-8")) if man.exists() else {})
        return 0
    emit = _emit_json if args.json_progress else (lambda ev: print(ev, flush=True))
    comps = args.components or ["llama.cpp", "sd.cpp", "python-wasm"]
    res = RuntimeInstaller(s, emit).install(comps, force=args.force)
    _emit_json({"event": "summary", "results": {k: (v if isinstance(v, str) else "ok") for k, v in res.items()}})
    critical_failed = isinstance(res.get("llama.cpp"), str) and "llama.cpp" in comps
    return 3 if critical_failed else 0


def cmd_diagnose(args) -> int:
    from .diagnostics import summarize, system_checks

    s = _settings(args)
    s.paths.ensure()
    report = summarize(system_checks(s))
    if args.json:
        _emit_json(report)
    else:
        for c in report["checks"]:
            print(f"[{c['status']:4}] {c['name']}: {c['value']} {('- ' + c['advice']) if c['advice'] else ''}")
        print(f"overall: {report['overall']}")
    return 0 if report["overall"] != "FAIL" else 1


def cmd_backup(args) -> int:
    from .backup import create_backup
    from .db import Database

    s = _settings(args)
    path = create_backup(s, Database(s.paths.db), not args.no_user_files)
    _emit_json({"backup": str(path), "size": path.stat().st_size})
    return 0


def cmd_restore(args) -> int:
    from .backup import restore_now

    s = _settings(args)
    p = Path(args.file)
    if not p.is_absolute():
        p = s.paths.backups / p
    restore_now(s, p)
    _emit_json({"ok": True})
    return 0


def cmd_urls(args) -> int:
    from .netinfo import connection_urls

    s = _settings(args)
    _emit_json({"urls": connection_urls(s.server.port, s.server.tls, s.server.public_url)})
    return 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nextai", description="NextAI Platform server")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("init")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--lan", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--name", default="NextAI Platform")
    p.add_argument("--public-url", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("create-admin")
    p.add_argument("--username", required=True)
    p.add_argument("--display-name")
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_create_admin)

    p = sub.add_parser("reset-password")
    p.add_argument("--username", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--must-change", action="store_true")
    p.add_argument("--activate", action="store_true")
    p.set_defaults(fn=cmd_reset_password)

    p = sub.add_parser("agent-account", help="Claude 専用アカウント (UI用メンバー + デバッグ用APIトークン) を発行/再発行")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--no-debug", action="store_true", help="管理APIの読み取り権限を付けない")
    p.add_argument("--out", help="接続情報 (.env) の書き出し先")
    p.add_argument("--revoke", action="store_true", help="Claude のトークンをすべて失効")
    p.set_defaults(fn=cmd_agent_account)

    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("hw-detect").set_defaults(fn=cmd_hw)

    p = sub.add_parser("models")
    p.add_argument("action", choices=["sets", "list", "install"])
    p.add_argument("--set")
    p.add_argument("--id", action="append")
    p.add_argument("--json-progress", action="store_true")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("runtime")
    p.add_argument("action", choices=["install", "status"])
    p.add_argument("--components", nargs="*")
    p.add_argument("--json-progress", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_runtime)

    p = sub.add_parser("diagnose")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_diagnose)

    p = sub.add_parser("backup")
    p.add_argument("--no-user-files", action="store_true")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore")
    p.add_argument("file")
    p.set_defaults(fn=cmd_restore)

    sub.add_parser("urls").set_defaults(fn=cmd_urls)
    sub.add_parser("version").set_defaults(fn=cmd_version)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "serve" and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING)
    return int(args.fn(args) or 0)
