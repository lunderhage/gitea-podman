#!/usr/bin/env bash
# Source this file. Settings parsing uses installed Python/YAML or the container.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo 'Use: source ./scripts/compose-env.sh' >&2
  exit 1
fi
_gitea_compose_env() {
  local project_dir values
  project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || return 1
  if [[ -f "$project_dir/settings.yaml" || -f "$project_dir/settings.yml" || -f "$project_dir/settings.json" ]] &&
     [[ "${GITEA_MANAGER_IMAGE:-}" =~ ^localhost/gitea-podman-manager:[1234]$ ]]; then
    echo 'An older GITEA_MANAGER_IMAGE is still exported. Run:' >&2
    echo 'export GITEA_MANAGER_IMAGE=localhost/gitea-podman-manager:5' >&2
    return 1
  fi
  if command -v python3 >/dev/null && python3 -c 'import yaml' >/dev/null 2>&1; then
    values=$(python3 -B "$project_dir/scripts/settings_io.py" exports "$project_dir") || return 1
  else
    local helper=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:5}
    command -v podman >/dev/null || { echo 'Need installed Python/PyYAML or the rootless management image.' >&2; return 1; }
    [[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]] || return 1
    podman image exists "$helper" || { echo "Build the management image first: $helper" >&2; return 1; }
    values=$(podman run --rm --network none --userns=keep-id --security-opt label=disable \
      -v "$project_dir:/project:ro" --entrypoint python3 "$helper" \
      -B /project/scripts/settings_io.py exports /project "$project_dir") || return 1
  fi
  local -a parsed
  mapfile -t parsed <<< "$values"
  [[ ${#parsed[@]} == 40 ]] || { echo 'Invalid Compose settings; no variables changed.' >&2; return 1; }
  local index key
  local -A seen
  for ((index=0; index<${#parsed[@]}; index+=2)); do
    key=${parsed[index]}
    case "$key" in
      GITEA_IMAGE|DATA_VOLUME|CONFIG_VOLUME|COMPOSE_PROJECT_NAME|HTTP_PORT|SSH_PORT|BACKUP_VOLUME|SSH_DOMAIN|SSH_LISTEN_PORT|GITEA_PROTOCOL|GITEA_ROOT_URL|GITEA_DOMAIN|PUBLIC_HOSTNAME|HTTP_REDIRECT_PORT|ACME_EMAIL|ACME_URL|ACME_DIRECTORY|TLS_ENABLED|COMPOSE_FILE|GITEA_PROJECT_DIR) ;;
      *) echo 'Invalid Compose export key; no variables changed.' >&2; return 1 ;;
    esac
    [[ -z ${seen[$key]+present} ]] || { echo 'Duplicate Compose export key; no variables changed.' >&2; return 1; }
    seen[$key]=1
  done
  for ((index=0; index<${#parsed[@]}; index+=2)); do
    export "${parsed[index]}=${parsed[index+1]}"
  done
  export GITEA_PODMAN_SOCKET="${GITEA_PODMAN_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}"
  export GITEA_MANAGER_IMAGE="${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:5}"
  printf 'Compose project: %s (web %s, SSH %s; TLS %s)\n' "$COMPOSE_PROJECT_NAME" "$HTTP_PORT" "$SSH_PORT" "$TLS_ENABLED"
}
if _gitea_compose_env; then
  unset -f _gitea_compose_env
else
  unset -f _gitea_compose_env
  return 1
fi
