#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ACTION=${1:?Specify command}; shift
SOCKET=${GITEA_PODMAN_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}
[[ $(id -u) != 0 ]] || { echo 'Run as a regular user, without sudo.' >&2; exit 1; }
[[ -S "$SOCKET" ]] || { echo "Rootless Podman socket missing: $SOCKET. Run systemctl --user enable --now podman.socket." >&2; exit 1; }
mkdir -p "$ROOT/private" "$ROOT/state"
chmod 700 "$ROOT/private" "$ROOT/state"
exec 9>"$ROOT/state/operation.lock"
flock -n 9 || { echo 'Another Gitea operation is running.' >&2; exit 1; }
# Keep the lock in this shell only. Podman's background processes must not
# inherit it and keep it held after this script has exited.
podman() {
  command podman "$@" 9>&-
}
CIDFILE="$ROOT/state/manager.cid"
rm -f "$CIDFILE"
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -f "$CIDFILE" ]]; then
    podman rm -f "$(cat "$CIDFILE")" >/dev/null 2>&1 || true
    rm -f "$CIDFILE"
  fi
  if [[ -f "$ROOT/state/restart-container" ]]; then
    if podman start "$(cat "$ROOT/state/restart-container")" >/dev/null; then
      rm -f "$ROOT/state/restart-container"
    else
      echo 'Automatic restart failed; inspect Gitea and run start.sh.' >&2
      status=1
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
IMAGE=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:3}
if ! podman image exists "$IMAGE"; then
  podman build -t "$IMAGE" -f "$ROOT/Containerfile" "$ROOT"
fi
RUN_ARGS=(--rm --cidfile "$CIDFILE" --userns=keep-id --network host --security-opt label=disable)
if [[ "$ACTION" == configure-rclone ]]; then RUN_ARGS+=(-it); fi
podman run "${RUN_ARGS[@]}" \
  -v "$SOCKET:/run/podman/podman.sock" \
  -v "$ROOT:$ROOT" -w "$ROOT" \
  -e CONTAINER_HOST=unix:///run/podman/podman.sock \
  -e HOME=/tmp/gitea-manager-home -e XDG_RUNTIME_DIR=/tmp/gitea-manager-runtime \
  -e GITEA_MANAGER_IMAGE="$IMAGE" \
  -e GITEA_PODMAN_SOCKET="$SOCKET" \
  -e GITEA_ROOT="$ROOT" "$IMAGE" "$ACTION" "$@"
