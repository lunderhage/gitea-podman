#!/usr/bin/env bash
# Separate experiment: never operates on the production Gitea container/volumes.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ACTION=${1:-run}
PROJECT=gitea-lifecycle-test
CONTROLLER=gitea-lifecycle-test-controller
TARGET=gitea-lifecycle-test-target
COMPOSE_FILE="$ROOT/tests/lifecycle/compose.yaml"
export LIFECYCLE_MANAGER_IMAGE=${GITEA_MANAGER_IMAGE:-localhost/gitea-podman-manager:3}
export LIFECYCLE_SOCKET=${GITEA_PODMAN_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}
case "$ACTION" in
  run|cleanup) ;;
  *) echo "Usage: $0 [run|cleanup]" >&2; exit 2 ;;
esac
[[ $(id -u) != 0 ]] || { echo 'Run as a regular user, without sudo.' >&2; exit 1; }
command -v podman >/dev/null || { echo 'Podman is required; no containers changed.' >&2; exit 1; }
if ! ROOTLESS=$(podman info --format '{{.Host.Security.Rootless}}'); then
  echo 'No usable Podman service. No test containers were changed.' >&2
  exit 1
fi
[[ "$ROOTLESS" == true ]] || { echo 'A rootless Podman engine is required.' >&2; exit 1; }
[[ -S "$LIFECYCLE_SOCKET" ]] || {
  echo "Rootless socket missing: $LIFECYCLE_SOCKET" >&2
  echo 'Enable it with: systemctl --user enable --now podman.socket' >&2
  exit 1
}
RESULTS="$ROOT/test-results/lifecycle"
mkdir -p "$RESULTS"
chmod 700 "$RESULTS"
exec 9>"$RESULTS/operation.lock"
flock -n 9 || { echo 'A lifecycle test or cleanup is already running.' >&2; exit 1; }
# No Podman/conmon/Compose background process may inherit the operation lock.
podman() { command podman "$@" 9>&-; }
RUN_DIR=$(mktemp -d "$RESULTS/$(date -u +%Y%m%dT%H%M%SZ)-${ACTION}.XXXXXX")
START_EPOCH=$(date +%s)
START_TIME=$(date -Is)
TARGET_ID=''
CONTROLLER_ID=''
CONTROLLER_STARTED=''
TEST_COMPLETE=false
PROVIDER=''
exec > >(tee "$RUN_DIR/commands.log" 9>&-) 2>&1

# The custom experiment label guards against colliding with unrelated containers.
owned() {
  local name=$1 label
  label=$(podman inspect "$name" --format '{{index .Config.Labels "org.gitea-podman.lifecycle-test"}}') || return 1
  [[ "$label" == "$PROJECT" ]]
}
assert_names_safe() {
  local name id label
  for name in "$CONTROLLER" "$TARGET"; do
    if podman container exists "$name"; then
      owned "$name" || { echo "Refusing to touch unrelated container: $name"; return 1; }
    fi
  done
  for id in $(podman ps -aq --filter "label=com.docker.compose.project=$PROJECT"); do
    owned "$id" || { echo 'Unexpected container under the test project; refusing operation.'; return 1; }
  done
  if podman network exists gitea-lifecycle-test-network; then
    label=$(podman network inspect gitea-lifecycle-test-network --format '{{index .Labels "org.gitea-podman.lifecycle-test"}}')
    [[ "$label" == "$PROJECT" ]] || { echo 'Refusing to use an unrelated network with the test name.'; return 1; }
  fi
}
compose() {
  if [[ "$PROVIDER" == native ]]; then
    command podman-compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@" 9>&-
  else
    podman run --rm --userns=keep-id --network host --security-opt label=disable \
      -v "$LIFECYCLE_SOCKET:/run/podman/podman.sock" \
      -v "$ROOT:$ROOT:ro" -w "$ROOT" \
      -e CONTAINER_HOST=unix:///run/podman/podman.sock \
      -e HOME=/tmp/compose-home -e XDG_RUNTIME_DIR=/tmp/compose-runtime \
      -e LIFECYCLE_SOCKET -e LIFECYCLE_MANAGER_IMAGE \
      --entrypoint /bin/sh "$LIFECYCLE_MANAGER_IMAGE" -c \
      'mkdir -p "$HOME" "$XDG_RUNTIME_DIR"; chmod 700 "$HOME" "$XDG_RUNTIME_DIR"; exec podman-compose "$@"' \
      compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
  fi
}
controller_pod() {
  owned "$CONTROLLER_ID" && owned "$TARGET_ID" || {
    echo 'Test ownership check failed.'; return 1;
  }
  podman exec "$CONTROLLER_ID" podman --url unix:///run/podman/podman.sock "$@"
}
assert_controller_running() {
  local running started
  running=$(podman inspect "$CONTROLLER_ID" --format '{{.State.Running}}')
  started=$(podman inspect "$CONTROLLER_ID" --format '{{.State.StartedAt}}')
  [[ "$running" == true && "$started" == "$CONTROLLER_STARTED" ]] || {
    echo 'Controller stopped or restarted during the test.'; return 1;
  }
}
finish() {
  local status=$1 end_epoch end_time
  trap - EXIT INT TERM
  set +e
  # Recover only the exact disposable target selected by this run.
  if [[ "$ACTION" == run && "$status" != 0 && -n "$TARGET_ID" ]]; then
    if owned "$TARGET_ID"; then
      echo 'Failure: attempting to restart the disposable test target.'
      podman start "$TARGET_ID"
      echo "Recovery start exit status: $?"
    fi
  fi
  for name in "$TARGET" "$CONTROLLER"; do
    if podman container exists "$name" && owned "$name"; then
      podman inspect "$name" > "$RUN_DIR/${name}-final.json"
      podman logs "$name" > "$RUN_DIR/${name}.log" 2>&1
    fi
  done
  end_epoch=$(date +%s)
  end_time=$(date -Is)
  {
    echo "Action: $ACTION"
    echo "Project: $PROJECT"
    echo "Start: $START_TIME"
    echo "End: $end_time"
    echo "Compose provider: $PROVIDER"
    echo "Exit status: $status"
    if [[ "$status" == 0 && "$TEST_COMPLETE" == true ]]; then echo 'Result: PASS'; else echo 'Result: FAIL'; fi
    echo 'AppArmor rules were not changed by this test.'
    echo 'Inspect kernel messages for this exact test interval:'
    printf "sudo journalctl -k --since '@%s' --until '@%s' --no-pager --grep='apparmor=\"DENIED\"|pasta|passt'\n" "$START_EPOCH" "$end_epoch"
  } | tee "$RUN_DIR/report.txt" 9>&-
  echo "Diagnostics saved in: $RUN_DIR"
  if [[ "$ACTION" == run ]]; then
    echo 'Test containers preserved. Explicit cleanup:'
    echo "$ROOT/scripts/test-container-lifecycle.sh cleanup"
  fi
  exit "$status"
}
trap 'finish $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Lifecycle experiment on %s, started %s\n' "$(hostname)" "$START_TIME"
assert_names_safe
podman version > "$RUN_DIR/podman-version.txt"
podman info --format json > "$RUN_DIR/podman-info.json"
for profile in /etc/apparmor.d/usr.bin.pasta /etc/apparmor.d/local/usr.bin.pasta /etc/apparmor.d/usr.bin.podman; do
  if [[ -r "$profile" ]]; then
    { echo "File: $profile"; sha256sum "$profile"; cat "$profile"; } >> "$RUN_DIR/apparmor-profiles.txt"
  fi
done
# Reuse the project's container tools when the host lacks a Compose provider.
if ! podman image exists "$LIFECYCLE_MANAGER_IMAGE"; then
  podman build -t "$LIFECYCLE_MANAGER_IMAGE" -f "$ROOT/Containerfile" "$ROOT"
fi
if command -v podman-compose >/dev/null; then
  PROVIDER=native
  command podman-compose --version 9>&- > "$RUN_DIR/compose-version.txt" 2>&1
else
  PROVIDER=container
  podman run --rm --network host --entrypoint podman-compose \
    "$LIFECYCLE_MANAGER_IMAGE" --version > "$RUN_DIR/compose-version.txt" 2>&1
fi

if [[ "$ACTION" == cleanup ]]; then
  # Refuse unexpected project services instead of letting Compose remove them.
  ids=$(podman ps -aq --filter "label=com.docker.compose.project=$PROJECT")
  for id in $ids; do
    owned "$id" || { echo 'Unexpected container under the test project; refusing cleanup.'; exit 1; }
  done
  compose down
  for name in "$TARGET" "$CONTROLLER"; do
    if podman container exists "$name"; then echo "Cleanup did not remove $name"; exit 1; fi
  done
  TEST_COMPLETE=true
  exit 0
fi

compose up -d controller target
TARGET_ID=$(podman inspect "$TARGET" --format '{{.Id}}')
CONTROLLER_ID=$(podman inspect "$CONTROLLER" --format '{{.Id}}')
owned "$TARGET_ID" && owned "$CONTROLLER_ID"
CONTROLLER_STARTED=$(podman inspect "$CONTROLLER_ID" --format '{{.State.StartedAt}}')
assert_controller_running
[[ $(podman inspect "$TARGET_ID" --format '{{.State.Running}}') == true ]]
[[ $(podman inspect "$TARGET_ID" --format '{{.HostConfig.RestartPolicy.Name}}') == always ]]
podman inspect "$TARGET_ID" "$CONTROLLER_ID" > "$RUN_DIR/initial-containers.json"
podman pod ps --format json > "$RUN_DIR/pods.json"
# Read network membership from both containers, without assuming shared netns/pod.
TARGET_NETWORKS=$(podman inspect "$TARGET_ID" --format '{{range $name, $value := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}')
CONTROLLER_NETWORKS=$(podman inspect "$CONTROLLER_ID" --format '{{range $name, $value := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}')
shared=false
for network in $TARGET_NETWORKS; do
  for controller_network in $CONTROLLER_NETWORKS; do
    if [[ "$network" == "$controller_network" ]]; then
      shared=true
      podman network inspect "$network" > "$RUN_DIR/network-${network}.json"
    fi
  done
 done
[[ "$shared" == true ]] || { echo 'Services do not report a shared network; inspect saved diagnostics.'; exit 1; }
for attempt in {1..10}; do
  if podman exec "$CONTROLLER_ID" test -d /tmp/lifecycle-runtime; then break; fi
  sleep 1
done
podman exec "$CONTROLLER_ID" test -d /tmp/lifecycle-runtime
controller_pod info --format '{{.Host.Security.Rootless}}' > "$RUN_DIR/controller-engine-rootless.txt"
[[ $(cat "$RUN_DIR/controller-engine-rootless.txt") == true ]]

for cycle in 1 2 3; do
  echo "Cycle $cycle: stopping target from controller ($(date -Is))"
  if controller_pod stop --time 30 "$TARGET_ID"; then
    echo "Cycle $cycle: stop exit status 0"
  else
    status=$?
    echo "Cycle $cycle: stop exit status $status"
    podman inspect "$TARGET_ID" > "$RUN_DIR/cycle-${cycle}-failed-stop.json"
    exit "$status"
  fi
  [[ $(podman inspect "$TARGET_ID" --format '{{.State.Running}}') == false ]]
  [[ $(podman inspect "$TARGET_ID" --format '{{.State.ExitCode}}') == 0 ]]
  podman inspect "$TARGET_ID" > "$RUN_DIR/cycle-${cycle}-stopped.json"
  for second in {1..15}; do
    assert_controller_running
    [[ $(podman inspect "$TARGET_ID" --format '{{.State.Running}}') == false ]] || {
      echo "Target restarted automatically after $second seconds"; exit 1;
    }
    sleep 1
  done
  echo "Cycle $cycle: target stayed stopped for 15 seconds; controller uninterrupted"
  if controller_pod start "$TARGET_ID"; then
    echo "Cycle $cycle: start exit status 0"
  else
    status=$?; echo "Cycle $cycle: start exit status $status"; exit "$status"
  fi
  [[ $(podman inspect "$TARGET_ID" --format '{{.State.Running}}') == true ]]
  assert_controller_running
  podman inspect "$TARGET_ID" > "$RUN_DIR/cycle-${cycle}-restarted.json"
done
TEST_COMPLETE=true
