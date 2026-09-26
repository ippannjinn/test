"""Full Python sandbox (Pyodide in Deno): installer and backend.

The end-to-end run needs a real Deno binary and Pyodide core; point NEXTAI_TEST_DENO / NEXTAI_TEST_PYODIDE at them
(e.g. from the npm packages @deno/linux-x64-glibc and pyodide) to run it, otherwise it is skipped.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest

from nextai.install import runtime as rt_mod
from nextai.install.downloader import DownloadError
from nextai.install.runtime import RuntimeInstaller, _check_integrity, pyodide_closure
from nextai.tools.sandbox import PyodideBackend, SandboxManager

from conftest import make_settings

LOCK = {"info": {}, "packages": {
    "numpy": {"file_name": "numpy-2.0-cp313-wasm32.whl", "depends": [], "sha256": ""},
    "pandas": {"file_name": "pandas-2.0-cp313-wasm32.whl", "depends": ["numpy", "python-dateutil"], "sha256": ""},
    "python-dateutil": {"file_name": "python_dateutil-2.9-py3-none-any.whl", "depends": ["six"], "sha256": ""},
    "six": {"file_name": "six-1.17-py3-none-any.whl", "depends": [], "sha256": ""},
    "scipy": {"file_name": "scipy-1.14-cp313-wasm32.whl", "depends": ["numpy"], "sha256": ""},
}}


def test_closure_follows_dependencies_and_skips_unknown():
    assert pyodide_closure(LOCK, ["pandas", "nope"]) == ["numpy", "pandas", "python-dateutil", "six"]
    assert pyodide_closure(LOCK, ["Python_Dateutil"]) == ["python-dateutil", "six"]


def _sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_integrity_check(tmp_path):
    f = tmp_path / "a.tgz"
    f.write_bytes(b"hello")
    _check_integrity(f, _sri(b"hello"))
    with pytest.raises(DownloadError):
        _check_integrity(f, _sri(b"other"))
    assert not f.exists()  # a tampered download is not kept


def _tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(data))
        evil = tarfile.TarInfo("package/../../escape.txt")
        evil.size = 1
        t.addfile(evil, io.BytesIO(b"x"))
    return buf.getvalue()


def test_install_pyodide_verifies_everything(tmp_path, monkeypatch):
    lock = json.loads(json.dumps(LOCK))
    wheels = {v["file_name"]: f"wheel {k}".encode() for k, v in lock["packages"].items()}
    for k, v in lock["packages"].items():
        v["sha256"] = hashlib.sha256(wheels[v["file_name"]]).hexdigest()
    exe = "deno.exe" if os.name == "nt" else "deno"
    deno_tgz = _tgz({f"package/{exe}": b"#!/bin/sh\n"})
    core_tgz = _tgz({"package/pyodide.mjs": b"export {}", "package/pyodide-lock.json": json.dumps(lock).encode()})
    monkeypatch.setitem(rt_mod.DENO, "win", ("@deno/win32-x64", _sri(deno_tgz)))
    monkeypatch.setitem(rt_mod.DENO, "linux", ("@deno/linux-x64-glibc", _sri(deno_tgz)))
    monkeypatch.setitem(rt_mod.PYODIDE, "integrity", _sri(core_tgz))
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        seen.append(url)
        if "deno" in url:
            body = deno_tgz
        elif url.endswith(".tgz"):
            body = core_tgz
        else:
            body = wheels[url.rsplit("/", 1)[-1]]
        if req.headers.get("range") == "bytes=0-0":
            return httpx.Response(200, content=body, headers={"content-length": str(len(body))})
        return httpx.Response(200, content=body)

    s = make_settings(tmp_path)
    inst = RuntimeInstaller(s, lambda ev: None, httpx.Client(transport=httpx.MockTransport(handler)))
    entry = inst.install_pyodide(["pandas"])
    assert entry["packages"] == ["numpy", "pandas", "python-dateutil", "six"]
    core = s.paths.runtime / entry["core"]
    assert (core / "pandas-2.0-cp313-wasm32.whl").exists() and not (core / "scipy-1.14-cp313-wasm32.whl").exists()
    assert not any(tmp_path.rglob("escape.txt"))
    assert all(u.startswith((rt_mod.NPM, rt_mod.PYODIDE["cdn"])) for u in seen)
    assert PyodideBackend(s.paths.runtime).packages == entry["packages"]

    # a wheel whose hash does not match the pinned lock is rejected
    (core / "scipy-1.14-cp313-wasm32.whl").unlink(missing_ok=True)
    wheels["scipy-1.14-cp313-wasm32.whl"] = b"tampered"
    with pytest.raises(DownloadError):
        inst.install_pyodide(["scipy"])


def test_backend_permissions_are_minimal(tmp_path):
    rt = tmp_path / "runtime"
    (rt / "tools/pyodide/core").mkdir(parents=True)
    (rt / "tools/pyodide/deno").mkdir(parents=True)
    (rt / "manifest.json").write_text(json.dumps({"python-full": {
        "exe": "tools/pyodide/deno/deno", "core": "tools/pyodide/core", "packages": ["numpy"]}}))
    b = PyodideBackend(rt)
    cmd = b.command(tmp_path / "ws", 512, 50)
    flags = [c for c in cmd if c.startswith("--allow")]
    assert flags == [f"--allow-read={rt / 'tools/pyodide/core'},{tmp_path / 'ws'}", f"--allow-write={tmp_path / 'ws'}"]
    assert "--no-remote" in cmd and "--no-prompt" in cmd


def test_manager_prefers_pyodide_and_describes_it(tmp_path):
    s = make_settings(tmp_path, sandbox={"backend": "auto"})
    rt = s.paths.runtime
    (rt / "tools/pyodide/core").mkdir(parents=True)
    (rt / "tools/pyodide/core/pyodide.mjs").write_text("")
    (rt / "tools/pyodide/deno").mkdir(parents=True)
    (rt / "tools/pyodide/deno/deno").write_text("")
    m = SandboxManager(s)
    assert m.name != "pyodide"
    (rt / "manifest.json").write_text(json.dumps({"python-full": {
        "exe": "tools/pyodide/deno/deno", "core": "tools/pyodide/core", "packages": ["numpy", "pandas", "six"]}}))
    m.reload()
    assert m.full_python and m.name == "pyodide"
    assert m.describe() == "Python 3.13 + numpy, pandas"
    from nextai.tools.registry import ALL_TOOLS, RunCode

    assert "numpy, pandas" in ALL_TOOLS["run_code"].schema()["function"]["description"]
    RunCode.python_env = "Python 3 (standard library only)"


DENO = os.environ.get("NEXTAI_TEST_DENO")
PYO = os.environ.get("NEXTAI_TEST_PYODIDE")


@pytest.mark.skipif(not (DENO and PYO), reason="needs NEXTAI_TEST_DENO and NEXTAI_TEST_PYODIDE")
async def test_pyodide_end_to_end(tmp_path):
    s = make_settings(tmp_path, sandbox={"backend": "pyodide", "timeout_seconds": 20})
    rt = s.paths.runtime
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "manifest.json").write_text(json.dumps({"python-full": {"exe": DENO, "core": PYO, "packages": []}}))
    m = SandboxManager(s)
    assert m.name == "pyodide"
    ws = tmp_path / "ws"
    (ws / "uploads").mkdir(parents=True)
    (ws / "uploads" / "in.csv").write_text("a,b\n1,2\n")
    code = (
        "import csv, socket, js\n"
        "rows = list(csv.reader(open('uploads/in.csv')))\n"
        "open('out.txt', 'w').write(str(rows))\n"
        "try:\n    socket.create_connection(('example.com', 80), timeout=2); print('NET OPEN')\n"
        "except Exception: print('net blocked')\n"
        "try:\n    js.Deno.readTextFileSync('/etc/hostname'); print('READ OPEN')\n"
        "except Exception: print('read blocked')\n"
        "try:\n    js.Deno.Command.new('sh').outputSync(); print('RUN OPEN')\n"
        "except Exception: print('run blocked')\n"
    )
    res = await m.run("u1", code, workspace=ws)
    assert res.ok, res.stderr
    assert "net blocked" in res.stdout and "read blocked" in res.stdout and "OPEN" not in res.stdout
    assert (ws / "out.txt").read_text() == "[['a', 'b'], ['1', '2']]"
    assert "out.txt" in res.files

    res = await m.run("u1", "import sys\nprint('x')\nsys.exit(3)\n", workspace=ws)
    assert res.exit_code == 3 and res.stdout.strip() == "x"
    res = await m.run("u1", "raise ValueError('boom')\n", workspace=ws)
    assert res.exit_code == 1 and "ValueError: boom" in res.stderr


def test_repair_hints():
    from nextai.tools.registry import _repair_hint
    from nextai.tools.sandbox import SandboxResult

    env = "Python 3.13 + numpy, pandas"
    assert _repair_hint(SandboxResult(True, 0, "ok", ""), env) == ""
    h = _repair_hint(SandboxResult(False, 1, "", "ModuleNotFoundError: No module named 'seaborn'"), env)
    assert "seaborn" in h and "numpy, pandas" in h
    assert "fix the cause" in _repair_hint(SandboxResult(False, 1, "", "Traceback ...\nValueError"), env)
    assert "time limit" in _repair_hint(SandboxResult(False, 124, "", "", timed_out=True), env)
