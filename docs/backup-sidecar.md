# Compose backup service

The main compose.yaml now contains server and backup services on the same gitea
network. The backup service stays running while it stops and restarts server,
using the rootless Podman socket, as demonstrated by the separate lifecycle test.
It does not edit AppArmor profiles or stop the entire Compose project.

## Start and test a new instance

Use a disposable instance first, with its own project name, data/config volume
names, ports, local project directory, and cloud prefix. Keep primary2's existing
instance untouched: its one-time export/migration remains a separate task.

Build the new management image and run the unit tests before starting services:

```sh
podman build -t localhost/gitea-podman-manager:5 -f Containerfile .
./scripts/test.sh
```

For a new installation, create settings.yaml from settings.example.yaml, adjust
the test names/ports/remote, and initialize normally:

```sh
systemctl --user enable --now podman.socket
./scripts/init.sh
```

Complete Gitea's installation using SQLite, configure rclone with
configure-rclone.sh, and create the encrypted destination as previously described.
Do not run init.sh on existing production volumes.

For an already initialized disposable instance using this project:

```sh
./scripts/start.sh
```

The wrapper supplies Compose with GITEA_IMAGE, DATA_VOLUME, CONFIG_VOLUME,
GITEA_PROJECT_DIR, GITEA_PODMAN_SOCKET, GITEA_MANAGER_IMAGE and BACKUP_VOLUME from
settings and the invoking user's socket. If invoking podman-compose directly,
export these variables yourself. The backup volume name defaults to
`<project>-backups`; optional backupVolume in settings.yaml overrides it.

The backup container owns scheduling: backupEnabled defaults to true and
backupHourUTC defaults to 3 (daily at 03:00 UTC). It reads changed settings without
requiring container recreation. Set backupEnabled=false for manual-only testing.
Failed scheduled attempts back off for one hour, and manual requests remain
available immediately. The systemd installer disables the previous backup timer
so there is only one scheduler. Retention remains keep=14 completed snapshots.

## Manual backup

```sh
./scripts/backup.sh
./scripts/list-backups.sh
```

backup.sh executes the sidecar's one-shot command while the normal sidecar process
continues keeping the network alive. Manual operations, the scheduled sidecar,
and upgrades share the project operation lock. The sidecar retries an unfinished
upload before capturing another snapshot.

The workflow is:

1. Verify encryption settings, remote connectivity, installation, SQLite, supported
   paths, image/user metadata and free space before stopping Gitea.
2. Stop only server and verify clean shutdown. A Podman stop error aborts capture
   and attempts to restart the server, even if the error happened during networking
   cleanup after the application exited.
3. Run a short-lived dump container using the server's exact image digest, Gitea
   executable, UID/GID, and persistent data/config volumes. This job has no network
   and runs gitea dump -c /etc/gitea/app.ini with workdir and tempdir on /backup.
4. Write gitea-dump.zip into the named backup volume shared with the sidecar; also
   archive the full configuration volume as config.tar, preserving certificates
   and other configuration files outside app.ini.
5. Restart server after capture, including on capture failure.
6. Upload through rclone crypt, verify by downloading, publish COMPLETE, prune
   completed old snapshots, and remove successful staging.

The dump layout requires APP_DATA_PATH=/var/lib/gitea and repository
ROOT=/var/lib/gitea/git/repositories. These are the official rootless template
values. Other layouts are rejected before stop. Database/files are local and
SQLite-only; external storage is still unsupported. Credentials remain in the
writable private/rclone.conf bind mount so Jottacloud token rotation persists.

A completed snapshot contains gitea-dump.zip, config.tar, compose.yaml,
recovery.md, manifest.json and COMPLETE. The manifest records schema 2, image
version/digest, architecture, owner and hashes. Plaintext local staging is in the
named backup volume, not the project's state folder. Failed staging is retained.

## Restore and failure recovery

restore.sh accepts new schema-2 dumps and old schema-1 volume archives. New dumps
are verified before importing into fresh volumes. Configuration comes from the
complete config archive; repositories, custom files, SSH keys and other data are
restored from the ZIP. LFS/attachments are placed at their configured local paths.
The native SQLite file is used when present, with SQL import as a fallback when
missing; SQLite quick_check must succeed before startup. The destination Compose
file stays in place so its backup service is preserved.

Restore on the same architecture uses the original digest; a different
architecture uses the same release's native image and verifies its version. Run
restore.sh with an unused project and free ports. Restore variables in Compose
are not added by this change; startup bootstrap still uses the existing restore
command. A restore-before-start declarative mode can be added separately.

After a crashed backup process, a restart-container marker in the backup volume
records recovery intent. A later one-shot or scheduled attempt acquires the lock
before restarting that exact server and proceeding. It refuses a marker belonging
to an older/replaced server. Hard termination can still leave Gitea stopped until
that recovery attempt; inspect container state and start the server if necessary.

Read sidecar logs using the backup container's ID from podman ps. Do not share the
project/backup volume with another instance. Full lifecycle failure logs and
Gitea dump-and-restore acceptance must be checked on a working rootless host.
The implementation workspace cannot run Python/container tests; the previously
reported lifecycle PASS on primary2 did not yet exercise Gitea dumping.
