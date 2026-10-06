# Back up an existing instance and bootstrap a replacement

This earlier walkthrough describes the first migration. Its source backup step
must now be tailored separately: backup.sh requires the new running Compose
sidecar. Do not run start.sh against primary2's production deployment to make
this walkthrough work. The existing-instance export remains deferred while the
new sidecar dump/restore format is tested.

Use the existing aarch64 instance as the source of truth. Capture its current
Gitea version and both volumes; restore that version on the replacement before
considering an upgrade. These commands assume the source uses **rootless Podman**
and SQLite. A rootless Gitea image running in Docker requires a different source
capture procedure: do not try to mount Docker's named volumes through Podman.

## 1. Prepare the source VM

Copy this project to the VM and run commands as the same regular user that owns
the existing Podman containers. Leave the original deployment and volumes intact.

```sh
cd /path/to/gitea-podman
cp settings.example.json settings.json
chmod 600 settings.json
systemctl --user enable --now podman.socket
podman ps --format '{{.ID}} {{.Names}} {{.Image}}'
```

Edit settings.json: retain host ports 3000 and 2222 if using the original Compose
file and choose a dedicated backup prefix such as
`gitea-crypt:original-aarch64`. The example image and volume names will be replaced
by adoption; adoption does not upgrade the instance.

```sh
./scripts/adopt.sh EXISTING_GITEA_CONTAINER_ID
./scripts/configure-rclone.sh
```

Configure a normal cloud remote and a crypt remote wrapping it. Use standard
filename encryption and enable directory-name encryption. Keep an independent
copy of private/rclone.conf and both crypt passwords in secure storage; the
backup does not contain the keys needed to decrypt itself.

The dedicated destination folder must exist before the first backup. If needed,
create it with the already built management image, using your configured prefix:

```sh
podman run --rm \
  -v "$PWD/private:/config:ro" \
  --entrypoint rclone localhost/gitea-podman-manager:3 \
  --config /config/rclone.conf mkdir gitea-crypt:original-aarch64
```

The original YAML's floating `latest-rootless` tag is not used to choose the
backup version. Adoption records the digest of the actual running image and
backup reads its actual Gitea version.

## 2. Capture and verify the source backup

```sh
./scripts/backup.sh
./scripts/list-backups.sh
```

There is a maintenance window while both volumes are captured. Gitea restarts
before the encrypted upload and download verification. The backup includes the
SQLite database, repositories, configuration, SSH identity, attachments, LFS,
packages, and indexes contained in the two volumes. Save the printed snapshot ID
and use that specific ID for bootstrap, rather than allowing a later backup to
change the meaning of `latest`.

Do not run init.sh or upgrade.sh on the source as part of this procedure. There
is no need to install this project's startup service or backup timer to take the
initial backup. Check that the original Gitea is healthy after capture.

## 3. Bootstrap the replacement host

Use x86-64 Linux now, or the aarch64 VM/64-bit Pi later. Copy the project, create
fresh settings.json, and securely supply the same rclone configuration and crypt
keys. Do not copy the source's state directory or its architecture-specific
container images. Reconfigure cloud authentication if tokens need renewal.

```sh
cd /path/to/gitea-podman
cp settings.example.json settings.json
chmod 600 settings.json
systemctl --user enable --now podman.socket
./scripts/configure-rclone.sh
```

Set project to an unused name such as `gitea-restored`, configure free ports,
and set remote to the source's exact encrypted prefix. If the source and target
share a host, choose different host ports. The target's initial volume names are
placeholders: restore creates fresh uniquely named volumes. Do **not** run init.sh
before restore.

```sh
./scripts/list-backups.sh
./scripts/restore.sh SAVED_SNAPSHOT_ID
```

Restore verifies the snapshot, selects the original Gitea release for the
replacement architecture, checks its version, and creates/imports fresh volumes.
On the same architecture it preserves the original digest; across architectures
it resolves and pins the same-version native image. The current settings.json
is updated to point to the restored image and volumes.

The restored app.ini initially retains the source hostname, ROOT_URL, and
advertised SSH address. For an x86 test instance, adjust those for its hostname
and published ports as described in the main README. Keep test instances isolated
from production integrations, mirrors, and webhook targets; copied settings and
credentials are real production configuration.

## 4. Verify before cutover

Check login, repository contents, issues and attachments, HTTP and SSH clone/push,
SSH host identity, LFS if used, and repository search. An x86 restore demonstrates
recovery on x86; repeat acceptance on the final ARM host before production cutover.

Keep the source running during a test restore. For final cutover, stop writes on
the source, take a final backup, restore that specific snapshot, verify it, and
then redirect clients. Keep writes frozen after the final capture: restarting
source Gitea during upload is part of the backup command, so preserve a separate
maintenance barrier that blocks user/integration writes until cutover completes.

After validation, install startup and backups on the target:

```sh
./scripts/install-systemd.sh
```

If the target is only for testing, change its backup destination to a separate
test prefix before enabling scheduled backups, so its retention cannot prune the
source's recovery points. For a production replacement, agree on which instance
owns scheduled backups and retire the original startup job when cutting over.
Do not delete source volumes until the replacement and recovery procedure are
verified.
