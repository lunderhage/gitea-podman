# One-time backup of the original Gitea on primary2

Use the latest project code, including attach-existing-backup.sh and the sidecar's
--idle mode. Work in a separate local checkout, not the gitea-test configuration.
Copy project code only; do not copy settings, state or private credentials from an
active instance. Keep the checkout on primary2's local filesystem, not SSHFS.

Create a complete settings.yaml in that checkout with project gitea,
httpPort 3000, sshPort 2222, keep 14, readySeconds 900, backupEnabled false,
dataVolume gitea_gitea-data, configVolume gitea_gitea-config and a dedicated remote
such as gitea-crypt:original-aarch64. Use the example's pinned image as an initial
placeholder; adoption replaces it with the actual running image digest.

```sh
podman build -t localhost/gitea-podman-manager:5 -f Containerfile .
systemctl --user enable --now podman.socket
./scripts/adopt.sh gitea_server_1
./scripts/configure-rclone.sh
```

For Jottacloud, authenticate this configuration with a fresh personal login token.
The crypt remote points to jottacloud:gitea-backups and uses the existing crypt
passwords, standard filename encryption and directory-name encryption. Do not
copy another active configuration's refresh token. Preserve credentials offline.
Create the chosen destination using the management image's rclone command.

```sh
podman run --rm -it -v "$PWD/private:/config" \
  --entrypoint rclone localhost/gitea-podman-manager:5 \
  --config /config/rclone.conf mkdir gitea-crypt:original-aarch64
./scripts/attach-existing-backup.sh gitea_server_1
```

The attachment verifies rootless Podman, original project/service labels, running
state, named-volume mappings and one existing network. It creates only a manual
backup sidecar and a labelled staging volume. It does not recreate, stop or
upgrade the server. The controller's project/service labels let backup.sh find
it while the original server's existing network stays in use. The idle process
keeps that network alive without scheduling backups.

Start the actual backup during an acceptable maintenance window:

```sh
./scripts/backup.sh
./scripts/list-backups.sh
podman ps --filter name=gitea_server_1
```

The original server stops briefly for the matching-image dump, restarts before
upload, and the archive is encrypted and verified using the already tested schema-2
format. Failed stops/dumps abort upload and attempt restart. This uses the same
shared-network mechanism tested on primary2; unlike the old host-network manager,
the sidecar is attached to the original server's actual network.

Never run init.sh, start.sh, podman-compose up/down, restore.sh or upgrade.sh from
this attachment checkout against the existing production project. It is a backup
attachment, not a replacement deployment. After success, leave the manual-only
sidecar available or stop/remove ONLY the exact newly created container:

```sh
podman stop gitea-manual-backup
podman rm gitea-manual-backup
```

Leave gitea_server_1 and both original volumes intact. Keep the labelled staging
volume until recovery is verified, especially if upload failed. The snapshot ID
can be restored on raspberrypi using a separate unused Compose project.

Shell syntax is checked locally. The live original-instance attachment and dump
must still be verified on primary2; no direct access to that VM is configured in
the implementation workspace.
