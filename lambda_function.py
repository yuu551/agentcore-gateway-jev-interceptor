"""AgentCore Gateway REQUEST interceptor: Jev classifies; the Lambda decides; the gateway forwards.

Same policy as the LiteLLM custom guardrail (jev_filter.py): ask one Noul question per policy,
block if any probability >= its threshold, and fail closed when Jev can't be reached.
Inference targets use the `http` interceptor payload shape (base64 request body).
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
JEV_TIMEOUT = float(os.environ.get("JEV_TIMEOUT_SECONDS", "8"))
LEVEL = os.environ.get("SEMANTIC_FILTER_LEVEL", "standard")
SECRET_ID = os.environ["TYPESAFE_SECRET_ID"]
with open(os.path.join(os.path.dirname(__file__), "semantic_filter_rules.json"), encoding="utf-8") as fp:
    RULES = json.load(fp)
_api_key = None


def api_key():
    global _api_key
    if _api_key is None:  # cached across warm invocations
        _api_key = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"]
    return _api_key


def text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("unsupported content")
    parts = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            parts.append(block["text"])
        elif kind == "thinking":
            parts.append("[thinking]\n" + block.get("thinking", ""))
        elif kind == "tool_use":
            parts.append("[tool_use]\n" + json.dumps(block, ensure_ascii=False))
        elif kind == "tool_result":
            parts.append("[tool_result]\n" + text_of(block.get("content") or ""))
        else:
            raise ValueError("unsupported content block")
    return "\n".join(parts)


def extract_messages(body):
    """Anthropic Messages and OpenAI Chat Completions: system, tools, full history, tool I/O."""
    out = []
    if body.get("system"):
        out.append({"role": "system", "content": text_of(body["system"])})
    if body.get("tools"):
        out.append({"role": "system", "content": "[tools]\n" + json.dumps(body["tools"], ensure_ascii=False)})
    for m in body.get("messages") or []:
        content = "" if m.get("content") is None else text_of(m.get("content"))
        calls = m.get("tool_calls") or []
        extra = ["[tool_call]\n" + json.dumps(c, ensure_ascii=False) for c in calls]
        out.append({"role": m.get("role", "user"), "content": "\n".join([content, *extra]).strip()})
    if not out:
        raise ValueError("no messages")
    return out


def judge(messages):
    policies = {k: RULES["policies"][k] for k in RULES["levels"][LEVEL]}
    req = {
        "model": JEV_MODEL,
        "state": {"messages": messages},
        "questions": {k: {"type": "noul", "instructions": p["question"]} for k, p in policies.items()},
    }
    r = urllib.request.Request(JEV_URL, data=json.dumps(req).encode(), method="POST",
                               headers={"Authorization": "Bearer " + api_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=JEV_TIMEOUT) as resp:
        answers = json.loads(resp.read())["answers"]
    scores = {}
    for k in policies:
        v = answers[k]["noul"]
        if not 0 <= v <= 1:
            raise ValueError("invalid probability")
        scores[k] = v
    blocked = [k for k, p in policies.items() if scores[k] >= p["threshold"]]
    return policies, scores, blocked


def respond(status, payload):
    return {
        "interceptorOutputVersion": "1.0",
        "http": {"transformedGatewayResponse": {
            "statusCode": status,
            "contentType": "application/json",
            "body": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode(),
        }},
    }


def error_body(path, status, message, detail=None):
    if path.endswith("/v1/messages"):  # Anthropic dialect
        kind = {403: "permission_error", 400: "invalid_request_error"}.get(status, "api_error")
        return {"type": "error", "error": {"type": kind, "message": message, **({"details": detail} if detail else {})}}
    return {"error": {"message": message, "code": str(status), **({"details": detail} if detail else {})}}


def lambda_handler(event, context):
    start = time.perf_counter()
    http = event.get("http") or {}
    req = http.get("gatewayRequest") or {}
    path = req.get("path") or ""
    try:
        body = json.loads(base64.b64decode(req.get("body") or "") or b"{}")
        messages = extract_messages(body)
    except Exception as exc:  # noqa: BLE001
        logger.info(json.dumps({"path": path, "result": "rejected", "error_type": type(exc).__name__}))
        return respond(400, error_body(path, 400, "Unsupported request for the Jev filter; request not forwarded"))
    try:
        policies, scores, blocked = judge(messages)
    except Exception as exc:  # noqa: BLE001 - fail closed, never log request bodies
        status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        logger.warning(json.dumps({"path": path, "result": "unavailable", "error_type": type(exc).__name__, "upstream_status": status}))
        return respond(503, error_body(path, 503, "Jev filter unavailable; request not forwarded"))
    judge_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(json.dumps({"path": path, "scores": scores, "blocked": blocked, "judge_ms": judge_ms}))
    if blocked:
        detail = [{"policy": k, "description": policies[k]["description"], "probability": scores[k],
                   "threshold": policies[k]["threshold"]} for k in blocked]
        return respond(403, error_body(path, 403, "jev_policy_violation", detail))
    # Pass through unchanged.
    return {"interceptorOutputVersion": "1.0", "http": {"transformedGatewayRequest": {"body": req.get("body")}}}
