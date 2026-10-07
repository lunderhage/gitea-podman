#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if command -v python3 >/dev/null && python3 -c 'import yaml' >/dev/null 2>&1; then
  exec python3 -B "$ROOT/scripts/settings_io.py" migrate "$ROOT"
fi
[[ $(id -u) != 0 ]] || { echo 'Run without sudo.' >&2; exit 1; }
IMAGE=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:4}
if ! podman image exists "$IMAGE"; then
  podman build -t "$IMAGE" -f "$ROOT/Containerfile" "$ROOT"
fi
exec podman run --rm --network none --userns=keep-id --security-opt label=disable \
  -v "$ROOT:/project" --entrypoint python3 "$IMAGE" -B /project/scripts/settings_io.py migrate /project
