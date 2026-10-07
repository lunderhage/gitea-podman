#!/usr/bin/env bash
# Source this file. Settings parsing uses installed Python/YAML or the container.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo 'Use: source ./scripts/compose-env.sh' >&2
  exit 1
fi
_gitea_compose_env() {
  local project_dir values
  project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || return 1
  if command -v python3 >/dev/null && python3 -c 'import yaml' >/dev/null 2>&1; then
    values=$(python3 -B "$project_dir/scripts/settings_io.py" exports "$project_dir") || return 1
  else
    local helper=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:4}
    command -v podman >/dev/null || { echo 'Need installed Python/PyYAML or the rootless management image.' >&2; return 1; }
    [[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]] || return 1
    podman image exists "$helper" || { echo "Build the management image first: $helper" >&2; return 1; }
    values=$(podman run --rm --network none --userns=keep-id --security-opt label=disable \
      -v "$project_dir:/project:ro" --entrypoint python3 "$helper" \
      -B /project/scripts/settings_io.py exports /project) || return 1
  fi
  local -a parsed
  mapfile -t parsed <<< "$values"
  [[ ${#parsed[@]} == 9 ]] || { echo 'Invalid Compose settings; no variables changed.' >&2; return 1; }
  export GITEA_IMAGE="${parsed[0]}" DATA_VOLUME="${parsed[1]}" CONFIG_VOLUME="${parsed[2]}"
  export COMPOSE_PROJECT_NAME="${parsed[3]}" HTTP_PORT="${parsed[4]}" SSH_PORT="${parsed[5]}"
  export BACKUP_VOLUME="${parsed[6]}" SSH_DOMAIN="${parsed[7]}" SSH_LISTEN_PORT="${parsed[8]}"
  export COMPOSE_FILE="$project_dir/compose.yaml" GITEA_PROJECT_DIR="$project_dir"
  export GITEA_PODMAN_SOCKET="${GITEA_PODMAN_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}"
  export GITEA_MANAGER_IMAGE="${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:4}"
  printf 'Compose project: %s (HTTP %s, SSH %s)\n' "$COMPOSE_PROJECT_NAME" "$HTTP_PORT" "$SSH_PORT"
}
if _gitea_compose_env; then
  unset -f _gitea_compose_env
else
  unset -f _gitea_compose_env
  return 1
fi
