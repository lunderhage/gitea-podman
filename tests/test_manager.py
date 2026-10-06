"""Failure handling and snapshot round trips without a live Podman service."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import (Manager, release, validate_manifest, retained_snapshots,
                     stop_capture_restart, parse_ini, main)


SNAPSHOT = "20261005T030000Z-1234abcd"
DIGEST = "a" * 64
IMAGE = "docker.gitea.com/gitea@sha256:" + DIGEST


def manifest():
    return {"schema": 1, "id": SNAPSHOT, "image": IMAGE, "owner": "1000:1000",
            "version": "28.0.0", "hashes": dict.fromkeys(
                ("data.tar", "config.tar", "compose.yaml", "recovery.md"), DIGEST)}


def settings():
    return {"image": IMAGE, "project": "recovery", "dataVolume": "data",
            "configVolume": "config", "httpPort": 3000, "sshPort": 2222,
            "keep": 14, "readySeconds": 1, "remote": "crypt:test"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gitea-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stderr = redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def test_reject_floating_releases_and_malformed_manifests(self):
        for value in ("latest", "28-rootless", "28.0.0;touch /tmp/x", "", None):
            with self.subTest(version=value), self.assertRaises(ValueError):
                release(value)
        self.assertEqual(release("28.0.0"), "28.0.0")
        self.assertEqual(validate_manifest(manifest())["id"], SNAPSHOT)
        for changes in ({"image": "untrusted/image:latest"}, {"id": "../../escape"},
                        {"owner": "0:0;evil"}, {"hashes": {"../../escape": DIGEST}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_manifest(dict(manifest(), **changes))

    def test_capture_failure_attempts_restart(self):
        calls = []

        def capture():
            calls.append("capture")
            raise OSError("disk full")

        with self.assertRaisesRegex(OSError, "disk full"):
            stop_capture_restart(lambda: calls.append("stop"), capture,
                                 lambda: calls.append("start"))
        self.assertEqual(calls, ["stop", "capture", "start"])

    def test_do_not_capture_when_stop_fails(self):
        calls = []

        def stop():
            raise ValueError("unclean")

        with self.assertRaisesRegex(ValueError, "unclean"):
            stop_capture_restart(stop, lambda: calls.append("capture"),
                                 lambda: calls.append("start"))
        self.assertEqual(calls, [])

    def test_retain_newest_and_protect_rollback(self):
        self.assertEqual(retained_snapshots(["01", "02", "03", "04"], 2, "01"), ["02"])

    def test_parse_sqlite_and_storage_without_interpolation(self):
        ini = parse_ini("; comment\n[database]\nDB_TYPE=sqlite3\n"
                        "PATH='/var/lib/gitea/data/gitea.db'\n[storage]\nSTORAGE_TYPE=s3\n"
                        "[server]\nROOT_URL=https://example/%value\n")
        self.assertEqual(ini["database"]["DB_TYPE"], "sqlite3")
        self.assertEqual(ini["database"]["PATH"], "/var/lib/gitea/data/gitea.db")
        self.assertEqual(ini["storage"]["STORAGE_TYPE"], "s3")
        self.assertEqual(ini["server"]["ROOT_URL"], "https://example/%value")

    def test_failed_verification_never_completes_or_clears_pending(self):
        calls = []

        def command(program, arguments):
            calls.append(arguments)
            if "check" in arguments:
                raise OSError("remote corrupt")
            return ""

        manager = Manager(self.root, command)
        manager.remote = lambda: "crypt:test"
        (manager.state / SNAPSHOT).mkdir()
        manager.write(manager.state / "pending.json", {"id": SNAPSHOT})
        with self.assertRaisesRegex(OSError, "remote corrupt"):
            manager.upload(manifest())
        self.assertFalse(any("copyto" in args for args in calls))
        self.assertTrue((manager.state / "pending.json").exists())

    def test_crypt_defaults_are_accepted_and_insecure_options_rejected(self):
        cases = (({"type": "crypt"}, True),
                 ({"type": "crypt", "filename_encryption": "standard",
                   "directory_name_encryption": "true"}, True),
                 ({"type": "crypt", "filename_encryption": "off"}, False),
                 ({"type": "crypt", "filename_encryption": "obfuscate"}, False),
                 ({"type": "crypt", "directory_name_encryption": "false"}, False),
                 ({"type": "crypt", "directory_name_encryption": False}, False),
                 ({"type": "jottacloud"}, False))
        for options, accepted in cases:
            with self.subTest(options=options):
                def command(program, arguments):
                    return json.dumps({"crypt": options}) if "dump" in arguments else ""
                manager = Manager(self.root, command)
                manager.c = settings()
                if accepted:
                    self.assertEqual(manager.remote(), "crypt:test")
                else:
                    with self.assertRaisesRegex(ValueError, "Remote must use crypt"):
                        manager.remote()

    def test_refuse_rootful_engine(self):
        manager = Manager(self.root, lambda *_: json.dumps({"host": {"security": {"rootless": False}}}))
        with self.assertRaisesRegex(ValueError, "rootless"):
            manager.verify_rootless()

    def test_existing_deployment_refused_before_volume_changes(self):
        calls = []

        def command(program, arguments):
            calls.append(arguments)
            return "existing-server\n" if arguments[0] == "ps" else ""

        manager = Manager(self.root, command)
        manager.c = settings()
        manager.download = lambda _: (manifest(), self.root)
        manager.restore_image = lambda snapshot: snapshot["image"]
        with self.assertRaisesRegex(ValueError, "unused Compose project"):
            manager.restore(SNAPSHOT)
        self.assertFalse(any(args[0] == "volume" for args in calls))

    def upgrade_fixture(self, fail_upload=False, ready=True, architecture="arm64", host="arm64"):
        calls = []
        (self.root / "settings.json").write_text(json.dumps(settings()))

        def command(program, arguments, **options):
            calls.append(arguments)
            if arguments[0] == "info":
                return json.dumps({"host": {"arch": host}})
            if arguments[0] == "image":
                return json.dumps([{"Architecture": architecture,
                                    "RepoDigests": ["docker.gitea.com/gitea@sha256:" + "b" * 64]}])
            return ""

        manager = Manager(self.root, command)
        manager.remote = lambda: "crypt:test"
        manager.space = lambda: None
        manager.inspect_server = lambda: {"s": {"Id": "old"}, "image": IMAGE,
                                         "version": "27.0.0", "owner": "1000:1000"}

        def stop(_):
            (manager.state / "restart-container").write_text("old")
            calls.append(["stopped"])

        def upload(_):
            if fail_upload:
                raise OSError("network down")
            calls.append(["verified"])

        manager.stop = stop
        manager.capture = lambda *_: manifest()
        (manager.state / SNAPSHOT).mkdir()
        manager.upload = upload
        manager.compose_run = lambda *args: calls.append(["compose", *args])
        manager.ready = lambda _: ready
        manager.prune = lambda: None
        return manager, calls

    def test_upgrade_backup_failure_restarts_original_without_changing_settings(self):
        manager, calls = self.upgrade_fixture(fail_upload=True)
        with self.assertRaisesRegex(OSError, "network down"):
            manager.upgrade("28.0.0", True)
        self.assertIn(["start", "old"], calls)
        self.assertFalse(any(args[0] == "compose" for args in calls))
        self.assertEqual(manager.read(manager.config)["image"], IMAGE)

    def test_upgrade_timeout_leaves_migrations_and_preserves_rollback(self):
        manager, calls = self.upgrade_fixture(ready=False)
        with self.assertRaisesRegex(ValueError, "readiness"):
            manager.upgrade("28.0.0", True)
        self.assertEqual(manager.rollback_record()["id"], SNAPSHOT)
        self.assertFalse((manager.state / "restart-container").exists())
        self.assertFalse(any(args[0] in ("start", "stop") for args in calls))
        self.assertLess(calls.index(["verified"]), next(
            index for index, args in enumerate(calls) if args[0] == "compose"))

    def test_upgrade_requires_review_and_refuses_direct_downgrade(self):
        manager, calls = self.upgrade_fixture()
        with self.assertRaisesRegex(ValueError, "review"):
            manager.upgrade("28.0.0")
        with self.assertRaisesRegex(ValueError, "newer"):
            manager.upgrade("26.0.0", True)
        self.assertNotIn(["stopped"], calls)

    def test_architecture_mismatch_is_refused_before_downtime(self):
        manager, calls = self.upgrade_fixture(architecture="amd64")
        with self.assertRaisesRegex(ValueError, "architecture"):
            manager.upgrade("28.0.0", True)
        self.assertNotIn(["stopped"], calls)

    def test_x86_upgrade_selects_native_platform(self):
        manager, calls = self.upgrade_fixture(architecture="amd64", host="amd64")
        manager.upgrade("28.0.0", True)
        self.assertIn(["pull", "--platform", "linux/amd64",
                       "docker.gitea.com/gitea:28.0.0-rootless"], calls)

    def test_arm64_upgrade_selects_native_platform(self):
        manager, calls = self.upgrade_fixture()
        manager.upgrade("28.0.0", True)
        self.assertIn(["pull", "--platform", "linux/arm64",
                       "docker.gitea.com/gitea:28.0.0-rootless"], calls)

    def restore_image_fixture(self, host, version="28.0.0"):
        calls = []

        def command(program, arguments):
            calls.append(arguments)
            if arguments[0] == "info":
                return json.dumps({"host": {"arch": host}})
            if arguments[0] == "image":
                return json.dumps([{"Architecture": host, "RepoDigests": [IMAGE]}])
            if arguments[0] == "run":
                return f"Gitea version {version}\n"
            return ""

        return Manager(self.root, command), calls

    def test_cross_architecture_restore_selects_same_version_for_destination(self):
        for source, destination in (("amd64", "arm64"), ("arm64", "amd64")):
            with self.subTest(source=source, destination=destination):
                manager, calls = self.restore_image_fixture(destination)
                self.assertEqual(manager.restore_image(dict(manifest(), architecture=source)), IMAGE)
                self.assertIn(["pull", "--platform", f"linux/{destination}",
                               "docker.gitea.com/gitea:28.0.0-rootless"], calls)

    def test_same_architecture_restore_keeps_pinned_digest(self):
        manager, calls = self.restore_image_fixture("amd64")
        manager.restore_image(dict(manifest(), architecture="amd64"))
        self.assertIn(["pull", "--platform", "linux/amd64", IMAGE], calls)
        self.assertFalse(any("docker.gitea.com/gitea:28.0.0-rootless" in args for args in calls))

    def test_restore_refuses_different_gitea_version(self):
        manager, _ = self.restore_image_fixture("amd64", version="29.0.0")
        with self.assertRaisesRegex(ValueError, "Gitea version"):
            manager.restore_image(dict(manifest(), architecture="arm64"))

    def test_cli_help_needs_no_podman_service(self):
        with self.assertRaises(SystemExit) as result:
            main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("upgrade", self.output.getvalue())

    def test_settings_reject_boolean_retention_and_ambiguous_volumes(self):
        manager = Manager(self.root)
        for changes in ({"keep": True}, {"dataVolume": "data:/escape"},
                        {"configVolume": "data"}, {"readySeconds": 0}):
            manager.c = dict(settings(), **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                manager.settings()

    def test_archive_round_trip_and_corrupt_payload(self):
        data = self.root / "source-data"
        config = self.root / "source-config"
        cloud = self.root / "cloud"
        for directory in (data, config, cloud):
            directory.mkdir()
        (data / "database.db").write_text("sqlite fixture")
        (data / "repos").mkdir()
        (data / "repos" / "HEAD").write_text("ref: refs/heads/main\n")
        (config / "app.ini").write_text("[database]\nDB_TYPE=sqlite3")
        (self.root / "README.md").write_text("Recovery instructions")
        (self.root / "compose.yaml").write_text("services: {}")
        (self.root / "settings.json").write_text(json.dumps(settings()))
        volumes = {"data": data, "config": config}

        def remote_path(value):
            prefix = "crypt:test/"
            return cloud / value[len(prefix):] if value.startswith(prefix) else Path(value)

        def command(program, arguments, **options):
            if program == "rclone":
                action = arguments[6]
                if action == "lsf":
                    return "\n".join(directory.name + "/COMPLETE" for directory in cloud.iterdir()
                                     if (directory / "COMPLETE").exists())
                source = remote_path(arguments[7])
                destination = remote_path(arguments[8])
                if action == "copy":
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                elif action == "copyto":
                    shutil.copyfile(source, destination)
                elif action != "check":
                    raise AssertionError("Unhandled rclone command: " + action)
                return ""
            action = arguments[0]
            if action == "volume" and arguments[1] == "ls":
                return "\n".join(volumes)
            if action == "volume" and arguments[1] == "create":
                volumes[arguments[2]] = self.root / arguments[2]
                volumes[arguments[2]].mkdir()
                return ""
            if action == "run":
                executable = arguments[arguments.index("--entrypoint") + 1]
                if executable == "chown":
                    return ""
                self.assertEqual(executable, "tar")
                mounts = [arg for index, arg in enumerate(arguments) if arguments[index - 1] == "-v"]
                volume = mounts[0].split(":")[0]
                staging = Path(mounts[1].split(":")[0])
                if "-cpf" in arguments:
                    archive = staging / Path(arguments[-2]).name
                    with tarfile.open(archive, "w") as output:
                        output.add(volumes[volume], arcname=".")
                else:
                    archive = staging / Path(arguments[-1]).name
                    with tarfile.open(archive) as payload:
                        payload.extractall(volumes[volume], filter="data")
                return ""
            if action in ("ps", "pull", "start"):
                return ""
            raise AssertionError("Unhandled Podman command: " + " ".join(arguments))

        manager = Manager(self.root, command)
        manager.remote = lambda: "crypt:test"
        manager.space = lambda: None
        manager.stop = lambda _: None
        manager.inspect_server = lambda: {"s": {"Id": "old"}, "image": IMAGE,
                                         "version": "28.0.0", "owner": "1000:1000"}
        manager.ready = lambda _: True
        manager.restore_image = lambda snapshot: snapshot["image"]
        manager.compose_run = lambda *_: None
        snapshot = manager.backup()
        self.assertEqual(manager.list_backups(), [snapshot])
        self.assertFalse((manager.state / snapshot).exists())
        manager.restore(snapshot)
        restored = volumes[manager.c["dataVolume"]]
        self.assertEqual((restored / "database.db").read_text(), "sqlite fixture")
        self.assertEqual((restored / "repos" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((volumes[manager.c["configVolume"]] / "app.ini").read_text(),
                         "[database]\nDB_TYPE=sqlite3")
        with (cloud / snapshot / "data.tar").open("ab") as archive:
            archive.write(b"corruption")
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            manager.download(snapshot)


if __name__ == "__main__":
    unittest.main()
