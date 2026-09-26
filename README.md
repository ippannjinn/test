# NextAI Platform

**1つの EXE を実行するだけで、Windows 11 PC を「自己ホスト型・マルチユーザー・マルチモーダル AI プラットフォーム」にする製品**です。

- **メンバー** はブラウザ (PC / スマートフォン) から利用します。
- **管理者** は専用の Windows デスクトップアプリ「NextAI 管理コンソール」で管理します。
- 自作プログラムや既存ツールからは **OpenAI 互換 API** (`/v1`) で使えます。メンバーが自分用の API キーを発行します ([docs/API.md](docs/API.md))。
- ユーザーはモデルを選びません。**Dynamic Profile Engine** が、要求内容・会話・ツール・GPU/RAM・キュー状況から、モデル・推論深度・ツール・リソース配分をその都度決めます。

想定環境は RTX 5070 (12GB) / RAM 32GB / 空き約131GB の家庭用 PC で、身内の数十人程度の同時利用です。**ホスト PC の安定性を最優先**し、ゲームなど他のアプリと共存できるように設計しています。

## 納品物

| ファイル | 内容 |
|---|---|
| `dist/NextAI-Platform-Setup.exe` | **単一 EXE のブートストラップインストーラー** (約400KB)。実機で環境診断 → ランタイムとモデルの取得 → サービス登録 → ヘルスチェックまでを自動で行います |
| `dist/NextAI-Platform-Setup.exe.sha256` | チェックサム |
| `dist/update-manifest.json` | アップデート確認用のマニフェスト |

大型モデルは EXE に含めません。**インストーラーが実機上で Hugging Face / GitHub から直接取得**し、SHA-256 で検証します。取得は分割並列ダウンロードで、中断しても再開できます。

## セットアップ (実機)

1. `NextAI-Platform-Setup.exe` を実行します (UAC の確認が表示されます)。
2. **システム診断**で Windows・CPU・RAM・GPU・VRAM・ドライバ・CUDA・ストレージ・ネットワークを確認します。不足があれば、何が足りないかを表示します。
3. **インストール設定**では、PC に合ったモデルセットが自動で選ばれます (VRAM / RAM / 空き容量から長期運用できる構成)。
4. **管理者アカウント**を作成します。
5. 自動インストールが進みます。uv と Python 3.12、依存パッケージ (ハッシュ固定)、llama.cpp / stable-diffusion.cpp / python.wasm、モデルを取得したうえで、Windows サービスの登録、ファイアウォール設定、ショートカット作成、起動、ヘルスチェックまで行います。
6. 完了画面に**メンバー用の接続URL**が表示されます。管理コンソールで「メンバー作成」を行い、表示された招待情報を渡してください。

PowerShell・bash・Docker Compose の操作や、モデルの手動配置は不要です。同じ EXE を再実行すると、データを保持したまま**再開・修復・更新**します。アンインストールは Windows の「アプリ」から行え、以下を選べます。

- アプリのみ削除
- モデルも削除
- すべて削除

詳しくは [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) を参照してください。

### 実機で最初にやること

管理コンソールの「サーバー」→「**フル診断**」を実行してください。この PC での次の項目を実測し、PASS / WARN / FAIL で表示します。

- VRAM 使用量
- モデルのロード時間とスワップ時間
- 推論速度 (tokens/s)
- GPU 温度

実測値は VRAM 配置計画の補正 (キャリブレーション) に使われます。**クラウド開発環境では実機の GPU 性能は測定していません。** 実機固有の値はすべてこの診断で取得します。

## 構成

```
┌──────────── Windows 11 PC ─────────────────────────────────────────────┐
│  NextAIServer (Windows サービス, 自動起動/自動復旧, Job Object で保護)  │
│   └ Python (FastAPI)  ── Dynamic Profile Engine ── GPU Scheduler        │
│        │ Auth/RBAC/Session/Device │ Resource Governor (VRAM/RAM/温度/Disk)│
│        │ Files/Memory/Sandbox(WASM)/Web(SSRF対策) │ Model Manager(Hot/Warm/Cold)
│        └→ llama-server (LLM/VLM/Embedding) / sd (画像/動画) / MusicGen │
│  NextAI 管理コンソール (WinForms デスクトップアプリ, localhost 管理API)  │
└────────────────────────────────────────────────────────────────────────┘
          ▲ HTTPS (ローカルCA / 任意の証明書)
   メンバーのブラウザ・スマートフォン (信頼端末ならログイン不要)
```

| 領域 | 実装 |
|---|---|
| サーバー | `server/nextai/` (Python 3.12, FastAPI, SQLite WAL) |
| メンバー UI | `server/nextai/web/` (依存なしの SPA、厳格な CSP、モバイル対応) |
| 管理コンソール | `desktop/NextAI.Admin/` (.NET Framework 4.8 WinForms。Windows 11 標準搭載のため追加ランタイム不要) |
| サービスホスト | `desktop/NextAI.ServiceHost/` (監視・自動再起動・プロセスツリー管理・優先度の制限) |
| インストーラー | `desktop/NextAI.Setup/` (ウィザード、レジューム、アンインストーラー) |
| モデルカタログ | `server/nextai/catalog/models.json` (モデルセットと HF 取得元) |

各部の設計は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、セキュリティは [docs/SECURITY.md](docs/SECURITY.md) を参照してください。

### 既定モデル (12GB 標準セット、約77GB)

| 用途 | モデル | 配置 |
|---|---|---|
| 軽量・高速 | Qwen3 4B Instruct 2507 (Q4_K_M) | VRAM に常駐 |
| 汎用 | Qwen3 30B-A3B Instruct 2507 (MoE, Q4_K_M) | Attention と KV は VRAM、Expert は VRAM/RAM/NVMe に分割 |
| コーディング | Qwen3 Coder 30B-A3B (MoE) | 同上 |
| 画像理解・OCR | Qwen2.5-VL 7B + mmproj | 必要時にロード |
| 埋め込み | Qwen3 Embedding 0.6B | CPU |
| 画像生成 | FLUX.1 schnell (GGUF Q4) | 必要時のみ (一時的) |
| 動画生成 | Wan2.1 T2V 1.3B | 必要時のみ。短尺・低解像度 |
| 音楽生成 | MusicGen small | 必要時のみ。**非商用ライセンス** |

フルセット (`rtx12g-full`) では、推論特化の gpt-oss-20b が追加されます。管理コンソールから Hugging Face の GGUF モデルを追加することもできます。

## 開発

```bash
# サーバーテスト (モック GPU/モックモデルで全経路を検証)
cd server && pip install -r requirements.in pytest pytest-asyncio && python -m pytest -q
# C# 共通ロジックのテスト
dotnet test desktop/NextAI.Common.Tests
# 単一 EXE のビルド (.NET SDK 8 と python3。Linux からクロスビルド可能)
./build/build.sh
# ローカルでモックサーバーを起動 (GPU 不要)
cd server && python -m nextai --data-dir /tmp/nx init --port 8443
printf '[models]\nbackend_mode = "mock"\n' >> /tmp/nx/config.toml
python -m nextai --data-dir /tmp/nx create-admin --username owner
python -m nextai --data-dir /tmp/nx serve
```

CI (`.github/workflows/build.yml`) は、サーバーテスト・C# テスト・インストーラービルドを実行し、`v*` タグでリリースに EXE を添付します。

### クラウド開発環境で検証済みのこと / 実機でのみ確定すること

| クラウドで検証済み (自動テスト) | 実機で確定 (フル診断で自動計測) |
|---|---|
| 認証・信頼端末・CSRF・RBAC・データ分離・レート制限・ブルートフォース対策 | RTX 5070 での実際の VRAM 使用量 |
| Dynamic Profile・スケジューラ (公平性・エージング・ドレイン・マイクロバッチ) | 推論速度 (tokens/s) |
| VRAM 配置計画・モデルスワップ・スラッシング検出 (モック GPU) | 実際のモデルロード時間とスワップ時間 |
| llama.cpp / sd.cpp アダプタ (互換の偽バイナリで引数・SSE・ツール呼び出しを検証) | GPU 温度・負荷、複数ユーザー同時利用時の性能 |
| サンドボックスの制限 (時間・メモリ・終了コード)、SSRF 遮断 | Windows / ドライバ / CUDA 固有の問題 |
| ダウンローダー (分割・再開・チェックサム)、バックアップ・復元 | 実際のダウンロード元 (HF / GitHub) の解決 |
| ブラウザ UI (Playwright による PC・スマホでの操作) | WinForms アプリの実際の表示 |
| インストール処理 (ペイロード → ロック済み依存 → CLI 初期化) を Linux で再現 | サービスアカウントでの GPU アクセス |
| 管理コンソールとセットアップウィザードを Mono + Xvfb で起動 (ログイン、全タブ、診断、メンバー作成、インストールのエラーと再試行) | Windows 実機での UAC・サービス登録・ファイアウォール・ショートカット |

サードパーティのライセンスは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。
