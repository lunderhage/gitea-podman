#!/usr/bin/env bash
# Check the exact Compose target shell program with the installed host /bin/sh.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SCRATCH=$(mktemp -d /tmp/gitea-lifecycle-target.XXXXXX)
# Compose turns $$ into $ before passing the command to the container.
sed -n '/^        trap.*Target stopped cleanly/,/^    networks:/p' "$HERE/compose.yaml" |
  sed '$d; s/^        //; s/\$\$/\$/g' > "$SCRATCH/target.sh"
/bin/sh -n "$SCRATCH/target.sh"
/bin/sh "$SCRATCH/target.sh" > "$SCRATCH/output.log" 2>&1 &
TEST_PID=$!
cleanup() { kill -TERM "$TEST_PID" 2>/dev/null || true; }
trap cleanup EXIT
for attempt in {1..30}; do
  if grep -q 'Target ready' "$SCRATCH/output.log"; then break; fi
  sleep 0.1
 done
grep -q 'Target ready' "$SCRATCH/output.log"
kill -TERM "$TEST_PID"
wait "$TEST_PID"
trap - EXIT
grep -q 'Target stopped cleanly' "$SCRATCH/output.log"
echo 'PASS: Compose target command exits with status 0 on SIGTERM'
echo "Test output: $SCRATCH/output.log"
