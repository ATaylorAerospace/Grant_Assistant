#!/bin/bash
# test-lambdas.sh - Run the Python unit tests for every Lambda function that has them.
#
# Each function directory is self-contained (handler.py + test_handler.py), and
# several share the test file name, so each suite runs in its own pytest process
# from inside its directory. Handlers read required configuration from the
# environment at import time, so safe placeholder values are exported here.
#
# Usage:
#   ./scripts/test-lambdas.sh            # run all suites
#   ./scripts/test-lambdas.sh kb-search  # run one function's suite
#
# Exit code is non-zero if any suite fails.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FUNCTIONS_DIR="$REPO_ROOT/amplify/functions"

# Placeholder AWS config so boto3 clients can be constructed without a real account.
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_REGION="${AWS_REGION:-$AWS_DEFAULT_REGION}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-testing}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-testing}"

# Required by handlers at import time (values are never used against AWS in tests).
export DOCUMENT_BUCKET="${DOCUMENT_BUCKET:-test-document-bucket}"
export DOCUMENT_TABLE="${DOCUMENT_TABLE:-test-document-table}"
export KNOWLEDGE_BASE_ID="${KNOWLEDGE_BASE_ID:-test-kb-id}"
export DATA_SOURCE_ID="${DATA_SOURCE_ID:-test-ds-id}"
export PROPOSALS_TABLE="${PROPOSALS_TABLE:-test-proposals-table}"
export PROPOSALS_BUCKET="${PROPOSALS_BUCKET:-test-proposals-bucket}"
export REGION="${REGION:-$AWS_DEFAULT_REGION}"

if [ $# -gt 0 ]; then
  DIRS=()
  for name in "$@"; do DIRS+=("$FUNCTIONS_DIR/$name"); done
else
  DIRS=()
  for d in "$FUNCTIONS_DIR"/*/; do
    if ls "$d"/test_*.py >/dev/null 2>&1; then DIRS+=("${d%/}"); fi
  done
fi

PASS=(); FAIL=()
for d in "${DIRS[@]}"; do
  name="$(basename "$d")"
  echo "=================================================================="
  echo "  $name"
  echo "=================================================================="
  if (cd "$d" && python3 -m pytest -q -p no:cacheprovider "${PYTEST_ARGS:-}"); then
    PASS+=("$name")
  else
    FAIL+=("$name")
  fi
  echo
done

echo "=================================================================="
echo "  Summary: ${#PASS[@]} suite(s) passed, ${#FAIL[@]} failed"
for n in "${PASS[@]}"; do echo "    PASS  $n"; done
for n in "${FAIL[@]}"; do echo "    FAIL  $n"; done
echo "=================================================================="
[ "${#FAIL[@]}" -eq 0 ]
