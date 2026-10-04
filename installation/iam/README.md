# Deploying GROW2 without AdministratorAccess

The Quick Start asks for `AWSAdministratorAccess` because the deploy script does
two different jobs with one credential: a **one-time account setup** that
genuinely needs broad rights, and the **repeatable deploy** that does not. This
directory splits them.

| Step | Who | Permissions | How often |
|------|-----|-------------|-----------|
| 1. CDK bootstrap (`cdk bootstrap aws://<account>/<region>`) | an administrator | admin | once per account + region |
| 2. Create the deployer role (`grow2-deployer-role.yaml`) | an administrator | admin (creates IAM) | once per account |
| 3. Run `deploy-grow2-bootstrap.sh` / `delete-grow2.sh` | the operator | `grow2-operator-policy.json` | every deploy |

The CodeBuild project that actually runs the CDK deploy assumes the deployer
role, so the operator only needs to start builds, upload the source bundle, and
pass that role to CodeBuild.

## Set-up (administrator, once)

```bash
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

npx --yes cdk bootstrap aws://$ACCOUNT/$REGION

aws cloudformation deploy \
  --region $REGION \
  --stack-name grow2-deployer-role \
  --template-file installation/iam/grow2-deployer-role.yaml \
  --capabilities CAPABILITY_NAMED_IAM

aws iam create-policy \
  --policy-name grow2-operator-policy \
  --policy-document file://installation/iam/grow2-operator-policy.json
# attach grow2-operator-policy to the operator's user, group or permission set
```

## Deploying (operator)

```bash
export GROW2_DEPLOYER_POLICY_ARN=$(aws cloudformation describe-stacks \
  --stack-name grow2-deployer-role --query 'Stacks[0].Outputs[?OutputKey==`PolicyArn`].OutputValue' --output text)
./installation/deploy-grow2-bootstrap.sh us-east-1
```

When `GROW2_DEPLOYER_POLICY_ARN` is set, `deploy-via-codebuild.sh` attaches that
policy to the CodeBuild role instead of `AdministratorAccess` (and detaches
`AdministratorAccess` if a previous run attached it). When it is unset the script
behaves exactly as before, so existing installs are unaffected.

## Scope of the deployer policy

`grow2-deployer-policy.json` is the reference copy of what the role template
grants. It is service-scoped rather than resource-scoped: CDK creates resources
with generated names, so most statements use `Resource: "*"` but only for the
services the stack actually uses (CloudFormation, Amplify, AppSync, Cognito,
DynamoDB, Lambda, EventBridge, Step Functions, SQS, CloudWatch, SSM parameters,
S3, ECR, Bedrock, Bedrock AgentCore, OpenSearch Serverless, WAF, CodeBuild) plus
the IAM role/policy actions CDK needs to create execution roles. It cannot touch
IAM users, organizations, billing, or services outside that list.

> This policy was derived from the resources the stack declares, not recorded
> from a live deploy. If a deploy fails with `AccessDenied`, the CodeBuild log
> names the missing action — add it to both the JSON and the YAML and redeploy
> the role stack.
