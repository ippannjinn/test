## NextAI Platform — 単一EXEインストーラー

**`NextAI-Platform-Setup.exe`** を Windows 11 PC で実行するだけでセットアップできます (管理者権限が必要)。

- 環境診断 → PCに合ったモデルセットの自動選択 → ランタイム/モデルの取得 (再開可能・SHA-256検証) → Windowsサービス登録 → ヘルスチェック
- AIモデルはEXEに含まれていません。インストール中に Hugging Face / GitHub から直接ダウンロードします (標準セットで約77GB)。
- 署名なしのため SmartScreen の警告が出た場合は「詳細情報」→「実行」を選んでください。
- 改ざん確認: `NextAI-Platform-Setup.exe.sha256` と照合してください (`certutil -hashfile NextAI-Platform-Setup.exe SHA256`)。

インストール後は管理コンソールの「サーバー」→「フル診断」で実機性能を計測してください。詳細は README / docs/DEPLOYMENT.md を参照。
