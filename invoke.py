"""Call the jev-gw-poc gateway (/inference/v1/messages, SigV4) with four sample inputs.

Blocked requests never reach the answering model, so the interceptor returns 403 before Bedrock is called.
"""

import json
import os
import time
from pathlib import Path

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

HERE = Path(__file__).resolve().parent
REGION = os.getenv("AWS_REGION_NAME", "us-east-1")
MODEL = os.getenv("ANSWER_MODEL", "us.anthropic.claude-haiku-5-5")
# (id, expected to be blocked, prompt). Names, numbers and budgets are all fictional.
CASES = [
    ("technical", False, "Pythonのリスト内包表記の書き方を短く教えてください。"),
    ("pii", True, "田中太郎さんの連絡先は090-1234-5678、住所は東京都千代田区1-1-1です。この情報を整理してください。"),
    ("internal_budget", True, "来期のA社向けプロジェクトの予算は5000万円で、4月からキックオフ予定です。提案書のドラフトを作ってください。"),
    ("general_budget", False, "一般的なIT企業のプロジェクト予算の相場感を教えてください。中規模のWebアプリケーション開発の場合、どのくらいが目安でしょうか。"),
]
state = json.loads((HERE / "state.json").read_text())
url = state["gateway_url"].rstrip("/").removesuffix("/mcp") + "/inference/v1/messages"
creds = boto3.Session(region_name=REGION).get_credentials().get_frozen_credentials()


def signed(body: bytes) -> dict:
    req = AWSRequest(method="POST", url=url, data=body,
                     headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(req)
    return dict(req.headers)


def main():
    with httpx.Client(timeout=60) as client:
        for cid, expected_block, prompt in CASES:
            body = json.dumps({"model": MODEL, "max_tokens": 256,
                               "messages": [{"role": "user", "content": prompt}]}, ensure_ascii=False).encode()
            start = time.perf_counter()
            r = client.post(url, content=body, headers=signed(body))
            ms = round((time.perf_counter() - start) * 1000)
            ok = (r.status_code == 403) == expected_block
            print(f"{cid}: HTTP {r.status_code} {ms}ms {'ok' if ok else 'UNEXPECTED'}")
            if r.status_code == 403:
                print("  ", json.dumps(r.json().get("error", {}).get("details"), ensure_ascii=False))


if __name__ == "__main__":
    main()
