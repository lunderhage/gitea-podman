#!/usr/bin/env python3
"""Compose sidecar: consistent Gitea dumps followed by encrypted uploads."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
import yaml
from settings_io import settings_path

from manager import Manager, file_hash, parse_ini, stop_capture_restart, validate_manifest


class DumpManager(Manager):
    def __init__(self, root, command=None):
        super().__init__(root, command)
        self.state = Path(os.environ.get("GITEA_BACKUP_PATH", "/backup"))
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)

    def capture(self, info, kind):
        snapshot = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        directory = self.state / snapshot
        directory.mkdir(mode=0o700)
        temporary = self.state / (snapshot + "-tmp")
        temporary.mkdir(mode=0o700)
        uid, gid = map(int, info["owner"].split(":"))
        os.chown(directory, uid, gid)
        os.chown(temporary, uid, gid)
        volume = os.environ["GITEA_BACKUP_VOLUME"]
        try:
            # No exec into a stopped container. Use its exact image and configured
            # user in an isolated, one-shot job sharing its persistent volumes.
            self.pod("run", "--rm", "--network", "none", "--user", info["owner"],
                     "--workdir", f"/backup/{temporary.name}",
                     "-v", f'{self.c["dataVolume"]}:/var/lib/gitea',
                     "-v", f'{self.c["configVolume"]}:/etc/gitea',
                     "-v", f"{volume}:/backup", "--entrypoint", info["binary"], info["image"],
                     "dump", "-c", "/etc/gitea/app.ini", "--tempdir", f"/backup/{temporary.name}",
                     "--file", f"/backup/{snapshot}/gitea-dump.zip")
            with zipfile.ZipFile(directory / "gitea-dump.zip") as archive:
                if archive.testzip() is not None:
                    raise ValueError("Dump ZIP is corrupt")
                if "gitea-db.sql" not in archive.namelist():
                    raise ValueError("Dump ZIP is missing the database export")
            # Preserve the complete config volume, including certificates and
            # custom files outside app.ini. Gitea's data ZIP preserves SSH keys
            # because the supported rootless layout uses /var/lib/gitea as data.
            self.pod("run", "--rm", "--network", "none", "--user", "0",
                     "--security-opt", "label=disable", "--entrypoint", "tar",
                     "-v", f'{self.c["configVolume"]}:/source:ro', "-v", f"{volume}:/backup",
                     self.helper(), "-C", "/source", "--numeric-owner", "-cpf",
                     f"/backup/{snapshot}/config.tar", ".")
            shutil.copyfile(self.compose, directory / "compose.yaml")
            shutil.copyfile(self.root / "README.md", directory / "recovery.md")
            manifest = {"schema": 2, "id": snapshot, "kind": kind, "image": info["image"],
                        "version": info["version"], "owner": info["owner"],
                        "architecture": info["architecture"],
                        "settings": dict(self.c, image=info["image"]),
                        "hashes": {name: file_hash(directory / name) for name in
                                   ("gitea-dump.zip", "config.tar", "compose.yaml", "recovery.md")}}
            validate_manifest(manifest)
            self.write(directory / "manifest.json", manifest)
            self.write(self.state / "pending.json", {"id": snapshot})
            return manifest
        finally:
            shutil.rmtree(temporary)

    def inspect_server(self):
        info = super().inspect_server()
        sections = parse_ini(self.pod("exec", info["s"]["Id"], "cat", "/etc/gitea/app.ini"))
        if sections.get("security", {}).get("INSTALL_LOCK", "false").lower() != "true":
            raise ValueError("Complete Gitea installation before the first dump backup")
        if sections.get("server", {}).get("APP_DATA_PATH", "").rstrip("/") != "/var/lib/gitea":
            raise ValueError("Dump backups require the standard rootless APP_DATA_PATH=/var/lib/gitea")
        if sections.get("repository", {}).get("ROOT", "").rstrip("/") != "/var/lib/gitea/git/repositories":
            raise ValueError("Dump backups require the standard rootless repository path")
        info["binary"] = self.pod("exec", info["s"]["Id"], "sh", "-c",
                                 "command -v gitea || { test -x /app/gitea/gitea && echo /app/gitea/gitea; }").strip()
        if not info["binary"].startswith("/"):
            raise ValueError("Cannot determine the Gitea executable")
        return info

    def space(self):
        # The staging archive and dump temporary files are on the backup volume.
        super().space()

    def backup(self):
        self.settings()
        self.remote()
        pending = self.pending()
        if pending:
            manifest = validate_manifest(self.read(self.state / pending["id"] / "manifest.json"))
        else:
            info = self.inspect_server()
            self.space()
            manifest = stop_capture_restart(lambda: self.stop(info["s"]),
                                           lambda: self.capture(info, "ordinary"),
                                           lambda: self.restart(info["s"]["Id"]))
        self.upload(manifest)
        self.prune()
        shutil.rmtree(self.state / manifest["id"])
        self.write(self.state / "last-success.json", {"time": time.time(), "id": manifest["id"]})
        return manifest["id"]

    def rollback_record(self):
        filename = self.root / "state" / "rollback.json"
        return self.read(filename) if filename.exists() else None

    def restart(self, container_id):
        super().restart(container_id)
        if not self.server()["State"]["Running"]:
            raise ValueError("Gitea did not restart after the dump")

    def stop(self, server):
        try:
            super().stop(server)
        except BaseException:
            # A Podman error can occur after the application actually stopped.
            # Never proceed to capture on that error; attempt recovery first.
            self.restart(server["Id"])
            raise

    def recover(self):
        marker = self.state / "restart-container"
        if marker.exists():
            container_id = marker.read_text().strip()
            if self.server()["Id"] != container_id:
                raise ValueError("Recovery marker belongs to an older server; inspect it before continuing")
            self.restart(container_id)


def backup_once(manager, lock_held=False):
    lockfile = manager.root / "state" / "operation.lock"
    with lockfile.open("a") as lock:
        if not lock_held:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another Gitea operation is running")
        manager.recover()
        print(manager.backup(), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--idle", action="store_true", help="Keep the sidecar/network alive for manual backups only")
    parser.add_argument("--lock-held", action="store_true", help="Internal: caller already holds the project lock")
    args = parser.parse_args()
    for variable in ("HOME", "XDG_RUNTIME_DIR"):
        Path(os.environ[variable]).mkdir(parents=True, exist_ok=True, mode=0o700)
    manager = DumpManager(os.environ["GITEA_ROOT"])
    manager.verify_rootless()
    if args.once:
        backup_once(manager, args.lock_held)
        return
    if args.lock_held:
        raise ValueError("--lock-held is valid only with --once")
    if args.idle:
        print("Manual-only backup sidecar ready; server is untouched until a backup request", flush=True)
        while True:
            time.sleep(3600)
    print("Backup sidecar ready; monitoring configuration for scheduled backups", flush=True)
    retry_after = 0
    while True:
        try:
            manager.config = settings_path(manager.root)
            manager.c = manager.read(manager.config)
            if manager.c.get("backupEnabled", True):
                hour = manager.c.get("backupHourUTC", 3)
                if type(hour) is not int or not 0 <= hour <= 23:
                    raise ValueError("backupHourUTC must be an integer from 0 to 23")
                scheduled = datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()
                success = manager.state / "last-success.json"
                last = manager.read(success)["time"] if success.exists() else 0
                # No configured remote: stay alive on the network while the user
                # completes installation/configuration. Never stop Gitea here.
                if manager.rc.exists() and time.time() >= scheduled and last < scheduled and time.monotonic() >= retry_after:
                    backup_once(manager)
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, yaml.YAMLError) as error:
            print(f"Scheduled backup deferred or failed: {error}", file=sys.stderr, flush=True)
            retry_after = time.monotonic() + 3600
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
