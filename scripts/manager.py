#!/usr/bin/env python3
"""Manage Gitea through a rootless Podman service and an encrypted rclone remote.

Python's standard library and PyYAML are used. Shell wrappers provide the operation
lock and launch this program inside the management container. Settings and
snapshot formats stay compatible with earlier versions of the project.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import yaml
from settings_io import settings_path, load_settings, save_settings


IMAGE_DIGEST = r"docker\.gitea\.com/gitea@sha256:[a-f0-9]{64}"
SNAPSHOT_ID = r"\d{8}T\d{6}Z-[a-f0-9]{8}"
SNAPSHOT_FILES = ("data.tar", "config.tar", "compose.yaml", "recovery.md")
DUMP_SNAPSHOT_FILES = ("gitea-dump.zip", "config.tar", "compose.yaml", "recovery.md")
UPGRADE_GUIDE = "https://docs.gitea.com/installation/upgrade-from-gitea/"


def release(version):
    """Accept stable, explicit versions rather than floating image tags."""
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use an explicit stable version, e.g. 28.0.0")
    return version


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("Invalid snapshot manifest")
    patterns = {"id": SNAPSHOT_ID, "image": IMAGE_DIGEST, "owner": r"[0-9]+:[0-9]+"}
    valid = manifest.get("schema") in (1, 2)
    for key, pattern in patterns.items():
        value = manifest.get(key)
        valid = valid and isinstance(value, str) and re.fullmatch(pattern, value) is not None
    hashes = manifest.get("hashes")
    valid = valid and isinstance(hashes, dict)
    if valid:
        expected_files = DUMP_SNAPSHOT_FILES if manifest["schema"] == 2 else SNAPSHOT_FILES
        valid = set(hashes) == set(expected_files) and all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
            for value in hashes.values()
        )
    if not valid:
        raise ValueError("Invalid snapshot manifest")
    if "architecture" in manifest and (not isinstance(manifest["architecture"], str) or
                                        not re.fullmatch(r"[a-z0-9_]+", manifest["architecture"])):
        raise ValueError("Invalid snapshot architecture")
    release(manifest.get("version"))
    return manifest


def retained_snapshots(ids, keep, protected_id=None):
    """Return old snapshots eligible for deletion, preserving recovery points."""
    return [snapshot for index, snapshot in enumerate(sorted(ids, reverse=True))
            if index >= keep and snapshot != protected_id]


def stop_capture_restart(stop, capture, restart):
    stop()
    try:
        return capture()
    finally:
        restart()


def parse_ini(text):
    """Read Gitea's section/key syntax without interpolation of percent signs."""
    sections = {"DEFAULT": {}}
    section = "DEFAULT"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        match = re.match(r"\[([^\]]+)\]", line)
        if match:
            section = match.group(1).lower()
            sections.setdefault(section, {})
        elif "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] in "\"'`" and value[0] == value[-1]:
                value = value[1:-1]
            sections[section][key.strip().upper()] = value
    return sections


def file_hash(filename):
    digest = hashlib.sha256()
    with Path(filename).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_command(program, arguments, *, env=None):
    result = subprocess.run(
        [program, *(str(argument) for argument in arguments)],
        check=True, text=True, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
        env=env,
    )
    return result.stdout


class Manager:
    """Coordinate containers, snapshots, and deployment configuration."""

    def __init__(self, root, command=None):
        self.root = Path(root)
        self.command = command or run_command
        self.config = settings_path(self.root)
        self.state = self.root / "state"
        self.private = self.root / "private"
        self.compose = self.root / "compose.yaml"
        for directory in (self.state, self.private):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.c = self.read(self.config) if self.config.exists() else {}
        self.rc = self.private / "rclone.conf"

    @staticmethod
    def read(filename):
        if Path(filename).suffix in (".yaml", ".yml"):
            return load_settings(filename)
        with Path(filename).open(encoding="utf-8") as stream:
            return json.load(stream)

    @staticmethod
    def write(filename, value):
        if Path(filename).suffix in (".yaml", ".yml"):
            save_settings(filename, value)
            return
        filename = Path(filename)
        temporary = filename.with_name(filename.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(filename)

    def pod(self, *arguments):
        return self.command("podman", [str(argument) for argument in arguments])

    def pod_json(self, *arguments):
        return json.loads(self.pod(*arguments))

    def rclone(self, *arguments):
        return self.command("rclone", ["--config", str(self.rc), "--transfers", "1",
                                      "--checkers", "2", *(str(arg) for arg in arguments)])

    def verify_rootless(self):
        info = self.pod_json("info", "--format", "json")
        if info.get("host", {}).get("security", {}).get("rootless") is not True:
            raise ValueError("A rootless Podman service is required")

    def settings(self):
        project = self.c.get("project", "")
        volumes = [self.c.get("dataVolume", ""), self.c.get("configVolume", "")]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", project) or not all(
            isinstance(volume, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume)
            for volume in volumes
        ) or volumes[0] == volumes[1]:
            raise ValueError("Invalid project/volume settings")
        for key in ("keep", "readySeconds"):
            if type(self.c.get(key)) is not int or self.c[key] < 1:
                raise ValueError("keep and readySeconds must be positive integers")
        for key in ("httpPort", "sshPort"):
            if type(self.c.get(key)) is not int or not 1024 <= self.c[key] <= 65535:
                raise ValueError("Ports must be integers from 1024 to 65535")
        domain = self.c.get("sshDomain", "localhost")
        listen = self.c.get("sshListenPort", 2222)
        if not isinstance(domain, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]+", domain):
            raise ValueError("sshDomain must be a hostname or IP address")
        if type(listen) is not int or not 1024 <= listen <= 65535:
            raise ValueError("sshListenPort must be an integer from 1024 to 65535")
        if not re.fullmatch(
            r"docker\.gitea\.com/gitea(?::\d+\.\d+\.\d+-rootless|@sha256:[a-f0-9]{64})",
            self.c.get("image", ""),
        ):
            raise ValueError("Use a pinned official rootless image")

    def env(self):
        return dict(os.environ, GITEA_IMAGE=self.c["image"],
                    DATA_VOLUME=self.c["dataVolume"], CONFIG_VOLUME=self.c["configVolume"],
                    HTTP_PORT=str(self.c["httpPort"]), SSH_PORT=str(self.c["sshPort"]),
                    SSH_DOMAIN=self.c.get("sshDomain", "localhost"),
                    SSH_LISTEN_PORT=str(self.c.get("sshListenPort", 2222)),
                    GITEA_PROJECT_DIR=str(self.root),
                    GITEA_PODMAN_SOCKET=os.environ.get("GITEA_PODMAN_SOCKET", ""),
                    GITEA_MANAGER_IMAGE=self.helper(),
                    BACKUP_VOLUME=self.c.get("backupVolume", self.c["project"] + "-backups"))

    def backup_container(self):
        ids = self.pod("ps", "--filter", f'label=com.docker.compose.project={self.c["project"]}',
                       "--filter", "label=com.docker.compose.service=backup", "--format", "{{.ID}}").split()
        if len(ids) > 1:
            raise ValueError("More than one backup sidecar exists for this project")
        return ids[0] if ids else None

    def compose_run(self, *arguments):
        return self.command("podman-compose", ["-p", self.c["project"], "-f",
                            str(self.compose), *arguments], env=self.env())

    def server(self):
        ids = self.pod("ps", "-a", "--filter",
                       f'label=com.docker.compose.project={self.c["project"]}',
                       "--filter", "label=com.docker.compose.service=server",
                       "--format", "{{.ID}}").split()
        if len(ids) != 1:
            raise ValueError(f'Expected one server for project {self.c["project"]}; found {len(ids)}')
        return self.pod_json("inspect", ids[0])[0]

    def remote(self):
        remote = self.c.get("remote", "")
        if not re.fullmatch(r"[\w-]+:[\w./-]+", remote) or ".." in remote:
            raise ValueError("Set a crypt remote with a dedicated prefix, e.g. gitea-crypt:server")
        configuration = json.loads(self.rclone("config", "dump"))
        crypt = configuration.get(remote.split(":", 1)[0], {})
        # Rclone omits default-valued options from the saved configuration.
        if (crypt.get("type") != "crypt" or crypt.get("filename_encryption", "standard") != "standard"
                or str(crypt.get("directory_name_encryption", "true")).lower() == "false"):
            raise ValueError("Remote must use crypt with standard filename and directory encryption")
        self.rclone("lsf", remote, "--max-depth", "1")
        return remote.rstrip("/")

    @staticmethod
    def image_digest(image):
        return next((digest for digest in image.get("RepoDigests", [])
                     if re.fullmatch(IMAGE_DIGEST, digest)), None)

    def host_architecture(self):
        architecture = self.pod_json("info", "--format", "json")["host"]["arch"]
        if not isinstance(architecture, str) or not re.fullmatch(r"[a-z0-9_]+", architecture):
            raise ValueError("Cannot determine the Podman host architecture")
        return architecture

    def pull_native_image(self, reference):
        """Select the daemon host's platform, then pin its official image digest."""
        architecture = self.host_architecture()
        self.pod("pull", "--platform", f"linux/{architecture}", reference)
        image = self.pod_json("image", "inspect", reference)[0]
        if image.get("Architecture") != architecture:
            raise ValueError(f'Target image architecture {image.get("Architecture")} '
                             f"does not match host {architecture}")
        digest = self.image_digest(image)
        if not digest:
            raise ValueError("Missing target digest")
        return digest

    def restore_image(self, manifest):
        """Use the exact image on the original platform, the same release elsewhere."""
        architecture = self.host_architecture()
        source = manifest.get("architecture")
        reference = manifest["image"]
        if source is None:
            # Schema-1 backups made before architecture metadata was added.
            self.pod("pull", reference)
            source = self.pod_json("image", "inspect", reference)[0].get("Architecture")
        if source != architecture:
            reference = f'docker.gitea.com/gitea:{manifest["version"]}-rootless'
            print(f"Restoring from {source} to {architecture}; selecting the same "
                  f'Gitea release {manifest["version"]} for the destination host.')
        digest = self.pull_native_image(reference)
        output = self.pod("run", "--rm", "--network", "none", "--entrypoint",
                          "gitea", digest, "--version")
        match = re.search(r"version (\d+\.\d+\.\d+)", output)
        if not match or match.group(1) != manifest["version"]:
            raise ValueError("Restore image does not match the snapshot's Gitea version")
        return digest

    def inspect_server(self):
        server = self.server()
        if not server["State"]["Running"]:
            raise ValueError("Gitea must be running before backup/upgrade")
        mounts = server.get("Mounts", [])
        for destination, name in (("/var/lib/gitea", self.c["dataVolume"]),
                                  ("/etc/gitea", self.c["configVolume"])):
            if not any(mount.get("Destination") == destination and
                       mount.get("Type") == "volume" and mount.get("Name") == name
                       for mount in mounts):
                raise ValueError(f"Unexpected mount at {destination}")
        if any(mount.get("Destination") not in
               ("/var/lib/gitea", "/etc/gitea", "/etc/localtime", "/etc/timezone")
               for mount in mounts):
            raise ValueError("Additional mounts need an explicit backup design")
        image_info = self.pod_json("image", "inspect", server["Image"])[0]
        image = self.image_digest(image_info)
        if not image:
            raise ValueError("Cannot record the official image digest")
        output = self.pod("exec", server["Id"], "gitea", "--version")
        match = re.search(r"version (\d+\.\d+\.\d+)", output)
        version = release(match.group(1) if match else None)
        owner = self.pod("exec", server["Id"], "sh", "-c",
                         'printf "%s:%s" "$(id -u)" "$(id -g)"').strip()
        sections = parse_ini(self.pod("exec", server["Id"], "cat", "/etc/gitea/app.ini"))
        database = sections.get("database", {})
        if database.get("DB_TYPE", "").lower() != "sqlite3":
            raise ValueError("Only SQLite is supported")

        def under(value, base="/var/lib/gitea"):
            return isinstance(value, str) and ".." not in value.split("/") and (
                value == base or value.startswith(base + "/"))

        if not under(database.get("PATH")):
            raise ValueError("SQLite must be inside /var/lib/gitea")
        path_keys = {"PATH", "ROOT", "ROOT_PATH", "APP_DATA_PATH", "LFS_CONTENT_PATH",
                     "AVATAR_UPLOAD_PATH", "REPOSITORY_AVATAR_UPLOAD_PATH",
                     "ISSUE_INDEXER_PATH", "REPO_INDEXER_PATH"}
        for section, entries in sections.items():
            for key, value in entries.items():
                if (key == "STORAGE_TYPE" or section.startswith("storage") and key == "TYPE"):
                    if value.lower() != "local":
                        raise ValueError(f"External storage: [{section}] {key}")
                if key in path_keys and value and not under(value) and not under(value, "/etc/gitea"):
                    raise ValueError(f"Uncovered or ambiguous storage path: [{section}] {key}; "
                                     "use an absolute volume path")
        return {"s": server, "image": image, "version": version, "owner": owner,
                "architecture": image_info["Architecture"]}

    def stop(self, server):
        restart_file = self.state / "restart-container"
        restart_file.write_text(server["Id"], encoding="utf-8")
        restart_file.chmod(0o600)
        self.pod("stop", "--time", "120", server["Id"])
        state = self.pod_json("inspect", server["Id"])[0]["State"]
        if state["Running"] or state.get("OOMKilled") or state.get("ExitCode") != 0:
            self.restart(server["Id"])
            raise ValueError("Gitea did not shut down cleanly; backup aborted")

    def restart(self, container_id):
        self.pod("start", container_id)
        (self.state / "restart-container").unlink(missing_ok=True)

    @staticmethod
    def helper():
        return os.environ.get("GITEA_MANAGER_IMAGE", "localhost/gitea-podman-manager:4")

    def capture(self, info, kind):
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        directory = self.state / snapshot_id
        directory.mkdir(mode=0o700)
        # The remote Podman client cannot export volumes. Tar in a short-lived
        # rootless container preserves container UID/GID values instead.
        for filename, volume in (("data.tar", self.c["dataVolume"]),
                                 ("config.tar", self.c["configVolume"])):
            self.pod("run", "--rm", "--network", "none", "--security-opt", "label=disable", "--user", "0",
                     "--entrypoint", "tar", "-v", f"{volume}:/source:ro", "-v",
                     f"{directory}:/staging", self.helper(), "-C", "/source",
                     "--numeric-owner", "-cpf", f"/staging/{filename}", ".")
        shutil.copyfile(self.compose, directory / "compose.yaml")
        shutil.copyfile(self.root / "README.md", directory / "recovery.md")
        manifest = {"schema": 1, "id": snapshot_id, "kind": kind,
                    "image": info["image"], "version": info["version"], "owner": info["owner"],
                    "settings": dict(self.c, image=info["image"]),
                    "hashes": {filename: file_hash(directory / filename) for filename in SNAPSHOT_FILES}}
        if "architecture" in info:
            manifest["architecture"] = info["architecture"]
        validate_manifest(manifest)
        self.write(directory / "manifest.json", manifest)
        self.write(self.state / "pending.json", {"id": snapshot_id})
        return manifest

    def upload(self, manifest):
        directory = self.state / manifest["id"]
        destination = f'{self.remote()}/{manifest["id"]}'
        self.rclone("copy", directory, destination, "--exclude", "COMPLETE")
        self.rclone("check", directory, destination, "--download", "--one-way", "--exclude", "COMPLETE")
        marker = directory / "COMPLETE"
        marker.write_text(file_hash(directory / "manifest.json") + "\n", encoding="utf-8")
        marker.chmod(0o600)
        self.rclone("copyto", marker, f"{destination}/COMPLETE")
        (self.state / "pending.json").unlink()

    def list_backups(self):
        files = self.rclone("lsf", self.remote(), "--recursive", "--files-only").splitlines()
        return sorted(filename.split("/")[0] for filename in files
                      if re.fullmatch(SNAPSHOT_ID + r"/COMPLETE", filename))

    def rollback_record(self):
        filename = self.state / "rollback.json"
        return self.read(filename) if filename.exists() else None

    def pending(self):
        filename = self.state / "pending.json"
        return self.read(filename) if filename.exists() else None

    def prune(self):
        record = self.rollback_record()
        protected_id = record["id"] if record else None
        ordinary = [snapshot for snapshot in self.list_backups() if snapshot != protected_id]
        for snapshot in retained_snapshots(ordinary, self.c["keep"], protected_id):
            self.rclone("purge", f'{self.c["remote"]}/{snapshot}')

    def backup(self):
        self.settings()
        self.remote()
        pending = self.pending()
        if pending:
            manifest = validate_manifest(self.read(self.state / pending["id"] / "manifest.json"))
        else:
            info = self.inspect_server()
            self.space()
            manifest = stop_capture_restart(
                lambda: self.stop(info["s"]),
                lambda: self.capture(info, "ordinary"),
                lambda: self.restart(info["s"]["Id"]),
            )
        self.upload(manifest)
        self.prune()
        shutil.rmtree(self.state / manifest["id"])
        return manifest["id"]

    def space(self):
        size = 0
        for volume in (self.c["dataVolume"], self.c["configVolume"]):
            output = self.pod("run", "--rm", "--network", "none", "--entrypoint", "du", "-v",
                              f"{volume}:/source:ro", self.helper(), "-sk", "/source")
            size += int(output.split()[0]) * 1024
        if shutil.disk_usage(self.state).free < size * 2 + 256 * 1024 * 1024:
            raise ValueError("Insufficient staging space")

    def download(self, snapshot_id):
        ids = self.list_backups()
        if snapshot_id == "latest":
            snapshot_id = ids[-1] if ids else None
        if not snapshot_id or snapshot_id not in ids:
            raise ValueError("No matching complete snapshot")
        directory = self.state / ("restore-" + snapshot_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.rclone("copy", f'{self.c["remote"]}/{snapshot_id}', directory)
        if (directory / "COMPLETE").read_text(encoding="utf-8").strip() != file_hash(directory / "manifest.json"):
            raise ValueError("Manifest checksum mismatch")
        manifest = validate_manifest(self.read(directory / "manifest.json"))
        if manifest["id"] != snapshot_id:
            raise ValueError("Snapshot ID mismatch")
        for filename, digest in manifest["hashes"].items():
            if file_hash(directory / filename) != digest:
                raise ValueError(f"Checksum mismatch: {filename}")
        return manifest, directory

    def restore(self, snapshot_id, *, rollback=False):
        self.settings()
        manifest, directory = self.download(snapshot_id)
        image = self.restore_image(manifest)
        servers = self.pod("ps", "-a", "--filter",
                           f'label=com.docker.compose.project={self.c["project"]}',
                           "--format", "{{.ID}}").strip()
        if servers and not rollback:
            raise ValueError("Restore requires an unused Compose project; select a new project and free ports")
        if rollback:
            self.compose_run("stop", "server")
        suffix = secrets.token_hex(4)
        data = f'{self.c["project"]}-restore-data-{suffix}'
        config = f'{self.c["project"]}-restore-config-{suffix}'
        for volume in (data, config):
            existing = self.pod("volume", "ls", "--format", "{{.Name}}").splitlines()
            if volume in existing:
                raise ValueError("Recovery volume already exists")
            self.pod("volume", "create", volume)
        self.restore_payload(manifest, directory, data, config)
        for volume in (data, config):
            self.pod("run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "chown", "-v",
                     f"{volume}:/restore", self.helper(), "-R", manifest["owner"], "/restore")
        previous = dict(self.c)
        self.c.update(image=image, dataVolume=data, configVolume=config)
        shutil.copyfile(self.compose, self.state / "before-restore-compose.yaml")
        # Retain the destination Compose topology (including its sidecar).
        # The original Compose file remains available in the snapshot metadata.
        self.write(self.state / "before-restore.json", previous)
        self.write(self.config, self.c)
        self.compose_run("up", "-d")
        if not self.ready(manifest["version"]):
            raise ValueError("Restored server not ready; inspect logs. Original volumes are preserved.")
        shutil.rmtree(directory)
        print(f'Restored {manifest["id"]}. Check login, SSH clone/push, and search.')

    def restore_payload(self, manifest, directory, data, config):
        archives = ((config, "config.tar"),) if manifest["schema"] == 2 else ((data, "data.tar"), (config, "config.tar"))
        for volume, filename in archives:
            self.pod("run", "--rm", "--network", "none", "--security-opt", "label=disable", "--user", "0",
                     "--entrypoint", "tar", "-v", f"{volume}:/restore", "-v",
                     f"{directory}:/staging:ro", self.helper(), "-C", "/restore",
                     "--numeric-owner", "-xpf", f"/staging/{filename}")
        if manifest["schema"] == 2:
            self.pod("run", "--rm", "--network", "none", "--security-opt", "label=disable",
                     "--user", "0", "--entrypoint", "python3", "-v", f"{data}:/var/lib/gitea",
                     "-v", f"{config}:/etc/gitea:ro", "-v", f"{directory}:/staging:ro",
                     self.helper(), "/opt/gitea/dump_restore.py", "/staging/gitea-dump.zip")

    def ready(self, version):
        deadline = time.monotonic() + self.c["readySeconds"]
        while time.monotonic() < deadline:
            try:
                with urlopen(f'http://127.0.0.1:{self.c["httpPort"]}/api/v1/version', timeout=10) as response:
                    if json.load(response).get("version") == version:
                        return True
            except (OSError, HTTPError, URLError, ValueError):
                pass
            time.sleep(3)
        return False

    def upgrade(self, version, reviewed=False):
        release(version)
        self.settings()
        self.remote()
        if not reviewed:
            raise ValueError(f"First review {UPGRADE_GUIDE} and official release notes; resolve Site "
                             "Administration deprecation warnings and template incompatibilities. "
                             "Then use upgrade.sh VERSION --reviewed. Follow release-specific "
                             "intermediate-version requirements.")
        if self.pending():
            raise ValueError("Resolve pending backup before upgrading")
        info = self.inspect_server()
        if tuple(map(int, version.split("."))) <= tuple(map(int, info["version"].split("."))):
            raise ValueError("Target must be newer; use rollback for recovery")
        print(f'Official upgrade guidance: {UPGRADE_GUIDE}\nRelease notes: https://blog.gitea.com/\n'
              f'Upgrading {info["version"]} -> {version}')
        tag = f"docker.gitea.com/gitea:{version}-rootless"
        digest = self.pull_native_image(tag)
        self.space()
        self.stop(info["s"])
        try:
            manifest = self.capture(info, "upgrade")
            self.upload(manifest)
        except BaseException:
            self.restart(info["s"]["Id"])
            raise
        self.write(self.state / "rollback.json", {"id": manifest["id"],
                   "previousSettings": dict(self.c, image=info["image"]), "target": digest})
        self.c["image"] = digest
        self.write(self.config, self.c)
        # Remove the old-image restart marker BEFORE migration can start.
        (self.state / "restart-container").unlink(missing_ok=True)
        self.compose_run("up", "-d", "--force-recreate", "server")
        if not self.ready(version):
            print("Readiness observation timed out. Migrations may still be running. No automatic "
                  "rollback or interruption. Inspect podman logs, then explicitly use rollback.sh "
                  "if needed.", file=sys.stderr)
            raise ValueError("Upgrade readiness not confirmed")
        self.prune()
        shutil.rmtree(self.state / manifest["id"])
        print(f'Gitea {version} is ready. Rollback point: {manifest["id"]}. '
              "Check login, SSH clone/push, and search.")

    def init(self):
        self.settings()
        digest = self.pull_native_image(self.c["image"])
        for volume in (self.c["dataVolume"], self.c["configVolume"]):
            if volume in self.pod("volume", "ls", "--format", "{{.Name}}").splitlines():
                raise ValueError(f"Volume {volume} exists; use adopt.sh for an existing instance")
        self.c["image"] = digest
        self.write(self.config, self.c)
        for volume in (self.c["dataVolume"], self.c["configVolume"]):
            self.pod("volume", "create", volume)
            self.pod("run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "chown", "-v",
                     f"{volume}:/init", self.helper(), "1000:1000", "/init")
        self.compose_run("up", "-d")

    def adopt(self, container):
        if not container:
            raise ValueError("Specify the existing server container ID")
        server = self.pod_json("inspect", container)[0]
        for destination, key in (("/var/lib/gitea", "dataVolume"), ("/etc/gitea", "configVolume")):
            mount = next((mount for mount in server.get("Mounts", []) if
                          mount.get("Destination") == destination and mount.get("Type") == "volume"), None)
            if not mount:
                raise ValueError(f"Missing named volume at {destination}")
            self.c[key] = mount["Name"]
        self.c["project"] = server.get("Config", {}).get("Labels", {}).get("com.docker.compose.project", "")
        self.c["image"] = self.image_digest(self.pod_json("image", "inspect", server["Image"])[0])
        self.settings()
        self.inspect_server()
        self.write(self.config, self.c)
        print("Adopted existing volumes and image. Keep the original deployment stopped "
              "before recreating from this project.")


def main(argv=None):
    os.umask(0o077)
    for variable in ("HOME", "XDG_RUNTIME_DIR"):
        directory = os.environ.get(variable, "")
        if directory.startswith("/tmp/gitea-manager-"):
            Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "start", "stop", "backup", "list-backups", "configure-rclone"):
        actions.add_parser(action)
    actions.add_parser("adopt").add_argument("container")
    actions.add_parser("restore").add_argument("snapshot")
    upgrade_parser = actions.add_parser("upgrade")
    upgrade_parser.add_argument("version")
    upgrade_parser.add_argument("--reviewed", action="store_true")
    actions.add_parser("rollback").add_argument("--accept-data-loss", action="store_true")
    args = parser.parse_args(argv)
    manager = Manager(os.environ.get("GITEA_ROOT", os.getcwd()))
    manager.verify_rootless()
    if args.action == "init":
        manager.init()
    elif args.action == "adopt":
        manager.adopt(args.container)
    elif args.action in ("start", "stop"):
        manager.settings()
        manager.compose_run(*(("up", "-d") if args.action == "start" else ("stop",)))
    elif args.action == "backup":
        manager.settings()
        sidecar = manager.backup_container()
        if not sidecar:
            raise ValueError("Start the Compose backup service before taking a backup")
        print(manager.pod("exec", sidecar, "python3", "/opt/gitea/backup_service.py",
                          "--once", "--lock-held"), end="")
    elif args.action == "list-backups":
        print("\n".join(manager.list_backups()))
    elif args.action == "restore":
        manager.restore(args.snapshot)
    elif args.action == "upgrade":
        manager.upgrade(args.version, args.reviewed)
    elif args.action == "rollback":
        if not args.accept_data_loss:
            raise ValueError("Rollback discards changes since upgrade. Use rollback.sh "
                             "--accept-data-loss after checking migration status.")
        record = manager.rollback_record()
        if not record:
            raise ValueError("No recorded upgrade recovery point")
        manager.restore(record["id"], rollback=True)
    elif args.action == "configure-rclone":
        subprocess.run(["rclone", "--config", str(manager.rc), "config"], check=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Check Gitea status and pending backup state.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
