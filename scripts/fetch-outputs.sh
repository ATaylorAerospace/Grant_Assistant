#!/bin/bash
# fetch-outputs.sh - Pull amplify_outputs.json from a deployed GROW2 stack so the
# React app (and local agent harnesses) can run locally against that backend.
#
# Usage:
#   ./scripts/fetch-outputs.sh <region>
#
# Writes:
#   react-aws/src/amplify_outputs.json   (git-ignored)
#
# The deployer CodeBuild uploads amplify_outputs.json to the stack's deployment
# assets bucket (see installation/deploy-via-codebuild.sh post_build). This script
# finds that bucket via the CloudFormation export that ends in
# "-DeploymentAssetsBucket" and copies the file down.

set -euo pipefail

REGION="${1:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [ -z "$REGION" ]; then
  echo "Usage: $0 <region>   (or set AWS_REGION)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/react-aws/src/amplify_outputs.json"

# One export per deployment. With several deployments in the region, pick by
# GROW2_IDENTIFIER (the stack is amplify-grow2-<identifier>-sandbox-<hash>).
PREFIX="amplify-grow2-${GROW2_IDENTIFIER:+${GROW2_IDENTIFIER}-}"
MATCHES=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?ends_with(Name,\`-DeploymentAssetsBucket\`) && contains(ExportingStackId, \`stack/${PREFIX}\`)].[Name,Value]" \
  --output text 2>/dev/null || true)
COUNT=$(printf '%s\n' "$MATCHES" | grep -c . || true)

if [ "$COUNT" -eq 0 ]; then
  echo "ERROR: no CloudFormation export ending in -DeploymentAssetsBucket for '${PREFIX}*' in $REGION." >&2
  echo "       Is GROW2 deployed in this region? (see README → Quick Start Deployment)" >&2
  exit 1
elif [ "$COUNT" -gt 1 ]; then
  echo "ERROR: $COUNT GROW2 deployments in $REGION — set GROW2_IDENTIFIER to choose one:" >&2
  printf '%s\n' "$MATCHES" | sed 's/^/       /' >&2
  exit 1
fi
BUCKET=$(printf '%s\n' "$MATCHES" | awk '{print $2}')

echo "Deployment assets bucket: $BUCKET"
aws s3 cp "s3://$BUCKET/codebuild-deploy/amplify_outputs.json" "$DEST" --region "$REGION"

SIZE=$(wc -c < "$DEST" | tr -d ' ')
if [ "$SIZE" -le 100 ]; then
  echo "ERROR: downloaded file is only $SIZE bytes — the last deploy probably failed." >&2
  exit 1
fi

echo "Wrote $DEST ($SIZE bytes)"
echo
echo "Next:"
echo "  cd react-aws && npm install && npm start      # UI on http://localhost:3000 against the deployed backend"
echo "  ./bc/invoke-local.sh <agent>                   # run an AgentCore agent locally (see README → Local Development)"
