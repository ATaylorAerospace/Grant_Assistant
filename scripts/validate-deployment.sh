#!/bin/bash
# validate-deployment.sh - Check a deployed GROW2 stack against what this version
# of the code says should exist. Read-only; safe to run any time.
#
# Usage:
#   ./scripts/validate-deployment.sh <region> [--identifier <name>] [--expect-env dev|prod] [--smoke]
#
#   --identifier  GROW2_IDENTIFIER of the deployment to check (needed when several
#                 deployments share the region)
#   --expect-env  also assert the prod/dev hardening (PITR, deletion protection)
#   --smoke       invoke the US grants search Lambda with a test query and watch
#                 the AgentCore runtime log for a result (takes ~1-2 min)
#
# Companion to install_docs/deployment/VALIDATION_RUNBOOK.md. Each check prints
# PASS/FAIL/WARN; exit code is non-zero if any check FAILs.

set -uo pipefail

REGION=""; IDENT="${GROW2_IDENTIFIER:-}"; EXPECT_ENV=""; SMOKE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --identifier) IDENT="$2"; shift ;;
    --expect-env) EXPECT_ENV="$2"; shift ;;
    --smoke) SMOKE=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) REGION="$1" ;;
  esac
  shift
done
[ -z "$REGION" ] && { echo "Usage: $0 <region> [--identifier name] [--expect-env dev|prod] [--smoke]" >&2; exit 2; }
export AWS_PAGER=""

PASS=0; FAIL=0; WARN=0
pass() { PASS=$((PASS+1)); echo "  PASS  $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
warn() { WARN=$((WARN+1)); echo "  WARN  $*"; }
section() { echo; echo "== $*"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "GROW2 deployment validation — account $ACCOUNT, region $REGION${IDENT:+, identifier $IDENT}"

# ---------------------------------------------------------------------------
section "Root stack and deployment id"
PREFIX="amplify-grow2-${IDENT:+${IDENT}-}"
ROOTS=$(aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query "StackSummaries[?starts_with(StackName, \`${PREFIX}\`) && !contains(StackName, \`-data\`) && !contains(StackName, \`-function\`) && !contains(StackName, \`-auth\`) && !contains(StackName, \`AgentCore\`) && !contains(StackName, \`BedrockPrompts\`)].StackName" \
  --output text | tr '\t' '\n' | grep -v '^$' || true)
N=$(printf '%s\n' "$ROOTS" | grep -c . || true)
if [ "$N" -eq 0 ]; then fail "no root stack matching ${PREFIX}* (is GROW2 deployed here?)"; echo; exit 1; fi
if [ "$N" -gt 1 ]; then fail "$N root stacks match ${PREFIX}* — pass --identifier:"; printf '%s\n' "$ROOTS" | sed 's/^/        /'; echo; exit 1; fi
ROOT="$ROOTS"; DEPLOYMENT_ID="${ROOT##*-}"
pass "root stack $ROOT (deployment id: $DEPLOYMENT_ID)"
ALL_ROOTS=$(aws cloudformation list-stacks --region "$REGION" --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?starts_with(StackName, `amplify-grant_assistant-`) || starts_with(StackName, `amplify-grow2-`)].StackName' --output text | tr '\t' '\n' | grep -vcE "(-data|-function|-auth|AgentCore|BedrockPrompts|^$)" || true)
echo "        ($ALL_ROOTS GROW2 root stack(s) in this region)"

# ---------------------------------------------------------------------------
section "CloudFormation exports carry the deployment id"
EXPECTED_EXPORTS="GuardrailId GuardrailVersion WafWebAclArn AgentCore-UsGrantsV2AgentArn AgentCore-EuGrantsV2AgentArn AgentCore-ProposalGenerationAgentArn GrantRecordTableName EuGrantRecordTableName UserProfileTableName AgentConfigTableName ProposalTableName ProposalsBucketName DocumentBucketName GraphQLApiId KnowledgeBaseId DeploymentRegion EuGrantsCacheBucketName AgentDiscoveryStepFunctionArn"
EXPORTS_JSON=$(aws cloudformation list-exports --region "$REGION" --output json)
export_value() { echo "$EXPORTS_JSON" | python3 -c "import json,sys; e=[x for x in json.load(sys.stdin)['Exports'] if x['Name']=='$1']; print(e[0]['Value'] if e else '')"; }
for e in $EXPECTED_EXPORTS; do
  name="GROW2-${DEPLOYMENT_ID}-${e}"
  v=$(export_value "$name")
  [ -n "$v" ] && pass "export $name" || fail "export $name missing"
done
LEGACY=$(echo "$EXPORTS_JSON" | python3 -c "import json,sys; print(' '.join(x['Name'] for x in json.load(sys.stdin)['Exports'] if x['Name'] in ('GROW2-GuardrailId','GraphQLApiId','KnowledgeBaseId','AgentCore-UsGrantsV2AgentArn')))")
[ -z "$LEGACY" ] && pass "no un-suffixed legacy exports" || warn "legacy un-suffixed exports still present (older deployment in region?): $LEGACY"

# ---------------------------------------------------------------------------
section "AgentCore runtimes"
RUNTIMES=$(aws bedrock-agentcore list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?ends_with(agentRuntimeName, \`_${DEPLOYMENT_ID}\`)].[agentRuntimeName,status]" --output text || true)
for a in grants_search_agent_v2 eu_grants_search_agent_v2 pdf_converter_agent proposal_evaluator_agent proposal_generation_agent; do
  line=$(printf '%s\n' "$RUNTIMES" | grep "^${a}_${DEPLOYMENT_ID}" || true)
  if [ -z "$line" ]; then fail "runtime ${a}_${DEPLOYMENT_ID} not found"
  elif echo "$line" | grep -q READY; then pass "runtime ${a}_${DEPLOYMENT_ID} READY"
  else warn "runtime ${a}_${DEPLOYMENT_ID} status: $(echo "$line" | awk '{print $2}')"; fi
done
# the Lambdas resolve agent ARNs through the suffixed exports — check they agree
for pair in "AgentCore-UsGrantsV2AgentArn:grants_search_agent_v2" "AgentCore-EuGrantsV2AgentArn:eu_grants_search_agent_v2" "AgentCore-ProposalGenerationAgentArn:proposal_generation_agent"; do
  exp="${pair%%:*}"; rt="${pair##*:}"
  arn=$(export_value "GROW2-${DEPLOYMENT_ID}-${exp}")
  case "$arn" in *"/${rt}_${DEPLOYMENT_ID}-"*) pass "export $exp → runtime ${rt}_${DEPLOYMENT_ID}" ;; *) fail "export $exp points at '$arn', expected runtime ${rt}_${DEPLOYMENT_ID}" ;; esac
done

# ---------------------------------------------------------------------------
section "Guardrail, WAF, OpenSearch, KB bucket"
GID=$(export_value "GROW2-${DEPLOYMENT_ID}-GuardrailId")
GNAME=$(aws bedrock get-guardrail --guardrail-identifier "$GID" --region "$REGION" --query name --output text 2>/dev/null || echo "")
[ "$GNAME" = "GROW2-PromptInjection-Guardrail-${DEPLOYMENT_ID}" ] && pass "guardrail $GNAME" || fail "guardrail name '$GNAME' (expected GROW2-PromptInjection-Guardrail-${DEPLOYMENT_ID})"
WARN_ARN=$(export_value "GROW2-${DEPLOYMENT_ID}-WafWebAclArn")
case "$WARN_ARN" in *"/GROW2-GraphQL-RateLimit-${DEPLOYMENT_ID}/"*) pass "WAF WebACL GROW2-GraphQL-RateLimit-${DEPLOYMENT_ID}" ;; *) fail "WAF ARN '$WARN_ARN' lacks deployment id" ;; esac
API_ID=$(export_value "GROW2-${DEPLOYMENT_ID}-GraphQLApiId")
WAF_ASSOC=$(aws wafv2 get-web-acl-for-resource --region "$REGION" --resource-arn "arn:aws:appsync:${REGION}:${ACCOUNT}:apis/${API_ID}" --query 'WebACL.Name' --output text 2>/dev/null || echo "")
[ "$WAF_ASSOC" = "GROW2-GraphQL-RateLimit-${DEPLOYMENT_ID}" ] && pass "WAF attached to AppSync API $API_ID" || fail "WAF not attached to AppSync API (got '$WAF_ASSOC')"
COLL=$(aws opensearchserverless list-collections --region "$REGION" --query "collectionSummaries[?name==\`kb-${DEPLOYMENT_ID}\`].status" --output text 2>/dev/null || echo "")
[ "$COLL" = "ACTIVE" ] && pass "OpenSearch collection kb-${DEPLOYMENT_ID} ACTIVE" || fail "OpenSearch collection kb-${DEPLOYMENT_ID}: '${COLL:-missing}'"
DOCB=$(export_value "GROW2-${DEPLOYMENT_ID}-DocumentBucketName")
[ "$DOCB" = "kb-docs-${ACCOUNT}-${REGION}-${DEPLOYMENT_ID}" ] && pass "KB document bucket $DOCB" || fail "KB document bucket '$DOCB' (expected kb-docs-${ACCOUNT}-${REGION}-${DEPLOYMENT_ID})"
KBID=$(export_value "GROW2-${DEPLOYMENT_ID}-KnowledgeBaseId")
KBS=$(aws bedrock-agent get-knowledge-base --knowledge-base-id "$KBID" --region "$REGION" --query 'knowledgeBase.status' --output text 2>/dev/null || echo "")
[ "$KBS" = "ACTIVE" ] && pass "Knowledge Base $KBID ACTIVE" || fail "Knowledge Base $KBID status '$KBS'"

# ---------------------------------------------------------------------------
section "Seeder, Amplify Hosting, UI"
SEEDER=$(echo "$EXPORTS_JSON" | python3 -c "import json,sys; print(' '.join(x['Value'] for x in json.load(sys.stdin)['Exports'] if x['Name'].endswith('-SeederProjectName') and '$ROOT' in x['ExportingStackId']))")
case "$SEEDER" in *"-${DEPLOYMENT_ID}") pass "seeder project $SEEDER carries deployment id" ;; "") fail "no SeederProjectName export for $ROOT" ;; *) fail "seeder project '$SEEDER' lacks deployment id" ;; esac
if [ -n "$SEEDER" ]; then
  LAST=$(aws codebuild list-builds-for-project --project-name "$SEEDER" --region "$REGION" --sort-order DESCENDING --query 'ids[0]' --output text 2>/dev/null || echo "")
  ST=$(aws codebuild batch-get-builds --ids "$LAST" --region "$REGION" --query 'builds[0].buildStatus' --output text 2>/dev/null || echo "NONE")
  [ "$ST" = "SUCCEEDED" ] && pass "last seeder build SUCCEEDED" || warn "last seeder build: $ST"
fi
APP=$(aws amplify list-apps --region "$REGION" --query "apps[?name==\`${ROOT}-grow2-app\`].[appId,defaultDomain]" --output text 2>/dev/null || true)
if [ -n "$APP" ]; then
  APP_ID=$(echo "$APP" | awk '{print $1}'); DOMAIN=$(echo "$APP" | awk '{print $2}')
  pass "Amplify app ${ROOT}-grow2-app ($APP_ID)"
  JOB=$(aws amplify list-jobs --app-id "$APP_ID" --branch-name main --region "$REGION" --max-results 1 --query 'jobSummaries[0].status' --output text 2>/dev/null || echo "")
  [ "$JOB" = "SUCCEED" ] && pass "latest UI deployment on main SUCCEED" || warn "latest UI deployment status: ${JOB:-none}"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://main.${DOMAIN}/" || echo "000")
  [ "$CODE" = "200" ] && pass "UI reachable https://main.${DOMAIN}/" || warn "UI returned HTTP $CODE at https://main.${DOMAIN}/"
else
  fail "Amplify app ${ROOT}-grow2-app not found"
fi

# ---------------------------------------------------------------------------
if [ -n "$EXPECT_ENV" ]; then
  section "Environment hardening (expect $EXPECT_ENV)"
  for t in GrantRecordTableName UserProfileTableName ProposalTableName; do
    tn=$(export_value "GROW2-${DEPLOYMENT_ID}-${t}")
    D=$(aws dynamodb describe-table --table-name "$tn" --region "$REGION" --query 'Table.DeletionProtectionEnabled' --output text 2>/dev/null || echo "")
    P=$(aws dynamodb describe-continuous-backups --table-name "$tn" --region "$REGION" --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' --output text 2>/dev/null || echo "")
    [ "$P" = "ENABLED" ] && pass "$tn PITR enabled" || fail "$tn PITR $P"
    if [ "$EXPECT_ENV" = "prod" ]; then [ "$D" = "True" ] && pass "$tn deletion protection on" || fail "$tn deletion protection $D"
    else [ "$D" = "False" ] && pass "$tn deletion protection off (dev)" || warn "$tn deletion protection $D in dev"; fi
  done
  UP=$(aws cognito-idp list-users --user-pool-id "$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" --query "UserPools[?contains(Name, \`${DEPLOYMENT_ID}\`)].Id | [0]" --output text)" --region "$REGION" --filter 'email = "test_user@example.com"' --query 'Users[0].Username' --output text 2>/dev/null || echo "")
  if [ "$EXPECT_ENV" = "prod" ]; then [ -z "$UP" ] || [ "$UP" = "None" ] && pass "no demo user in prod" || fail "demo user test_user@example.com exists in prod"
  else [ -n "$UP" ] && [ "$UP" != "None" ] && pass "demo user seeded (dev)" || warn "demo user not found (seeder not run yet?)"; fi
fi

# ---------------------------------------------------------------------------
if [ "$SMOKE" -eq 1 ]; then
  section "Smoke test — US grants search end to end"
  FN=$(export_value "GROW2-${DEPLOYMENT_ID}-GrantsSearchV2FunctionName")
  SID="validate-$(date +%s)"
  PAYLOAD=$(printf '{"fieldName":"startGrantSearchV2","arguments":{"input":{"query":"thermal transport","sessionId":"%s"}},"identity":{"claims":{"sub":"validate-user","email":"validate@example.com"}}}' "$SID")
  OUT=$(mktemp)
  if aws lambda invoke --function-name "$FN" --region "$REGION" --cli-binary-format raw-in-base64-out --payload "$PAYLOAD" "$OUT" >/dev/null 2>&1 && ! grep -q '"errorMessage"' "$OUT"; then
    pass "Lambda $FN accepted the search (session $SID)"
    LG="/aws/bedrock-agentcore/runtimes/$(aws bedrock-agentcore list-agent-runtimes --region "$REGION" --query "agentRuntimes[?agentRuntimeName==\`grants_search_agent_v2_${DEPLOYMENT_ID}\`].agentRuntimeId | [0]" --output text)-DEFAULT"
    echo "        waiting up to 120s for the agent to log results in $LG ..."
    HIT=""
    for i in $(seq 1 24); do
      HIT=$(aws logs filter-log-events --log-group-name "$LG" --region "$REGION" --start-time $(( ($(date +%s) - 300) * 1000 )) --filter-pattern "\"$SID\"" --query 'events[?contains(message, `Converted`) || contains(message, `grants to UI format`)].message | [0]' --output text 2>/dev/null || true)
      [ -n "$HIT" ] && [ "$HIT" != "None" ] && break
      sleep 5
    done
    [ -n "$HIT" ] && [ "$HIT" != "None" ] && pass "agent produced results: ${HIT:0:100}" || warn "no agent result logged for $SID within 120s — check $LG"
  else
    fail "Lambda $FN invocation failed: $(head -c 300 "$OUT")"
  fi
  rm -f "$OUT"
fi

echo
echo "== Summary: $PASS passed, $FAIL failed, $WARN warnings"
[ "$FAIL" -eq 0 ]
