# AgentCore Gateway × Jev インターセプター

Amazon Bedrock AgentCore Gateway の REQUEST インターセプター（Lambda）で、モデルに送る前の会話を TypeSafe の Jev に判定させるサンプルです。
どれかのポリシーの確率がしきい値以上なら、回答モデルを呼ばずに 403 を返します。

```text
クライアント ──SigV4──▶ AgentCore Gateway ──▶ 推論ターゲット（bedrock-runtime）──▶ Bedrock
                              │
                              ▼ REQUEST インターセプター
                        Lambda ──▶ Jev（TypeSafe API）
                          └ しきい値以上なら 403 を返して止める
```

> [!NOTE]
> 検証用に作ったサンプルです。本番での利用は想定していません。

## ファイル

| ファイル | 内容 |
| --- | --- |
| `lambda_function.py` | インターセプターの本体。会話を取り出して Jev に Noul の質問をまとめて送り、しきい値と比べる |
| `semantic_filter_rules.json` | ブロックしたい項目ごとの英語の質問としきい値 |
| `setup.py` | Secrets Manager・Lambda・IAM ロール・Gateway・推論ターゲットを作る（名前はすべて `jev-gw-poc` から始まる） |
| `invoke.py` | Gateway の `/inference/v1/messages` に4つの入力を送って結果を表示する |
| `teardown.py` | `setup.py` で作ったものをすべて消す |

## 動かし方

TypeSafe の API キーと、AgentCore Gateway・Lambda・IAM・Secrets Manager を作れる AWS の認証情報が必要です。

```bash
uv sync
export AWS_REGION_NAME=us-east-1

# API キーは Secrets Manager にだけ保存されます（Lambda の環境変数には入りません）
TYPESAFE_API_KEY=... uv run python setup.py

uv run python invoke.py
```

```text
technical: HTTP 200 ... ok
pii: HTTP 403 ... ok
internal_budget: HTTP 403 ... ok
general_budget: HTTP 200 ... ok
```

試し終わったら消しておきます。

```bash
uv run python teardown.py
```

## メモ

- Jev に届かないときや結果がおかしいときは、入力を送らずに 503 を返します（fail closed）。
- リクエストの本文と API キーはログに出しません。ログに出すのは確率と判定時間だけです。
- 判定する会話は AWS の外にある TypeSafe の API へ送られます。送ってよいデータかを確かめてから使ってください。
- 質問を足したいときは `semantic_filter_rules.json` に項目を足し、`levels` に入れてから `setup.py` をもう一度実行します。
