#!/usr/bin/env bash
# Test the sourceable shell interface using a simulated Python settings renderer.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SCRATCH=$(mktemp -d /tmp/gitea-tls-env.XXXXXX)
mkdir -p "$SCRATCH/project/scripts" "$SCRATCH/bin"
cp "$ROOT/scripts/compose-env.sh" "$SCRATCH/project/scripts/"
printf 'tlsEnabled: false\n' > "$SCRATCH/project/settings.yaml"
cat > "$SCRATCH/bin/python3" <<'PYTHON_STUB'
#!/usr/bin/env bash
if [[ "$1" == -c ]]; then exit 0; fi
cat "$ENV_ROWS"
PYTHON_STUB
chmod +x "$SCRATCH/bin/python3"
export PATH="$SCRATCH/bin:$PATH"
export ENV_ROWS="$SCRATCH/exports.txt"
write_rows() {
  local mode=$1 webport rooturl protocol compose domain
  if [[ "$mode" == true ]]; then
    webport=8443; rooturl=https://git.example.net/; protocol=https
    compose="$SCRATCH/project/compose.yaml:$SCRATCH/project/compose.tls.yaml"; domain=git.example.net
  else
    webport=3000; rooturl=http://raspberrypi:3000/; protocol=http
    compose="$SCRATCH/project/compose.yaml"; domain=raspberrypi
  fi
  printf '%s\n' \
    GITEA_IMAGE docker.gitea.com/gitea:28.0.0-rootless \
    DATA_VOLUME data CONFIG_VOLUME config COMPOSE_PROJECT_NAME tls-test \
    HTTP_PORT "$webport" SSH_PORT 2222 BACKUP_VOLUME tls-test-backups \
    SSH_DOMAIN "$domain" SSH_LISTEN_PORT 2222 GITEA_PROTOCOL "$protocol" \
    GITEA_ROOT_URL "$rooturl" GITEA_DOMAIN "$domain" PUBLIC_HOSTNAME "$domain" \
    HTTP_REDIRECT_PORT 8080 ACME_EMAIL '' ACME_URL https://acme-v02.api.letsencrypt.org/directory \
    ACME_DIRECTORY /var/lib/gitea/https TLS_ENABLED "$mode" \
    COMPOSE_FILE "$compose" GITEA_PROJECT_DIR "$SCRATCH/project" > "$ENV_ROWS"
}
unset GITEA_MANAGER_IMAGE
write_rows true
source "$SCRATCH/project/scripts/compose-env.sh"
[[ "$TLS_ENABLED" == true && "$HTTP_PORT" == 8443 && "$SSH_DOMAIN" == git.example.net ]]
[[ "$COMPOSE_FILE" == "$SCRATCH/project/compose.yaml:$SCRATCH/project/compose.tls.yaml" ]]
write_rows false
source "$SCRATCH/project/scripts/compose-env.sh"
[[ "$TLS_ENABLED" == false && "$HTTP_PORT" == 3000 && "$GITEA_PROTOCOL" == http ]]
[[ "$COMPOSE_FILE" == "$SCRATCH/project/compose.yaml" ]]
# Invalid renderer output must not partly overwrite an existing environment.
sed -i 's/^GITEA_ROOT_URL$/UNEXPECTED_KEY/' "$ENV_ROWS"
if source "$SCRATCH/project/scripts/compose-env.sh"; then exit 1; fi
[[ "$GITEA_ROOT_URL" == http://raspberrypi:3000/ ]]
export GITEA_MANAGER_IMAGE=localhost/gitea-podman-manager:4
if source "$SCRATCH/project/scripts/compose-env.sh"; then exit 1; fi
echo 'PASS: TLS exports, HTTP rollback, atomic rejection, stale-image guard'
