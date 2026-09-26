# CLAUDE.md

NextAI Platform: 1つの EXE で Windows 11 PC をマルチユーザー AI サーバーにする製品。
構成・設計は README.md / docs/ARCHITECTURE.md / docs/SECURITY.md / docs/DEPLOYMENT.md を参照。

## リポジトリ

- `server/nextai/` — Python 3.12 FastAPI サーバー (Windows サービスとして常駐)。`web/` はメンバー用 SPA
- `desktop/` — .NET Framework 4.8 WinForms: `NextAI.Admin` (管理コンソール) / `NextAI.ServiceHost` / `NextAI.Setup` (単一EXEインストーラー)、`Common/` は共有コード
- `build/build.sh` — `dist/NextAI-Platform-Setup.exe` を生成 (Linux から .NET SDK 8 でクロスビルド可)
- `tools/nextai_debug.py` — 稼働中サーバーのデバッグ用クライアント (下記)

## 開発コマンド

```bash
cd server && python -m pytest -q                 # サーバーテスト (モックGPU/モックモデル)
dotnet test desktop/NextAI.Common.Tests          # C# 共通ロジック
./build/build.sh                                 # インストーラー生成
```

## リリース

1. `server/nextai/__init__.py` の `__version__` を上げる (唯一のバージョン定義。pyproject.toml も合わせる)
2. コミットメッセージに `[release]` を含めてプッシュ → CI が `v<version>` の GitHub Release を作成し
   EXE / .sha256 / update-manifest.json を添付 → 各PCの管理コンソールにワンクリック更新が表示される

## 稼働中のインストールをデバッグする (Claude 専用アカウント)

前提: **Claude Code をサーバーPC上で実行**していること (管理APIは localhost からのみ受け付ける)。

1. 接続情報は `%USERPROFILE%\.nextai\claude.env`。無ければユーザーに
   「管理コンソール →『メンバー』→『Claude用アカウント発行』」を依頼する (有効期限付き・再発行で旧情報は失効)。
   中身は秘密情報: 表示・コミット・外部送信しないこと。
2. 状態確認 (読み取り専用トークン。変更系の管理操作は 403 になる設計):
   ```
   python tools/nextai_debug.py dashboard          # リソース/Governor/キュー/ロード中モデル/直近エラー
   python tools/nextai_debug.py logs server.log --lines 300
   python tools/nextai_debug.py logs service.log    # サービスホスト (起動失敗・再起動ループ)
   python tools/nextai_debug.py logs llama-<model_id>.log
   python tools/nextai_debug.py diagnose [--full]   # PASS/WARN/FAIL。--full は実機ベンチ (数分, 利用者を待たせる)
   python tools/nextai_debug.py queue | models | workers | settings | audit
   python tools/nextai_debug.py chat "テスト" --mode fast   # claude アカウントとして実際に推論を流す
   python tools/nextai_debug.py v1 "テスト" [--stream]      # OpenAI互換API (/v1) 経由で推論を流す
   ```
   Python が無い場合は curl.exe で同等に呼べる:
   `curl.exe --ssl-no-revoke --cacert "%NEXTAI_CA%" -H "Authorization: Bearer %NEXTAI_TOKEN%" %NEXTAI_URL%/api/admin/dashboard`
3. UI 操作: ブラウザ (Playwright 等) で `NEXTAI_URL` を開き、`NEXTAI_UI_USER` / `NEXTAI_UI_PASSWORD` でログイン
   (claude はメンバー権限。ローカルCAのため `ignoreHTTPSErrors` を使うか `NEXTAI_CA` を信頼させる)。
   Web UI は通常ユーザーと同じ画面なので、表示崩れ・ストリーミング・生成物表示の検証に使える。
4. 設定変更・メンバー操作・サービス再起動・モデルのロード等の**変更操作はユーザー (管理者) に依頼**する。
   トークンでは実行できない。コード修正はこのリポジトリで行い、[release] でリリースして管理コンソールから更新。

主なログの場所 (サーバーPC): `C:\ProgramData\NextAI\logs\` (管理者権限が必要なため API 経由で読むこと)。
