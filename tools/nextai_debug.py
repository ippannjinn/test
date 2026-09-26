#!/usr/bin/env python3
"""Debug client for a live NextAI Platform install, used by Claude Code on the server PC.

Credentials come from %USERPROFILE%\\.nextai\\claude.env (written by the admin console's
"Claude用アカウント発行" button or `python -m nextai agent-account --out ...`). Environment variables
of the same name override the file. Standard library only.

  python tools/nextai_debug.py health
  python tools/nextai_debug.py dashboard
  python tools/nextai_debug.py logs [server.log] [--lines 200]
  python tools/nextai_debug.py audit [--action auth.*]
  python tools/nextai_debug.py queue | models | users | workers | settings
  python tools/nextai_debug.py diagnose [--full]
  python tools/nextai_debug.py chat "質問" [--mode auto|fast|quality]
  python tools/nextai_debug.py get /api/admin/...        (any read-only endpoint)
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_env() -> dict[str, str]:
    path = Path(os.environ.get("NEXTAI_ENV", Path.home() / ".nextai" / "claude.env"))
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("NEXTAI_URL", "NEXTAI_CA", "NEXTAI_TOKEN", "NEXTAI_UI_USER", "NEXTAI_UI_PASSWORD"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if not env.get("NEXTAI_TOKEN"):
        sys.exit(f"接続情報がありません: {path}\n管理コンソール →「メンバー」→「Claude用アカウント発行」を実行してください。")
    return env


ENV: dict[str, str] = {}


def _ctx() -> ssl.SSLContext:
    ca = ENV.get("NEXTAI_CA")
    return ssl.create_default_context(cafile=ca) if ca and Path(ca).exists() else ssl.create_default_context()


def call(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    url = ENV.get("NEXTAI_URL", "https://127.0.0.1:8443").rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {ENV['NEXTAI_TOKEN']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
            text = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(text).get("error", {})
        except ValueError:
            err = {"message": text[:300]}
        sys.exit(f"HTTP {e.code} {err.get('code', '')}: {err.get('message', '')}")
    except urllib.error.URLError as e:
        sys.exit(f"接続できません ({url}): {e.reason}\nサービスが起動しているか確認してください (sc query NextAIServer)。")
    return json.loads(text) if text else {}


def show(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_dashboard(_a) -> None:
    d = call("GET", "/api/admin/dashboard")
    r, g, q, s = d["resources"], (d["resources"].get("gpus") or [{}])[0], d["queue"], d["server"]
    print(f"server v{s['version']} up {s['uptime_seconds']}s backend={s['backend_mode']} gpu={s['gpu_provider']} sandbox={s['sandbox']}")
    print(f"level={d['governor']['level']} reasons={d['governor']['reasons']}")
    print(f"cpu={r['cpu_percent']}% ram_avail={r['ram_available_mb']}MB disk_free={r['disk_free_gb']}GB")
    if g:
        print(f"gpu={g.get('name')} vram={g.get('vram_used_mb')}/{g.get('vram_total_mb')}MB own={r['own_vram_mb']} "
              f"temp={g.get('temp_c')} budget={d['governor']['vram_budget_mb']}MB")
    print(f"queue pending={q['pending']} running={q['running']} wait={q['est_wait_seconds']}s thrashing={q['thrashing']}")
    print("loaded:", [m["id"] for m in d["models_loaded"]])
    for e in d["errors"][:15]:
        print(f"  [{e['level']}] {e['logger']}: {e['message']}")


def cmd_logs(a) -> None:
    for line in call("GET", f"/api/admin/logs?name={a.name}&lines={a.lines}")["lines"]:
        print(line)


def cmd_audit(a) -> None:
    q = f"&action={a.action}" if a.action else ""
    for e in call("GET", f"/api/admin/audit?limit={a.limit}{q}")["entries"]:
        print(time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"])), e["actor_name"] or "-", e["action"], e["target"] or "",
              e["ip"] or "", json.dumps(e["details"], ensure_ascii=False) if e["details"] else "")


def cmd_diagnose(a) -> None:
    job = call("POST", "/api/admin/diagnostics/run", {"full": a.full})["job"]
    while True:
        time.sleep(2)
        j = call("GET", f"/api/admin/jobs/{job['id']}")["job"]
        if j["status"] not in ("queued", "running"):
            break
    if j["status"] != "done":
        sys.exit(f"diagnostics {j['status']}: {j['error']}")
    rep = j["result"]
    print("overall:", rep["overall"], rep["counts"])
    for c in rep["checks"]:
        print(f"[{c['status']:4}] {c['name']}: {c['value']} {c['detail']} {c['advice']}".rstrip())


def cmd_chat(a) -> None:
    r = call("POST", "/api/conversations/new/messages", {"content": a.text, "mode": a.mode})
    job_id = r["job"]["id"]
    print(f"conversation={r['conversation_id']} job={job_id}")
    url = ENV["NEXTAI_URL"].rstrip("/") + f"/api/jobs/{job_id}/events"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ENV['NEXTAI_TOKEN']}"})
    etype = None
    with urllib.request.urlopen(req, context=_ctx(), timeout=a.timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:") and etype:
                data = json.loads(line[5:])
                if etype == "delta":
                    print(data.get("text", ""), end="", flush=True)
                elif etype not in ("ping",):
                    print(f"\n<{etype}> {json.dumps(data, ensure_ascii=False)[:400]}")
                if etype == "done":
                    return


SIMPLE = {"health": "/api/health", "queue": "/api/admin/queue", "models": "/api/admin/models", "users": "/api/admin/users",
          "workers": "/api/admin/workers", "settings": "/api/admin/settings", "tokens": "/api/admin/tokens",
          "status": "/api/status", "info": "/api/admin/server/info"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in SIMPLE:
        sub.add_parser(name)
    sub.add_parser("dashboard")
    p = sub.add_parser("logs")
    p.add_argument("name", nargs="?", default="server.log")
    p.add_argument("--lines", type=int, default=200)
    p = sub.add_parser("audit")
    p.add_argument("--action")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("diagnose")
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("chat")
    p.add_argument("text")
    p.add_argument("--mode", default="auto", choices=["auto", "fast", "quality"])
    p.add_argument("--timeout", type=float, default=600)
    p = sub.add_parser("get")
    p.add_argument("path")
    a = ap.parse_args()
    ENV.update(load_env())
    if a.cmd in SIMPLE:
        show(call("GET", SIMPLE[a.cmd]))
    elif a.cmd == "get":
        show(call("GET", a.path))
    else:
        {"dashboard": cmd_dashboard, "logs": cmd_logs, "audit": cmd_audit, "diagnose": cmd_diagnose, "chat": cmd_chat}[a.cmd](a)


if __name__ == "__main__":
    main()
