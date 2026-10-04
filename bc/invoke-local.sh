#!/bin/bash
# invoke-local.sh - Run a GROW2 AgentCore agent on your machine and invoke it.
#
# Every agent is a BedrockAgentCoreApp; `python agent.py` serves the same HTTP
# contract the managed runtime uses (POST /invocations, GET /ping) on port 8080.
# This script starts the agent, waits for /ping, POSTs a payload, prints the
# response, and stops the agent — a seconds-long loop instead of a 45-minute deploy.
#
# Usage:
#   ./bc/invoke-local.sh <agent-dir> [payload.json]
#
#   <agent-dir>    one of: grants-search-agent-v2, eu-grants-search-agent-v2,
#                  pdf-converter-agent, proposal-evaluator-agent, proposal-generation-agent
#   [payload.json] defaults to bc/<agent-dir>/sample-payload.json
#
# Environment:
#   Copy bc/common/local-env.example to bc/common/local-env (git-ignored), fill in
#   the values from your deployed stack, and this script sources it. Agents call
#   real AWS services (Bedrock, DynamoDB, S3, AppSync), so you need AWS credentials
#   with access to that stack in your shell.
#
# Options:
#   --keep     leave the agent running after the invocation (hit it with curl)
#   --port N   use a port other than 8080

set -euo pipefail

BC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT=""
PAYLOAD=""
KEEP=0
PORT=8080

while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1 ;;
    --port) PORT="$2"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) if [ -z "$AGENT" ]; then AGENT="$1"; else PAYLOAD="$1"; fi ;;
  esac
  shift
done

if [ -z "$AGENT" ] || [ ! -f "$BC_DIR/$AGENT/agent.py" ]; then
  echo "Usage: $0 <agent-dir> [payload.json]" >&2
  echo "Agents:" >&2
  for d in "$BC_DIR"/*/; do [ -f "$d/agent.py" ] && echo "  $(basename "$d")" >&2; done
  exit 1
fi

PAYLOAD="${PAYLOAD:-$BC_DIR/$AGENT/sample-payload.json}"
if [ ! -f "$PAYLOAD" ]; then
  echo "Payload file not found: $PAYLOAD" >&2
  exit 1
fi

# Local environment (endpoints, table names, bucket names from the deployed stack)
if [ -f "$BC_DIR/common/local-env" ]; then
  set -a; . "$BC_DIR/common/local-env"; set +a
else
  echo "Note: bc/common/local-env not found — using whatever is already in your environment." >&2
  echo "      Copy bc/common/local-env.example to bc/common/local-env and fill it in." >&2
fi
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_DEFAULT_REGION="$AWS_REGION"

# Shared package (bc/common) + domain config pack. In the image these sit next to
# agent.py; locally we put bc/ on the path and point at config/domains/<domain>.
export PYTHONPATH="$BC_DIR${PYTHONPATH:+:$PYTHONPATH}"
export GROW2_DOMAIN_DIR="${GROW2_DOMAIN_DIR:-$BC_DIR/../config/domains/${GROW2_DOMAIN:-grants}}"

# Per-agent virtualenv so requirements don't collide across agents
VENV="$BC_DIR/$AGENT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv at $VENV ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$BC_DIR/$AGENT/requirements.txt"
fi

LOG="$(mktemp -t grow2-agent-XXXXXX.log)"
echo "Starting $AGENT on :$PORT (log: $LOG) ..."
( cd "$BC_DIR/$AGENT" && PORT="$PORT" "$VENV/bin/python" agent.py >"$LOG" 2>&1 ) &
AGENT_PID=$!

cleanup() {
  if [ "$KEEP" -eq 0 ]; then
    kill "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi
}
# Also on SIGPIPE: if stdout is closed early (e.g. piped into `head`) the agent must still be stopped.
trap cleanup EXIT INT TERM PIPE

for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$AGENT_PID" 2>/dev/null; then
    echo "Agent exited before it was ready. Log:" >&2; cat "$LOG" >&2; exit 1
  fi
  sleep 0.5
done

echo "Invoking with $PAYLOAD"
echo "----------------------------------------------------------------------"
curl -sS -X POST "http://127.0.0.1:$PORT/invocations" \
  -H "Content-Type: application/json" \
  --data @"$PAYLOAD" | (python3 -m json.tool 2>/dev/null || cat)
echo
echo "----------------------------------------------------------------------"
echo "Agent log: $LOG"
if [ "$KEEP" -eq 1 ]; then
  echo "Agent still running (pid $AGENT_PID) on http://127.0.0.1:$PORT — kill $AGENT_PID to stop."
fi
