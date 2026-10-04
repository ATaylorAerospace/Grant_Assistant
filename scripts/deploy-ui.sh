#!/bin/bash
# deploy-ui.sh - Build the React app locally and publish it to Amplify Hosting.
#
# A UI-only change does not need the 35-55 minute CodeBuild deploy or the seeder:
# this builds with Vite (seconds), zips build/, and pushes it through Amplify's
# manual-deployment API. Typical wall time: ~2 minutes.
#
# Usage:
#   ./scripts/deploy-ui.sh <region> [amplify-app-id]
#
# Prerequisites:
#   - GROW2 deployed in <region> (the Amplify app is created by the stack)
#   - react-aws/src/amplify_outputs.json present (run ./scripts/fetch-outputs.sh <region>)
#   - AWS credentials with amplify:CreateDeployment / StartDeployment on the app
#
# The app id is discovered from `aws amplify list-apps` (name contains "grow2")
# unless passed explicitly. Deploys go to the `main` branch, the same one the
# seeder publishes to.

set -euo pipefail

REGION="${1:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
APP_ID="${2:-}"
BRANCH="${GROW2_UI_BRANCH:-main}"

if [ -z "$REGION" ]; then
  echo "Usage: $0 <region> [amplify-app-id]" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UI_DIR="$REPO_ROOT/react-aws"

if [ ! -f "$UI_DIR/src/amplify_outputs.json" ]; then
  echo "ERROR: $UI_DIR/src/amplify_outputs.json not found." >&2
  echo "       Run ./scripts/fetch-outputs.sh $REGION first." >&2
  exit 1
fi

if [ -z "$APP_ID" ]; then
  APP_ID=$(aws amplify list-apps --region "$REGION" \
    --query 'apps[?contains(name, `grow2`)].appId' --output text 2>/dev/null | awk '{print $1}')
  if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ]; then
    echo "ERROR: no Amplify app with 'grow2' in its name in $REGION; pass the app id explicitly." >&2
    exit 1
  fi
fi
echo "Amplify app: $APP_ID  branch: $BRANCH  region: $REGION"

echo "Building React app..."
( cd "$UI_DIR" && { [ -d node_modules ] || npm ci --no-audit --no-fund --legacy-peer-deps; } && npm run build )

ZIP="$(mktemp -t grow2-ui-XXXXXX).zip"
( cd "$UI_DIR/build" && zip -qr "$ZIP" . )
echo "Bundle: $ZIP ($(du -h "$ZIP" | cut -f1))"

# Amplify manual deployment: create-deployment returns a pre-signed upload URL,
# the bundle is PUT there, then start-deployment publishes it.
if ! aws amplify get-branch --app-id "$APP_ID" --branch-name "$BRANCH" --region "$REGION" >/dev/null 2>&1; then
  echo "Creating branch $BRANCH..."
  aws amplify create-branch --app-id "$APP_ID" --branch-name "$BRANCH" --region "$REGION" >/dev/null
fi

DEPLOYMENT=$(aws amplify create-deployment --app-id "$APP_ID" --branch-name "$BRANCH" --region "$REGION")
JOB_ID=$(echo "$DEPLOYMENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["jobId"])')
UPLOAD_URL=$(echo "$DEPLOYMENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["zipUploadUrl"])')

echo "Uploading bundle (job $JOB_ID)..."
curl -sSf -T "$ZIP" "$UPLOAD_URL" >/dev/null

aws amplify start-deployment --app-id "$APP_ID" --branch-name "$BRANCH" --job-id "$JOB_ID" --region "$REGION" >/dev/null
echo "Deployment started; waiting..."

for i in $(seq 1 60); do
  STATUS=$(aws amplify get-job --app-id "$APP_ID" --branch-name "$BRANCH" --job-id "$JOB_ID" --region "$REGION" \
    --query 'job.summary.status' --output text)
  case "$STATUS" in
    SUCCEED) break ;;
    FAILED|CANCELLED) echo "Deployment $STATUS" >&2; exit 1 ;;
  esac
  sleep 5
done

DOMAIN=$(aws amplify get-app --app-id "$APP_ID" --region "$REGION" --query 'app.defaultDomain' --output text)
echo "Deployed: https://$BRANCH.$DOMAIN"
rm -f "$ZIP"
