# Gitea with rootless Podman

Rootless Podman Compose, SQLite, a network-preserving backup sidecar, encrypted rclone dumps,
and deliberate upgrades following the official Gitea upgrade procedure.
The host needs a systemd Linux installation, rootless Podman configured for
its regular user, Bash and flock. Run all project commands as that user, without
sudo. Management dependencies are installed inside the included Containerfile,
not on the host. No Docker daemon or rootful Podman is used.

## Architectures and testing on x86-64

Use the same project and settings on x86-64 Linux (`amd64`) and 64-bit ARM Linux
(`arm64`). The management image is built natively on each host. Gitea image pulls
select the Podman host's platform, and initialization and upgrades check the
selected image before changing the deployment. No QEMU or architecture-specific
Compose file is needed. The selected Gitea release must publish a rootless image
for your host's architecture.

On your x86-64 machine, follow the new-installation steps below and run:

```sh
./scripts/test.sh
```

For an isolated test installation, use a distinct project name, fresh volume
names, free ports, and a separate backup prefix such as
`gitea-crypt:x86-test` in settings.json. This keeps test backup retention separate
from your eventual production instance. Populate Gitea with a test repository,
issue, and attachment; then test backup, restore, upgrade, and rollback. The unit
suite simulates Podman/rclone and covers both amd64 and arm64 platform selection;
real container testing requires the rootless Podman service on your test host.

When the Pi becomes available, copy the project and independent rclone credentials,
build the manager on the Pi, and restore your chosen snapshot. Do not copy the
x86 manager image or an architecture-specific Gitea image to the Pi.

## New installation

To use podman-compose directly with the settings in this project:

```sh
source ./scripts/compose-env.sh
podman-compose up -d
podman-compose down
```

Source the file again after editing settings.json or after restore updates volume
names. It exports COMPOSE_PROJECT_NAME and COMPOSE_FILE as well as image, volume,
port, project directory and socket variables. It starts/stops no services. It
uses an installed jq or reads settings with Python inside the existing management
image. For a fresh install, init.sh still creates the external data/config volumes
first; after initialization or restore, use up/down normally.

SSH configuration is exported from settings.json as follows:

| Setting | Compose environment | Meaning | Default |
|---|---|---|---|
| sshDomain | SSH_DOMAIN | Hostname advertised in SSH clone URLs | localhost |
| sshPort | SSH_PORT | Published host port and advertised clone port | 2222 |
| sshListenPort | SSH_LISTEN_PORT | Container SSH listener and mapping destination | 2222 |

For the restored test on raspberrypi, set sshDomain to raspberrypi, keep sshPort
at 2223, and sshListenPort at 2222. Source compose-env.sh and recreate the server
with `podman-compose up -d --force-recreate server` to apply the settings. Direct
environment overrides can also be exported after sourcing the script. Rebuild
the management image after updating manager.py to use these settings through
the management commands as well.

Copy this entire project to a writable directory on the host. Keep the directory
path free of spaces and %, &, or | if installing the included systemd units.

```sh
cp settings.example.json settings.json
chmod 600 settings.json
systemctl --user enable --now podman.socket
./scripts/init.sh
```

Edit settings.json before init: select an explicit official rootless Gitea
release supported on your host architecture, unused named-volume names, free ports, and a dedicated
rclone crypt prefix. The example version is a starting point, not an automatic
latest-release selection. Init pulls the image, records its digest, creates fresh
volumes with container ownership 1000:1000, and starts Gitea. Open port 3000 and
complete installation using SQLite, with its database path under /var/lib/gitea.
Use the same path when configuring repository and file storage. Preserve ports
3000 and 2222 unless you deliberately change them.

The manager is written in Python using only its standard library. Python runs
inside the management container and is not required on the Gitea host. The
manager image is built on first use. After changing its code or Containerfile,
rebuild it explicitly:

```sh
podman build -t localhost/gitea-podman-manager:3 -f Containerfile .
```

The manager uses the rootless Podman socket to manage containers. It mounts the
project directory and uses host networking to test Gitea's HTTP endpoint. Treat
this image and socket as trusted: the socket grants control of this user's
containers. The helper runs as your UID; short-lived tar/chown containers run as
container UID 0 inside the regular user's rootless Podman namespace.

## Adopt an existing Compose installation

For a backup of an existing aarch64 instance followed by bootstrap on x86-64 or
another ARM host, follow [the source-to-replacement walkthrough](docs/bootstrap-existing-instance.md).

Do not run init against existing volumes. Copy settings.example.json to
settings.json and set the actual HTTP/SSH ports and desired crypt remote.

```sh
podman ps
./scripts/adopt.sh EXISTING_SERVER_CONTAINER_ID
```

Adoption discovers the two actual volume names, Compose project label, running
version, and image digest. It does not move data, restart Gitea, or upgrade it.
This project supports the supplied Compose topology: SQLite and local storage
inside /var/lib/gitea and /etc/gitea. Additional mounts or external storage need a
separate backup design and are rejected. Copy any additional environment settings
from your old deployment into this project's compose.yaml before recreating it;
keep the data/config volume mappings and required image variables intact.

Use this project for future Compose operations. Disable the previous startup job
so two deployments do not compete. Adoption only records settings. Taking the
first backup from an existing deployment before switching its Compose topology
needs the separate migration procedure; do not recreate primary2's production
instance merely to add the unverified sidecar.
Do not run `compose down -v` or delete the original volumes.

## Configure encrypted cloud storage

Configure rclone using the manager container:

```sh
./scripts/configure-rclone.sh
```

Create a normal remote for your cloud service, then a `crypt` remote wrapping a
folder in it. Choose **standard filename encryption**, enable directory-name
encryption, and use strong independent crypt passwords. Set settings.json's
remote to a dedicated prefix such as `gitea-crypt:server`. Writable rclone backends
with list, upload, download, and delete operations are supported; authentication
and backend limits remain provider-specific. Browser authorization can be done
on another machine as described by rclone's configuration wizard.

Configuration is stored in private/rclone.conf. Rclone's stored obscured crypt
passwords are recoverable: protect this file like a password. Store an independent
recovery copy in a password manager or offline medium, including both crypt
passwords and cloud credentials. Do not keep your only key copy on the Gitea host or
inside this encrypted backup. OAuth tokens may need renewal on a replacement
machine. The backup does not include private/rclone.conf.

## Backups

The main Compose file now includes the backup sidecar. See
[its setup and dump/restore workflow](docs/backup-sidecar.md) before deploying it.

```sh
./scripts/backup.sh
./scripts/list-backups.sh
./scripts/start.sh
./scripts/install-systemd.sh
```

The Compose backup service runs daily at 03:00 UTC and catches up after downtime.
Set backupEnabled to false to disable scheduling, or backupHourUTC to another
hour (0–23) in settings.json. Manual backup.sh still works with scheduling disabled.
The systemd installer disables the old backup timer; the sidecar is the scheduler. Enable user
lingering (`loginctl enable-linger`) so services run at boot without a login;
this may require an administrator depending on your system's policy.

The backup service stays running on Gitea's Compose network. After checking
remote access and staging space, it stops only the server container and runs
gitea dump in a one-shot container using the exact server image and user, with
the same data/config volumes. The dump job uses no network. It writes a ZIP into
a named backup volume shared with the sidecar, then the server is restarted
before encrypted upload and verification. A separate config.tar preserves the
complete configuration volume. Dumps include repositories, database, SSH keys,
attachments, LFS, packages, and indexes under the standard rootless data layout.
The new dump format requires APP_DATA_PATH=/var/lib/gitea and repository
ROOT=/var/lib/gitea/git/repositories; a different layout is rejected before stop. See the official consistency guidance:
https://docs.gitea.com/administration/backup-and-restore/

Local staging is plaintext with owner-only access. Successful staging is removed;
failed uploads are retained, and the next backup command retries them before
capturing another snapshot. Allow at least twice the current volume size plus
256 MiB of free staging space. Files are encrypted before cloud upload using
rclone crypt. Every upload is verified by downloading and comparing its contents,
which works even without backend checksum support but doubles network traffic.

Only snapshots with a COMPLETE marker are restore candidates. Keep defaults to
14 completed ordinary snapshots. The latest upgrade recovery point is protected
in addition to that quota. Remote directories belonging to this instance are
pruned only after a successful upload. Do not share this prefix with other apps.
Manual failures produce a nonzero exit status; scheduled failures appear in the
backup container logs and are retried after a one-hour backoff; no notification
service is configured. Check those logs regularly.

## Restore on a new machine

Copy the project, create settings.json, configure the same crypt remote and
passwords, and enable the new user's Podman socket. Choose an unused Compose
project name and free HTTP/SSH ports. Existing containers under that project
cause restore to refuse the operation.

```sh
./scripts/list-backups.sh
./scripts/restore.sh latest
# Or choose a listed snapshot ID explicitly.
./scripts/install-systemd.sh
```

Restore verifies the manifest and archive checksums, selects the Gitea image,
creates fresh volumes, restores container ownership, updates
settings.json, and starts the recorded version. It preserves original volumes.
A recovery.md copy of these instructions and compose.yaml are also in each
snapshot. The archive manifest records the old settings; inspect it if recreating
old port mappings or deployment preferences. Use the same rootless image layout.
Snapshots record their source architecture. On the same architecture, restore
uses the recorded image digest. When moving between x86-64 and ARM64, it selects
the **same Gitea release** for the destination architecture, verifies its reported
version before touching volumes, and records the destination image's digest.
It does not combine migration to another architecture with a Gitea upgrade.
The image digest may differ between architectures; the data and configuration
are restored from the same snapshot. Older schema-1 snapshots without architecture
metadata are handled by inspecting their original image after pulling it.
The recorded image (or the same-version native image for a different architecture)
must remain available in the registry or a local image cache.
Keep an independent offline image export if registry-independent recovery matters.

Before exposing a restored instance, verify login, users/issues/attachments,
HTTP and SSH clone/push, SSH host identity, and code search. On an isolated test
restore, prevent outbound integrations, jobs, and webhooks from contacting real
services. If the hostname or ports change, update ROOT_URL, SSH_DOMAIN, and
SSH_PORT in /etc/gitea/app.ini using a container that mounts the config volume.
If installation paths change, regenerate repository hooks using the documented
`gitea admin regenerate hooks` command. Ports in settings.json describe host
publication; advertised URLs and SSH settings remain Gitea configuration.

## Upgrade according to official Gitea documentation

Authoritative procedure:
https://docs.gitea.com/installation/upgrade-from-gitea/
Official changelogs: https://blog.gitea.com/

For the current and target releases:

1. Read the official changelog and resolve breaking changes. Follow any official
   release-specific prerequisites or intermediate-version requirements.
2. Resolve deprecated configuration warnings shown in Site Administration.
3. Check custom-template compatibility and update incompatible templates.
4. Pull the new container image, stop Gitea, back up its database/configuration/
   application data, and start the new image. Gitea performs migrations itself.

The command automates step 4. The `--reviewed` flag acknowledges completion of
steps 1–3, including any required version steps; the script cannot prove that
human review is complete. It does not impose an invented upgrade-path policy.
Run separate upgrades for intermediate versions when official release guidance
requires them. Keep using the rootless image family.

```sh
./scripts/upgrade.sh TARGET_VERSION --reviewed
# TARGET_VERSION is an explicit stable version such as 28.0.0, not latest.
```

Additional project safeguards: serialize operations, pull before downtime,
verify the pre-upgrade cloud snapshot before starting the new image, pin its
digest, protect the recovery point, and check the reported version. If backup
fails, the original container is restarted; retry the retained upload with
backup.sh before another upgrade. Upgrade downtime includes capture, upload,
verification, and database migration.

Readiness is observed for 900 seconds by default, configurable via readySeconds.
A timeout does **not** stop migrations or automatically roll back. Inspect logs
and let an active migration finish. If Compose recreation fails after capture,
the verified recovery point still exists; inspect container state and choose
start.sh or explicit rollback.

Gitea warns that older releases must not use a migrated database. Rollback always
restores the old snapshot and image together, including for patch upgrades:

```sh
# Check that migrations are no longer running before choosing rollback.
./scripts/rollback.sh --accept-data-loss
```

Rollback downloads and verifies the recovery point before stopping the current
server, preserves upgraded volumes, restores into fresh volumes, and starts the
previous image. All changes since the recovery point are lost. Check login,
clone/push, attachments, and search after every upgrade or rollback.

## Operational failures and testing

To test container stop/start from a sidecar on primary2 without operating on
Gitea, follow [the separate lifecycle experiment](docs/container-lifecycle-test.md).

All commands and the sidecar share state/operation.lock. A scheduled attempt
encountering a busy operation defers safely. Partial cloud uploads have
no completion marker and are not restored. Before manually deleting local state,
inspect the backup volume's pending.json and state/rollback.json and preserve
needed archives. Failed ZIP uploads remain in the named backup volume.

A hard power loss or forcibly killed backup process may leave Gitea stopped.
Once the operation lock is released, use start.sh and inspect the previous backup
logs. Do not assume a partial snapshot is valid. Failed restore/upgrade volumes
are preserved for investigation and must be cleaned up deliberately.

Run the Python test suite:

```sh
./scripts/test.sh
bash -n scripts/run.sh
```

The test script uses an already installed Python 3.12+ when available, otherwise
it builds and runs the project's management container with rootless Podman. It
does not install Python or test dependencies on the host. Tests use unittest
and simulated Podman/rclone commands, including a real archive round trip.
The management image is now tagged :3 and includes the backup sidecar and ZIP
restore helper. Settings remain compatible. New backups use snapshot schema 2
(Gitea ZIP plus full config archive); schema 1 volume backups remain readable.
Restore keeps the destination Compose topology, including the backup service;
the source Compose file is preserved as recovery metadata.

Live acceptance on a rootless Podman host must also cover a populated backup and
restore, real Gitea version migration and rollback, interrupted upload, disk-full
capture, bad checksums, retention, reboot startup, and unattended timer execution.
The implementation workspace has a remote-only Podman client without a service
connection and no installed Python interpreter. Consequently the Python test
suite, container build, and live multi-architecture tests must be run on a host
with rootless Podman (or an existing Python installation for the unit tests).
Shell syntax checks can still run in the implementation workspace.
