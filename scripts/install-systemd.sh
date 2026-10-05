#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $(id -u) != 0 ]] || { echo 'Run without sudo.' >&2; exit 1; }
[[ "$ROOT" != *' '* && "$ROOT" != *'%'* && "$ROOT" != *'&'* && "$ROOT" != *'|'* ]] || { echo 'Use a project path without spaces or %, &, |.' >&2; exit 1; }
DEST=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$DEST"
for unit in gitea-podman.service gitea-podman-backup.service gitea-podman-backup.timer; do
  sed "s|@PROJECT@|$ROOT|g" "$ROOT/systemd/$unit" > "$DEST/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now podman.socket gitea-podman.service gitea-podman-backup.timer
printf '%s\n' 'Enable lingering for this account with loginctl enable-linger, if your system permits it.'
