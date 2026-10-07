#!/usr/bin/env bash
# Source this file; it exports configuration without starting/stopping containers.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo 'Use: source ./scripts/compose-env.sh' >&2
  exit 1
fi

_gitea_compose_env() {
  local project_dir settings_file values image data config project http ssh backup domain listen
  project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || return 1
  settings_file="$project_dir/settings.json"
  [[ -r "$settings_file" ]] || { echo "Cannot read $settings_file" >&2; return 1; }
  if command -v jq >/dev/null; then
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
      or ((.sshDomain // "localhost") | type) != "string"
      or ((.sshDomain // "localhost") | test("^[a-zA-Z0-9._:-]+$") | not)
      or ((.sshListenPort // 2222) | type) != "number"
      or (.sshListenPort // 2222) != ((.sshListenPort // 2222) | floor)
      or (.sshListenPort // 2222) < 1024 or (.sshListenPort // 2222) > 65535
    then error("Invalid Compose settings")
    else [.image, .dataVolume, .configVolume, .project, (.httpPort | tostring), (.sshPort | tostring),
          (.backupVolume // (.project + "-backups")), (.sshDomain // "localhost"),
          ((.sshListenPort // 2222) | tostring)] | .[] end
  ' "$settings_file") || { echo 'No variables were changed.' >&2; return 1; }
  else
    # Read-only parsing with the existing management image; install nothing on
    # the host and never evaluate configuration as shell code.
    local helper=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:3}
    command -v podman >/dev/null || { echo 'Need jq or rootless Podman with the management image.' >&2; return 1; }
    [[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]] || return 1
    podman image exists "$helper" || { echo "Build the management image first: $helper" >&2; return 1; }
    values=$(podman run --rm --network none --userns=keep-id --security-opt label=disable \
      -v "$settings_file:/settings.json:ro" --entrypoint python3 "$helper" -c '
import json, re
with open("/settings.json") as f:
    c = json.load(f)
patterns = {"project": r"[a-zA-Z0-9_-]+", "dataVolume": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", "configVolume": r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", "image": r"docker\.gitea\.com/gitea(?::[0-9]+\.[0-9]+\.[0-9]+-rootless|@sha256:[a-f0-9]{64})"}
for key, pattern in patterns.items():
    if not isinstance(c.get(key), str) or not re.fullmatch(pattern, c[key]):
        raise ValueError("Invalid Compose setting: " + key)
if c["dataVolume"] == c["configVolume"]:
    raise ValueError("Data and config volumes must differ")
for key in ("httpPort", "sshPort"):
    if type(c.get(key)) is not int or not 1024 <= c[key] <= 65535:
        raise ValueError("Invalid Compose setting: " + key)
backup = c.get("backupVolume", c["project"] + "-backups")
if not isinstance(backup, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", backup):
    raise ValueError("Invalid backup volume")
domain = c.get("sshDomain", "localhost")
listen = c.get("sshListenPort", 2222)
if not isinstance(domain, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]+", domain):
    raise ValueError("Invalid SSH domain")
if type(listen) is not int or not 1024 <= listen <= 65535:
    raise ValueError("Invalid SSH listen port")
for value in (c["image"], c["dataVolume"], c["configVolume"], c["project"], c["httpPort"], c["sshPort"], backup, domain, listen):
    print(value)
    ') || { echo 'No variables were changed.' >&2; return 1; }
  fi
  local -a parsed
  mapfile -t parsed <<< "$values"
  [[ ${#parsed[@]} == 9 ]] || { echo 'Invalid Compose settings' >&2; return 1; }
  image=${parsed[0]}; data=${parsed[1]}; config=${parsed[2]}; project=${parsed[3]}
  http=${parsed[4]}; ssh=${parsed[5]}; backup=${parsed[6]}
  domain=${parsed[7]}; listen=${parsed[8]}
  export GITEA_IMAGE="$image" DATA_VOLUME="$data" CONFIG_VOLUME="$config"
  export HTTP_PORT="$http" SSH_PORT="$ssh" BACKUP_VOLUME="$backup"
  export SSH_DOMAIN="$domain" SSH_LISTEN_PORT="$listen"
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
