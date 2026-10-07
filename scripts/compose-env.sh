#!/usr/bin/env bash
# Source this file; it exports configuration without starting/stopping containers.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo 'Use: source ./scripts/compose-env.sh' >&2
  exit 1
fi

_gitea_compose_env() {
  local project_dir settings_file values image data config project http ssh backup
  project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || return 1
  settings_file="$project_dir/settings.json"
  [[ -r "$settings_file" ]] || { echo "Cannot read $settings_file" >&2; return 1; }
  command -v jq >/dev/null || {
    echo 'This environment script needs jq, which is already available on primary2.' >&2
    echo 'No variables were changed.' >&2
    return 1
  }
  values=$(jq -er '
    if (.project | type) != "string" or (.project | test("^[a-zA-Z0-9_-]+$") | not)
      or (.image | type) != "string" or (.image | test("^docker\\.gitea\\.com/gitea(:[0-9]+\\.[0-9]+\\.[0-9]+-rootless|@sha256:[a-f0-9]{64})$") | not)
      or (.dataVolume | type) != "string" or (.dataVolume | test("^[a-zA-Z0-9][a-zA-Z0-9_.-]*$") | not)
      or (.configVolume | type) != "string" or (.configVolume | test("^[a-zA-Z0-9][a-zA-Z0-9_.-]*$") | not)
      or .dataVolume == .configVolume
      or (.httpPort | type) != "number" or .httpPort != (.httpPort | floor) or .httpPort < 1024 or .httpPort > 65535
      or (.sshPort | type) != "number" or .sshPort != (.sshPort | floor) or .sshPort < 1024 or .sshPort > 65535
      or ((.backupVolume // (.project + "-backups")) | type) != "string"
      or ((.backupVolume // (.project + "-backups")) | test("^[a-zA-Z0-9][a-zA-Z0-9_.-]*$") | not)
    then error("Invalid Compose settings")
    else [.image, .dataVolume, .configVolume, .project, (.httpPort | tostring), (.sshPort | tostring),
          (.backupVolume // (.project + "-backups"))] | .[] end
  ' "$settings_file") || { echo 'No variables were changed.' >&2; return 1; }
  local -a parsed
  mapfile -t parsed <<< "$values"
  [[ ${#parsed[@]} == 7 ]] || { echo 'Invalid Compose settings' >&2; return 1; }
  image=${parsed[0]}; data=${parsed[1]}; config=${parsed[2]}; project=${parsed[3]}
  http=${parsed[4]}; ssh=${parsed[5]}; backup=${parsed[6]}
  export GITEA_IMAGE="$image" DATA_VOLUME="$data" CONFIG_VOLUME="$config"
  export HTTP_PORT="$http" SSH_PORT="$ssh" BACKUP_VOLUME="$backup"
  export COMPOSE_PROJECT_NAME="$project" COMPOSE_FILE="$project_dir/compose.yaml"
  export GITEA_PROJECT_DIR="$project_dir"
  export GITEA_PODMAN_SOCKET="${GITEA_PODMAN_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}"
  export GITEA_MANAGER_IMAGE="${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:3}"
  printf 'Compose project: %s (HTTP %s, SSH %s)\n' "$project" "$http" "$ssh"
}

if _gitea_compose_env; then
  unset -f _gitea_compose_env
else
  unset -f _gitea_compose_env
  return 1
fi
