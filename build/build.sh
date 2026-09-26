#!/usr/bin/env bash
# Builds the single-file Windows installer: dist/NextAI-Platform-Setup.exe
# Requirements: .NET SDK 8 (cross-compiles the .NET Framework 4.8 WinForms apps), python3.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/out"
PAYLOAD="$ROOT/build/payload"
DIST="$ROOT/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/server/nextai/__init__.py")"
echo "== NextAI Platform $VERSION"

rm -rf "$OUT" "$PAYLOAD"
mkdir -p "$OUT" "$PAYLOAD/app/server" "$PAYLOAD/docs" "$DIST"

echo "== building desktop apps"
dotnet build "$ROOT/desktop/NextAI.Admin/NextAI.Admin.csproj" -c Release -nologo -v q -o "$OUT/admin" -p:Version="$VERSION"
dotnet build "$ROOT/desktop/NextAI.ServiceHost/NextAI.ServiceHost.csproj" -c Release -nologo -v q -o "$OUT/host" -p:Version="$VERSION"

echo "== assembling payload"
python3 - "$ROOT" "$PAYLOAD" <<'EOF'
import shutil, sys
from pathlib import Path
root, payload = Path(sys.argv[1]), Path(sys.argv[2])
shutil.copytree(root / "server/nextai", payload / "app/server/nextai",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for f in ("requirements.lock", "pyproject.toml"):
    shutil.copy2(root / "server" / f, payload / "app/server" / f)
shutil.copy2(root / "server/nextai/catalog/models.json", payload / "catalog.json")
for f in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
    if (root / f).exists():
        shutil.copy2(root / f, payload / "docs" / f)
for f in (root / "docs").glob("*.md"):
    shutil.copy2(f, payload / "docs" / f.name)
EOF
cp "$OUT/admin/NextAI.Admin.exe" "$OUT/admin/NextAI.Admin.exe.config" "$PAYLOAD/"
cp "$OUT/host/NextAI.ServiceHost.exe" "$OUT/host/NextAI.ServiceHost.exe.config" "$PAYLOAD/"
echo "$VERSION" > "$PAYLOAD/version.txt"
python3 - "$PAYLOAD" "$OUT/payload.zip" <<'EOF'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for base, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            info = zipfile.ZipInfo(os.path.relpath(full, src).replace(os.sep, "/"), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, "rb") as f:
                z.writestr(info, f.read())
print(f"payload: {os.path.getsize(dst) / 1024:.0f} KB")
EOF

echo "== building installer"
dotnet build "$ROOT/desktop/NextAI.Setup/NextAI.Setup.csproj" -c Release -nologo -v q -o "$OUT/setup" \
  -p:Version="$VERSION" -p:PayloadZip="$OUT/payload.zip"
cp "$OUT/setup/NextAI-Platform-Setup.exe" "$DIST/NextAI-Platform-Setup.exe"
(cd "$DIST" && sha256sum NextAI-Platform-Setup.exe > NextAI-Platform-Setup.exe.sha256)
RELEASE_BASE="${RELEASE_BASE:-https://github.com/ippannjinn/test/releases/download}"
python3 - "$DIST" "$VERSION" "$RELEASE_BASE" <<'EOF'
import hashlib, json, sys
from pathlib import Path
dist, version, base = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
exe = dist / "NextAI-Platform-Setup.exe"
manifest = {"version": version, "file": exe.name, "size": exe.stat().st_size,
            "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            "url": f"{base}/v{version}/{exe.name}", "notes": "管理コンソールからワンクリックで更新できます (データ・設定・モデルは保持)"}
(dist / "update-manifest.json").write_text(json.dumps(manifest, indent=2))
EOF
ls -l "$DIST"
echo "== done: $DIST/NextAI-Platform-Setup.exe"
