#!/usr/bin/env bash
# Use installed Python when available; otherwise use the management container.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
"$ROOT/tests/test-compose-env.sh"
if command -v python3 >/dev/null && python3 -c 'import sys, yaml; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
  exec python3 -B -m unittest discover -s "$ROOT/tests" -v
fi
[[ $(id -u) != 0 ]] || { echo 'Run without sudo.' >&2; exit 1; }
IMAGE=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:5}
if ! podman image exists "$IMAGE"; then
  podman build -t "$IMAGE" -f "$ROOT/Containerfile" "$ROOT"
fi
exec podman run --rm --userns=keep-id --security-opt label=disable \
  -v "$ROOT:/project:ro" -w /project -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python3 "$IMAGE" -B -m unittest discover -s tests -v
