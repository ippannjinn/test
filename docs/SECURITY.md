# セキュリティ設計

| 脅威 | 対策 | 実装 |
|---|---|---|
| 盗聴 | HTTPS (ローカル CA + LAN IP を SAN に含む証明書を自動で生成・更新、または任意の正規証明書)、HSTS | `security/tls.py`, `cli.py serve` |
| セッション窃取 | `__Host-` Cookie (Secure / HttpOnly / SameSite)、トークンはハッシュのみ保存、アイドル期限と絶対期限、失効 | `auth/service.py` |
| 信頼端末トークンの盗用 | 使うたびにローテーション、古いトークンの再利用を検知して失効 (同時リクエスト用に 60秒の猶予) | `refresh_with_device` |
| CSRF | セッションに紐づく CSRF トークン、Origin の検証、ログイン時の `X-Requested-With` 必須 | `auth/deps.py`, `api/auth.py` |
| XSS | 厳格な CSP (`script-src 'self'`、インライン禁止)、Markdown は先にエスケープしてから限られたタグだけを生成、リンクは http(s) のみ、アップロードファイルは `nosniff` と `sandbox` CSP 付きで配信し、HTML や SVG はダウンロード扱い | `web/md.js`, `app.py`, `api/files.py` |
| SQL インジェクション | すべてプレースホルダを使用 (動的なのはホワイトリスト内のカラム名のみ) | 全体 |
| ブルートフォース | IP / (ユーザー, IP) / ユーザー全体 の3つのキーで指数的にロックアウト、ログインのレート制限 | `authenticate` |
| DoS / 独占 | API のトークンバケット、ユーザー別のジョブ数と同時実行数、ボディサイズの上限、アップロードの Content-Length 必須 | `SecurityMiddleware`, `jobs.py` |
| SSRF | URL の構文検証、DNS の解決結果の検証、接続後の接続先 IP の検証、リダイレクトの再検証、ポートの制限 | `security/ssrf.py`, `tools/web.py` |
| 任意のコード実行 | WASM サンドボックス (ネットワークなし、プロセスなし、メモリ・時間・ディスクの上限)。コードから見えるのは会話ごとの作業ディレクトリ `/workspace` だけで、添付ファイル (uploads/)、SSRF 対策付きでホスト側が取得した Web データ (downloads/, research/) もこの中に置く。ファイル系ツールは相対パスのみ・ワークスペース外は拒否。会話の削除で作業ディレクトリも削除 | `tools/sandbox.py`, `tools/registry.py`, `runners/chat.py` |
| 生成された HTML / SVG のプレビュー | 通常のファイル配信では HTML/SVG を常にダウンロード扱い。プレビューは専用 URL のみで `CSP: sandbox allow-scripts` (same-origin なし・connect-src なし) + `frame-ancestors 'self'` のため、スクリプトはアプリの Cookie・API・画面に触れられない | `api/files.py` |
| 権限昇格 | RBAC (admin / member)、自分の情報として変更できる項目のホワイトリスト、最後の管理者を保護、管理 API は管理アプリの Bearer + localhost のみ | `auth/deps.py`, `service.py` |
| 他ユーザーのデータへのアクセス | すべてのクエリに user_id 条件、パスが自分の領域内にあるかを検証 | `services/files.py` |
| ローカルの他ユーザー / プロセス | データフォルダの ACL を SYSTEM / Admins / サービスのみに制限、llama-server は 127.0.0.1 のみで待ち受け、起動ごとのランダム API キー | `Installer.cs`, `llamacpp.py` |
| サービスアカウントの権限 | `NT SERVICE\NextAIServer` 仮想アカウントで実行 (LocalSystem は使わない) | `Installer.cs` |
| サプライチェーン | Python 依存はハッシュ固定 (`--require-hashes`)、uv は PyPI の SHA-256 で検証、uv が使えない PC 用の Python 公式パッケージ (nuget.org) は SHA-256 を固定、モデルは HF LFS の SHA-256 で検証、GitHub リリースは digest がある場合に検証 | `requirements.lock`, `downloader.py` |
| プロンプトインジェクション | ツールの結果を外部データとして囲み、システムプロンプトでその中の指示に従わないよう明示 | `runners/chat.py`, `agent.py` |
| 自動化トークン (Claude 等) | `nxt_` 付きランダムトークンをハッシュのみ保存、スコープ (member / debug / openai)、debug は管理APIの読み取りと診断実行のみ、localhost 限定、期限付き、再発行で旧トークン失効、アカウント停止・パスワードリセットで失効 | `auth/deps.py`, `service.py` |
| OpenAI 互換 API のキー | メンバーが自分で発行する `openai` スコープのみのキー (Web 用 API・管理 API・キー管理には使えない)、Cookie では `/v1` を使えない (CSRF の対象外にしない)、期限必須・1人あたりの個数上限、管理者が全体を無効化・個別に失効可能、API 経由でも GPU キュー・レート制限・同時実行数の制限を適用 | `api/openai.py`, `api/account.py` |
| 外部公開 (Tailscale Funnel) | Funnel はループバック専用の `server.tunnel_port` にだけ転送。そのポートへのリクエストは常にリモート扱い (管理 API・管理アプリのログイン拒否、プロキシヘッダがあっても localhost 扱いにしない)、接続元 IP は Tailscale が付与した X-Forwarded-For の末尾を使用、Origin は公開 URL のホストのみ追加で許可 | `app.py`, `auth/deps.py`, `cli.py` |
| アカウント削除 | 完全削除で会話・メッセージ・長期メモリ・カスタム指示・ファイル・サンドボックス作業ディレクトリ・APIキー・セッション・端末を削除。SQLite は secure_delete + WAL チェックポイントで削除済みデータを上書き。メモリ上のジョブ記録・プレビューも破棄。消せなかったフォルダは定期処理で再削除。監査ログとバックアップ ZIP は残る | `platform.purge_user`, `service.mark_deleted` |
| 外部ツール (ffmpeg / pandoc) | 初回利用時に公式 GitHub リリースから取得し SHA-256 (asset digest) で検証、アーカイブのパス逸脱・シンボリックリンクは展開しない。実行はシェルなし・NextAI が組み立てた引数のみ・サンドボックス作業ディレクトリ内のファイルのみ・タイムアウト・低優先度・最小限の環境変数。ffmpeg は `-protocol_whitelist file`、pandoc は `--sandbox` でネットワークや他ファイルを読まない。管理者は自動取得の停止・ツールごとの許可を設定可能 | `services/extools.py`, `tools/registry.py` |
| ツールの許可リスト | モデルが出力したツール名でも、そのターンに提示したツール以外は実行しない | `runners/agent.py` |
| 監査 | ログイン成功・失敗、アカウント操作、端末の登録・解除・再利用の検知、設定変更、バックアップ・復元、モデル操作を `audit_log` に記録 | `audit.py` |

## 既知の制限

- ローカル CA による HTTPS では、各端末への CA のインストールが必要です。外部公開する場合は Tailscale 証明書などの正規証明書を推奨します。
- WASM サンドボックスは Python の標準ライブラリのみ使えます (numpy などのネイティブ拡張は使えません)。
- レート制限の状態はプロセス内のメモリに保持しているため、サーバーを再起動するとリセットされます。ログイン失敗の記録は DB に保存するため、再起動しても保持されます。
