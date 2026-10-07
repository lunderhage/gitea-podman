#!/usr/bin/env bash
# Attach a manual-only sidecar to an adopted server; never recreate the server.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SERVER=${1:?Usage: attach-existing-backup.sh ORIGINAL_SERVER_NAME_OR_ID}
[[ $(id -u) != 0 ]] || { echo 'Run without sudo.' >&2; exit 1; }
[[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]] || { echo 'Rootless Podman required.' >&2; exit 1; }
source "$ROOT/scripts/compose-env.sh"
[[ -S "$GITEA_PODMAN_SOCKET" ]] || { echo 'Enable the rootless podman.socket first.' >&2; exit 1; }
mkdir -p "$ROOT/state" "$ROOT/private"
chmod 700 "$ROOT/state" "$ROOT/private"
exec 9>"$ROOT/state/operation.lock"
flock -n 9 || { echo 'Another Gitea operation is running.' >&2; exit 1; }
podman() { command podman "$@" 9>&-; }
[[ $(podman inspect "$SERVER" --format '{{.State.Running}}') == true ]] || { echo 'Original server is not running.' >&2; exit 1; }
[[ $(podman inspect "$SERVER" --format '{{index .Config.Labels "com.docker.compose.service"}}') == server ]] || { echo 'Not a Compose server service.' >&2; exit 1; }
[[ $(podman inspect "$SERVER" --format '{{index .Config.Labels "com.docker.compose.project"}}') == "$COMPOSE_PROJECT_NAME" ]] || { echo 'Settings do not match the original project. Adopt it in a separate checkout first.' >&2; exit 1; }
for pair in '/var/lib/gitea:data' '/etc/gitea:config'; do
  destination=${pair%:*}
  if [[ ${pair##*:} == data ]]; then expected=$DATA_VOLUME; else expected=$CONFIG_VOLUME; fi
  actual=$(podman inspect "$SERVER" --format "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{.Name}}{{end}}{{end}}")
  [[ "$actual" == "$expected" ]] || { echo "Settings do not match the server volume at $destination" >&2; exit 1; }
done
NETWORK_TEXT=$(podman inspect "$SERVER" --format '{{range $name, $value := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}')
mapfile -t NETWORKS <<< "$NETWORK_TEXT"
[[ ${#NETWORKS[@]} == 1 && -n ${NETWORKS[0]} ]] || { echo 'Expected one network on the original server; inspect its network configuration.' >&2; exit 1; }
NETWORK=${NETWORKS[0]}
NAME="${COMPOSE_PROJECT_NAME}-manual-backup"
[[ ! $(podman ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" --filter label=com.docker.compose.service=backup) ]] || { echo 'A backup sidecar already exists for the original project.' >&2; exit 1; }
if podman container exists "$NAME"; then echo "Container name already exists: $NAME" >&2; exit 1; fi
# Distinct staging volume, not a data/config volume and not the test staging.
BACKUP_VOLUME="${COMPOSE_PROJECT_NAME}-original-backups"
[[ "$BACKUP_VOLUME" != "$DATA_VOLUME" && "$BACKUP_VOLUME" != "$CONFIG_VOLUME" ]]
if podman volume exists "$BACKUP_VOLUME"; then
  [[ $(podman volume inspect "$BACKUP_VOLUME" --format '{{index .Labels "org.gitea-podman.original-backup"}}') == "$COMPOSE_PROJECT_NAME" ]] || { echo 'Existing staging volume is not owned by this backup procedure.' >&2; exit 1; }
else
  podman volume create --label "org.gitea-podman.original-backup=$COMPOSE_PROJECT_NAME" "$BACKUP_VOLUME"
fi
podman image exists "$GITEA_MANAGER_IMAGE" || { echo 'Build the updated management image first.' >&2; exit 1; }
podman run -d --name "$NAME" --restart always --network "$NETWORK" --security-opt label=disable \
  --label "com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
  --label com.docker.compose.service=backup \
  --label "org.gitea-podman.original-backup=$COMPOSE_PROJECT_NAME" \
  -v "$GITEA_PODMAN_SOCKET:/run/podman/podman.sock" \
  -v "$ROOT:$ROOT" -v "$BACKUP_VOLUME:/backup" \
  -e CONTAINER_HOST=unix:///run/podman/podman.sock \
  -e HOME=/tmp/gitea-manager-home -e XDG_RUNTIME_DIR=/tmp/gitea-manager-runtime \
  -e GITEA_ROOT="$ROOT" -e GITEA_MANAGER_IMAGE="$GITEA_MANAGER_IMAGE" \
  -e GITEA_BACKUP_PATH=/backup -e GITEA_BACKUP_VOLUME="$BACKUP_VOLUME" \
  --entrypoint python3 "$GITEA_MANAGER_IMAGE" /opt/gitea/backup_service.py --idle
sleep 2
[[ $(podman inspect "$NAME" --format '{{.State.Running}}') == true ]] || { podman logs "$NAME"; exit 1; }
echo "Attached $NAME to $NETWORK. Original server has not been stopped or recreated."
echo 'Run ./scripts/backup.sh when ready for the maintenance window.'
