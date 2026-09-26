# OpenAI 互換 API

NextAI Platform は OpenAI の Chat Completions / Embeddings と互換性のある API を `/v1` で提供します。
OpenAI の公式 SDK、LangChain、Continue、Open WebUI など、`base_url` を変えられるツールならそのまま使えます。

API からのリクエストも Web 画面と同じように次の仕組みを通ります。

- Dynamic Profile によるモデルの自動選択 (`model: "auto"`)
- GPU スケジューラの公平性・順番待ち・スワップ最小化
- メンバーごとのレート制限、同時実行数、1日のトークン集計

## APIキーの発行

1. ブラウザでログイン →「設定」→「APIキー (OpenAI互換)」
2. 名前と有効期限を選んで「新しいキーを発行」
3. 表示された `nxt_...` のキーを保存 (**この画面を閉じると二度と表示されません**)

- キーは本人のアカウントの権限で動きます。キーでは Web 画面用の API や管理 API は使えません。
- 不要になったキーは同じ画面の「失効」で無効化できます。アカウントの停止・パスワードリセットで全キーが失効します。
- 管理者は管理コンソールの「メンバー」→「APIトークン」で全キーの確認と失効、「AI設定」→「OpenAI互換API」で
  API 全体の有効/無効、メンバーによる発行の可否、最長有効日数、1人あたりのキー数、最大出力トークン数を設定できます。

## 接続先

| 項目 | 値 |
|---|---|
| Base URL | `https://<サーバーのアドレス>:<ポート>/v1` (設定画面に表示されます) |
| 認証 | `Authorization: Bearer nxt_...` |
| 証明書 | ローカル CA を使っている場合は、クライアントに CA 証明書 (`/ca.crt`) を信頼させてください |

## エンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/v1/models` | 使えるモデルの一覧。先頭の `auto` は自動選択 |
| POST | `/v1/chat/completions` | チャット。`stream`、`tools` (Function calling)、`response_format: {"type": "json_object"}`、`stop`、`temperature`、`top_p`、`max_tokens` / `max_completion_tokens`、`reasoning_effort`、`stream_options.include_usage` に対応。画像入力 (`image_url`) は VLM が入っている場合に使えます |
| POST | `/v1/embeddings` | 埋め込みベクトル (1回あたり256件まで) |

`model` に `auto` (または省略) を指定すると、内容・混雑状況・VRAM の状況から最適なモデルを選びます。
モデル ID を指定すると、そのモデルに固定されます (GPU の順番待ちはあります)。
非ストリーミングの応答には、選ばれたプロファイルを示す `nextai` フィールドが付きます。

ストリーミングで GPU の順番待ちが長いときは、接続を保つために SSE のコメント行 (`: waiting position=N`) が送られます。
ストリーミング中にクライアントが切断すると、推論はキャンセルされ GPU はすぐに次の人へ渡ります。

## 例

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="https://192.168.1.10:8443/v1", api_key="nxt_...")
res = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "こんにちは"}])
print(res.choices[0].message.content)

for chunk in client.chat.completions.create(model="auto", stream=True,
                                            messages=[{"role": "user", "content": "俳句を1つ"}]):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

ローカル CA の証明書を使う場合は、環境変数 `SSL_CERT_FILE` に CA 証明書のパスを指定するか、
SDK の `http_client` に `verify="ca.crt"` を設定したクライアントを渡してください。

### curl

```bash
curl --cacert ca.crt https://192.168.1.10:8443/v1/chat/completions \
  -H "Authorization: Bearer nxt_..." -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "こんにちは"}]}'
```

## エラー

| HTTP | code | 意味 |
|---|---|---|
| 401 | `invalid_api_key` | キーが無効・失効・期限切れ |
| 403 | `api_disabled` / `token_scope` | API が無効化されている / このキーでは使えない API |
| 404 | `model_not_found` | 指定したモデルが使えない |
| 429 | `rate_limited` / `too_many_jobs` | リクエストが多すぎる (`Retry-After` を参照) |
| 503 | `server_busy` / `model_unavailable` / `inference_failed` | 高負荷、モデルなし、推論の失敗 |

エラーの本文は `{"error": {"code": "...", "message": "..."}}` です (OpenAI SDK では例外として受け取れます)。
