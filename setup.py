"""Create the AgentCore Gateway PoC (all names start with jev-gw-poc). Run teardown.py to remove it.

Secrets Manager (TypeSafe key) -> Lambda REQUEST interceptor -> Gateway (AWS_IAM) -> inference target (bedrock-runtime connector).
The TypeSafe key is read from the TYPESAFE_API_KEY environment variable and written only to Secrets Manager.
"""

import io
import json
import os
import time
import zipfile
from pathlib import Path

import boto3

REGION = os.getenv("AWS_REGION_NAME", "us-east-1")
NAME = "jev-gw-poc"
HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"

sts = boto3.client("sts", region_name=REGION)
iam = boto3.client("iam")
sm = boto3.client("secretsmanager", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
agc = boto3.client("bedrock-agentcore-control", region_name=REGION)
account = sts.get_caller_identity()["Account"]
state = json.loads(STATE.read_text()) if STATE.exists() else {}


def save():
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def role(name, service, policy):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
             "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="jev-gw-poc (temporary)")["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(policy))
    return arn


# 1. Secret
key = os.environ["TYPESAFE_API_KEY"]
try:
    state["secret_arn"] = sm.create_secret(Name=f"{NAME}/typesafe-api-key", SecretString=key,
                                           Description="jev-gw-poc (temporary)")["ARN"]
except sm.exceptions.ResourceExistsException:
    state["secret_arn"] = sm.put_secret_value(SecretId=f"{NAME}/typesafe-api-key", SecretString=key)["ARN"]
del key
save()
print("secret ok")

# 2. Lambda role and function
state["lambda_role_arn"] = role(f"{NAME}-lambda", "lambda.amazonaws.com", {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": state["secret_arn"]},
    {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{NAME}-interceptor*"},
]})
save()
time.sleep(10)  # IAM propagation
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(HERE / "lambda_function.py", "lambda_function.py")
    z.write(HERE / "semantic_filter_rules.json", "semantic_filter_rules.json")
fn = f"{NAME}-interceptor"
env = {"Variables": {"TYPESAFE_SECRET_ID": state["secret_arn"], "SEMANTIC_FILTER_LEVEL": "standard", "JEV_MODEL": "jev-1.13.0"}}
for attempt in range(6):
    try:
        try:
            state["lambda_arn"] = lam.create_function(
                FunctionName=fn, Runtime="python3.13", Handler="lambda_function.lambda_handler",
                Role=state["lambda_role_arn"], Code={"ZipFile": buf.getvalue()}, Timeout=15, MemorySize=256,
                Environment=env, Description="jev-gw-poc REQUEST interceptor (temporary)")["FunctionArn"]
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(FunctionName=fn, ZipFile=buf.getvalue())
            lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
            state["lambda_arn"] = lam.update_function_configuration(FunctionName=fn, Environment=env, Timeout=15)["FunctionArn"]
        break
    except lam.exceptions.InvalidParameterValueException:
        time.sleep(10)  # role not assumable yet
lam.get_waiter("function_active_v2").wait(FunctionName=fn)
save()
print("lambda ok")

# 3. Gateway role
state["gateway_role_arn"] = role(f"{NAME}-gateway", "bedrock-agentcore.amazonaws.com", {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": state["lambda_arn"]},
    {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{account}:inference-profile/*"]},
    # The bedrock-runtime connector discovers models at target creation (it also lists provisioned throughputs).
    {"Effect": "Allow", "Action": ["bedrock:List*", "bedrock:Get*"], "Resource": "*"},
]})
save()
time.sleep(10)

# 4. Gateway with the REQUEST interceptor
if "gateway_id" not in state:
    g = agc.create_gateway(
        name=NAME, roleArn=state["gateway_role_arn"], protocolType="MCP", authorizerType="AWS_IAM",
        description="jev-gw-poc (temporary)",
        interceptorConfigurations=[{"interceptor": {"lambda": {"arn": state["lambda_arn"]}},
                                    "interceptionPoints": ["REQUEST"],
                                    "inputConfiguration": {"passRequestHeaders": False}}])
    state.update(gateway_id=g["gatewayId"], gateway_url=g.get("gatewayUrl"))
    save()
while (s := agc.get_gateway(gatewayIdentifier=state["gateway_id"]))["status"] not in ("READY", "FAILED"):
    time.sleep(5)
state["gateway_url"] = s.get("gatewayUrl", state.get("gateway_url"))
save()
print("gateway", s["status"], s.get("statusReasons"))

# 5. Inference target (bedrock-runtime connector)
if "target_id" not in state:
    t = agc.create_gateway_target(
        gatewayIdentifier=state["gateway_id"], name="bedrock",
        targetConfiguration={"inference": {"connector": {"source": {"connectorId": "bedrock-runtime"}}}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])
    state["target_id"] = t["targetId"]
    save()
while (t := agc.get_gateway_target(gatewayIdentifier=state["gateway_id"], targetId=state["target_id"]))["status"] not in ("READY", "FAILED"):
    time.sleep(5)
print("target", t["status"], t.get("statusReasons"))
print(json.dumps({k: v for k, v in state.items() if k != "secret_arn"}, indent=2))
