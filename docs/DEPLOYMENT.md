# デプロイ / 運用ガイド

## 1. 動作要件

| 項目 | 推奨 (標準セット) | 最低 |
|---|---|---|
| OS | Windows 11 64bit | Windows 10 22H2 64bit |
| GPU | NVIDIA 12GB (RTX 5070 など)、ドライバ 570 以降 (RTX 50 シリーズ) | GPU なし (最小構成・CPU 推論のみ) |
| RAM | 32GB | 8GB |
| 空き容量 | 105GB 以上 (安全マージン 15GB を含む) | 22GB |
| ネットワーク | インストール時にインターネット接続 (PyPI / Hugging Face / GitHub) | 同左 |

WSL2 と Docker は不要です。AI サーバーは Windows 上でネイティブに動作します。

- GPU 推論: llama.cpp の CUDA ビルドを使います。GPU を認識できない場合は、自動で Vulkan ビルドに切り替えます。
- コード実行: WASM サンドボックス (python.wasm + wasmtime) で隔離します。

WSL2 VM は RAM を大きく確保するため、ホストの安定性を優先してネイティブ構成を既定にしています。

## 2. インストールで作成されるもの

| 場所 | 内容 |
|---|---|
| `C:\Program Files\NextAI\` | 管理コンソール、サービスホスト、サーバーのコード、`install.json`、`ca.crt`、`NextAI-Setup.exe` (修復・アンインストール用) |
| `C:\ProgramData\NextAI\` (変更可) | `config.toml`、`nextai.db`、`models\`、`runtime\` (Python・llama.cpp・sd.cpp・wasm)、`users\` (メンバーごとの領域)、`logs\`、`backups\`、`certs\` |
| サービス `NextAIServer` | 遅延自動起動。異常終了時は 5秒 → 15秒 → 60秒 の間隔で再起動します。実行アカウントは `NT SERVICE\NextAIServer` (仮想アカウント) |
| ファイアウォール規則 `NextAI Platform` | 選択した TCP ポートを、プライベート / ドメインのネットワークにのみ許可します (パブリックは遮断) |
| スタートメニュー `NextAI Platform` | 管理コンソール / 診断 / ブラウザで開く |

データフォルダの ACL は、SYSTEM・Administrators・サービスアカウントのみに制限されます。他のユーザーはアクセスできません。

## 3. メンバーの追加と初回ログイン

1. 管理コンソール →「メンバー」→「＋ メンバー作成」を開きます。
2. 表示された招待情報 (URL・ユーザー名・初期パスワード) を、安全な方法でメンバーに渡します。
3. メンバーはブラウザで URL を開いてログインし、初期パスワードを変更します。
4. 「この端末を信頼する」を有効にすると、その端末は最大90日間 (使うたびに延長、上限365日) 再ログインが不要になります。紛失した端末は、メンバー自身 (設定 → 信頼済み端末) または管理者 (メンバー → 信頼端末の管理) が解除できます。

### スマートフォンの証明書警告

既定の HTTPS 証明書は、この PC 内で生成したローカル CA が発行しています。警告が出る場合は、`https://<サーバー>:8443/ca.crt` をダウンロードして端末にインストールしてください。

- iOS: 設定 → 一般 → VPN とデバイス管理 → インストール後、情報 → 証明書信頼設定で有効化
- Android: 設定 → セキュリティ → 暗号化と認証情報 → 証明書のインストール → CA 証明書

CA のフィンガープリント (SHA-256) は、管理コンソールの「サーバー」タブと招待情報に表示されます。照合してからインストールしてください。

## 4. 自宅の外からのアクセス

**推奨は Tailscale (または同等の VPN) です。** ルーターのポート開放は推奨しません。

1. サーバー PC と各端末に Tailscale を入れ、同じ tailnet に参加させます。
2. 管理コンソールの接続 URL に `https://100.x.y.z:8443/` (Tailscale) が表示されるので、それを使います。
3. 正規証明書にする場合は、`tailscale cert <machine>.<tailnet>.ts.net` で証明書を取得します。取得後、「AI設定 → サーバー」で次を設定し、サーバーを再起動します。
   - `server.cert_file` / `server.key_file`: 取得した証明書と鍵のパス
   - `server.public_url`: `https://<machine>.<tailnet>.ts.net:8443/`

Cloudflare Tunnel などのリバースプロキシを使う場合の設定です。

- `server.trusted_proxies` にプロキシの IP を設定します (X-Forwarded-For を正しく扱うため)。
- `server.public_url` を設定します。
- 管理 API は既定で localhost からのみ受け付けます (`server.allow_remote_admin = false`)。外部には公開されません。

外部公開時も、次の保護が有効です。

- HTTPS と Secure / HttpOnly / SameSite Cookie
- CSRF トークン
- ログイン試行の段階的ロックアウト
- API とジョブのレート制限
- SSRF 遮断
- 監査ログ

## 5. Claude によるデバッグ (Claude 専用アカウント)

Claude Code をサーバーPC上で使うと、Claude が稼働中のサーバーを調査・UI操作できます。

1. 管理コンソール →「メンバー」→「Claude用アカウント発行」(有効日数を指定)。
   メンバー `claude` (ブラウザUI用) と APIトークンが発行され、接続情報が `%USERPROFILE%\.nextai\claude.env` に保存されます。
2. このリポジトリを開いた Claude Code に「NextAI をデバッグして」等と依頼します (手順は `CLAUDE.md`)。
3. トークンの権限: 一般API (チャット等) + 管理APIの**読み取りと診断実行のみ**。メンバー作成・設定変更・再起動などの変更操作は不可、localhost からのみ有効、期限付き、すべて監査ログに記録。
4. 不要になったら「APIトークン」から失効、または `claude` アカウントを停止してください。再発行すると以前のパスワード・トークンは無効になります。

CLI の場合 (管理者): `python -m nextai --data-dir C:\ProgramData\NextAI agent-account --days 7 --out %USERPROFILE%\.nextai\claude.env` / 失効は `--revoke`。

## 6. 更新・修復・アンインストール

- **更新 (ワンクリック)**: 管理コンソールは起動時と6時間ごとに GitHub Releases の `update-manifest.json` を確認し、新版があるとステータスバーに「⬆ vX.Y.Z に更新できます」と表示します。クリック (または「サーバー」→「アップデート確認 / 更新」) すると、新しいセットアップをダウンロード → SHA-256 検証 → `/update` モードで起動し、診断や設定画面なしで上書き更新します。会話・ファイル・設定・モデルは保持され、DB は自動移行、完了後に管理コンソールが再起動します。手動の場合は新しい EXE をそのまま実行しても同様に更新されます。更新確認先は「AI設定 → サーバー → server.update_manifest_url」で変更できます。
- **修復・再開**: 同じ EXE を再実行するか、「アプリ」→ NextAI Platform →「変更」を選びます。完了済みの処理 (ダウンロード済みで検証済みのファイルなど) はスキップされます。
- **アンインストール**: Windows の「アプリ」から実行します。以下の3段階から選べます。
  - アプリのみ削除
  - アプリとモデル・ランタイムを削除
  - すべて削除 (「削除」の入力が必要)

## 7. バックアップと復元

「サーバー」→「バックアップ作成」で、DB・設定・証明書・ユーザーデータを ZIP にまとめます。既定では7世代を保持します。

復元は「復元…」から行い、`RESTORE` の入力が必要です。サーバーが再起動して復元され、直前の状態は `restore-rollback-*` に退避されます。

CLI の場合は次のコマンドを使います (サービス停止中に実行)。

```
python -m nextai --data-dir C:\ProgramData\NextAI backup
python -m nextai --data-dir C:\ProgramData\NextAI restore backup-YYYYmmdd-HHMMSS.zip
```

## 8. トラブルシューティング

| 症状 | 確認・対処 |
|---|---|
| サービスが起動しない | `C:\ProgramData\NextAI\logs\service.log` と `server.log` を確認します (管理コンソールの「ログ」タブからも見られます) |
| GPU が使われない (診断で GPU FAIL / 推論が遅い) | ドライバを更新します。サービスアカウントで GPU にアクセスできない環境では、管理者 PowerShell で `sc.exe config NextAIServer obj= LocalSystem` を実行し、サービスを再起動します |
| CUDA ビルドが RTX 50 で動かない | インストーラーが自動で Vulkan ビルドに切り替えます。手動で行う場合は `python -m nextai --data-dir <data> runtime install --components llama.cpp --force` を実行します |
| モデルのダウンロードが途中で止まった | セットアップを再実行するか、管理コンソール →「モデル」→「ダウンロード / 再開」を選びます。部分ファイルから再開されます |
| ディスク容量不足 | 安全マージン (15GB) を下回ると、新規ダウンロード・生成・アップロードを自動で停止し、一時ファイルを削除します。「モデル」画面で不要なモデルのファイルを削除してください |
| ゲーム中に重い | 他のアプリの VRAM 使用量を検出すると、AI 側の VRAM 予算を自動で縮小します (`resources.host_friendly`)。さらに AI サーバー全体は「通常以下」の優先度で動作します |
| 診断で FAIL となりインストールできないが、状況を理解したうえで続行したい | `NextAI-Platform-Setup.exe /force` で起動すると、FAIL があっても続行できます (自己責任) |
| 管理者のパスワードを忘れた | 管理者 PowerShell で `"<data>\runtime\venv\Scripts\python.exe" -m nextai --data-dir <data> reset-password --username <名前>` を実行します (`PYTHONPATH` に `C:\Program Files\NextAI\app\server` を設定) |

## 9. 実機ベンチマーク (最初に必ず実行)

管理コンソール →「サーバー」→「フル診断」を実行します。計測する項目は次のとおりです。

- モデルロード時間と VRAM 実測値 (予測との比 → 配置計画の補正係数として保存)
- 推論速度 (生成 / プロンプト tokens/s)
- モデルスワップ時間 (汎用 MoE モデル)
- 負荷後の GPU 温度
- キュー / サンドボックス / Web / SSRF 防御の動作

結果は `diagnostics\latest.json` と DB (`bench_results`) に保存されます。
