"""Delete everything setup.py created (jev-gw-poc)."""

import json
import os
import time
from pathlib import Path

import boto3

REGION = os.getenv("AWS_REGION_NAME", "us-east-1")
NAME = "jev-gw-poc"
STATE = Path(__file__).resolve().parent / "state.json"
state = json.loads(STATE.read_text()) if STATE.exists() else {}
iam = boto3.client("iam")
agc = boto3.client("bedrock-agentcore-control", region_name=REGION)


def quiet(fn, *a, **k):
    try:
        fn(*a, **k)
        return True
    except Exception as exc:  # noqa: BLE001
        print("skip", fn.__name__, type(exc).__name__)
        return False


if state.get("target_id"):
    quiet(agc.delete_gateway_target, gatewayIdentifier=state["gateway_id"], targetId=state["target_id"])
    for _ in range(60):
        try:
            agc.get_gateway_target(gatewayIdentifier=state["gateway_id"], targetId=state["target_id"])
            time.sleep(5)
        except agc.exceptions.ResourceNotFoundException:
            break
if state.get("gateway_id"):
    quiet(agc.delete_gateway, gatewayIdentifier=state["gateway_id"])
quiet(boto3.client("lambda", region_name=REGION).delete_function, FunctionName=f"{NAME}-interceptor")
quiet(boto3.client("logs", region_name=REGION).delete_log_group, logGroupName=f"/aws/lambda/{NAME}-interceptor")
for r in (f"{NAME}-lambda", f"{NAME}-gateway"):
    quiet(iam.delete_role_policy, RoleName=r, PolicyName=f"{r}-inline")
    quiet(iam.delete_role, RoleName=r)
quiet(boto3.client("secretsmanager", region_name=REGION).delete_secret,
      SecretId=f"{NAME}/typesafe-api-key", ForceDeleteWithoutRecovery=True)
STATE.unlink(missing_ok=True)
print("teardown done")
