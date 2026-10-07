"""Regression tests for the network-preserving backup sidecar and dump format."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import validate_manifest, Manager
from backup_service import DumpManager, backup_once
from dump_restore import restore_dump, local_path


class SidecarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.staging = self.root / "staging"
        environment = patch.dict(os.environ, {"GITEA_BACKUP_PATH": str(self.staging),
                                              "GITEA_BACKUP_VOLUME": "test-backups"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_schema_two_requires_dump_and_full_config_hashes(self):
        snapshot = {"schema": 2, "id": "20261006T030000Z-1234abcd",
                    "image": "docker.gitea.com/gitea@sha256:" + "a" * 64,
                    "version": "28.0.0", "owner": "1000:1000",
                    "hashes": dict.fromkeys(("gitea-dump.zip", "config.tar", "compose.yaml", "recovery.md"), "b" * 64)}
        self.assertEqual(validate_manifest(snapshot), snapshot)
        snapshot["hashes"].pop("gitea-dump.zip")
        with self.assertRaises(ValueError):
            validate_manifest(snapshot)

    def test_stop_error_attempts_restart_and_prevents_capture(self):
        calls = []
        def command(program, args):
            calls.append(args)
            if args[0] == "stop":
                raise OSError("network cleanup denied")
            return ""
        manager = DumpManager(self.root, command)
        manager.server = lambda: {"Id": "server-id", "State": {"Running": True}}
        with self.assertRaisesRegex(OSError, "network cleanup"):
            manager.stop({"Id": "server-id"})
        self.assertIn(["start", "server-id"], calls)
        self.assertFalse((manager.state / "restart-container").exists())

    def test_backup_locks_against_other_operations(self):
        import fcntl
        manager = DumpManager(self.root)
        with (self.root / "state" / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Another Gitea operation"):
                backup_once(manager)

    def test_dump_runs_matching_image_on_shared_volume_without_network(self):
        manager = DumpManager(self.root)
        manager.c = {"dataVolume": "data", "configVolume": "config"}
        (self.root / "compose.yaml").write_text("services: {}")
        (self.root / "README.md").write_text("Recovery instructions")
        info = {"owner": f"{os.getuid()}:{os.getgid()}", "image": "docker.gitea.com/gitea@sha256:" + "a" * 64,
                "version": "28.0.0", "architecture": "amd64", "binary": "/usr/local/bin/gitea"}
        calls = []
        def pod(*args):
            calls.append(args)
            if "dump" in args:
                output = args[args.index("--file") + 1]
                relative = Path(output).relative_to("/backup")
                with zipfile.ZipFile(self.staging / relative, "w") as archive:
                    archive.writestr("gitea-db.sql", "CREATE TABLE test(id INTEGER);")
            if "tar" in args:
                output = args[args.index("-cpf") + 1]
                (self.staging / Path(output).relative_to("/backup")).write_bytes(b"config fixture")
            return ""
        manager.pod = pod
        snapshot = manager.capture(info, "ordinary")
        dump = next(args for args in calls if "dump" in args)
        self.assertIn(info["image"], dump)
        self.assertIn("test-backups:/backup", dump)
        self.assertIn("none", dump)
        self.assertEqual(snapshot["schema"], 2)
        self.assertTrue((self.staging / snapshot["id"] / "gitea-dump.zip").exists())
        self.assertFalse(any(args[0] in ("stop", "start") for args in calls))

    def test_schema_two_restore_dispatches_to_python_dump_helper(self):
        calls = []
        manager = Manager(self.root, lambda program, args: calls.append(args) or "")
        manager.restore_payload({"schema": 2}, self.root, "data", "config")
        self.assertTrue(any("/opt/gitea/dump_restore.py" in args for args in calls))
        self.assertFalse(any("/staging/data.tar" in args for args in calls))

    def config(self):
        filename = self.root / "app.ini"
        filename.write_text("[server]\nAPP_DATA_PATH=/var/lib/gitea\n"
                            "[repository]\nROOT=/var/lib/gitea/git/repositories\n"
                            "[database]\nDB_TYPE=sqlite3\nPATH=/var/lib/gitea/data/gitea.db\n"
                            "[lfs]\nPATH=/var/lib/gitea/git/lfs\n"
                            "[attachment]\nPATH=/var/lib/gitea/data/attachments\n")
        return filename

    def test_dump_round_trip_sqlite_repos_lfs_and_ssh_identity(self):
        original = self.root / "original.db"
        with sqlite3.connect(original) as connection:
            connection.execute("CREATE TABLE users(name TEXT)")
            connection.execute("INSERT INTO users VALUES ('example')")
        archive_path = self.root / "dump.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(original, "data/data/gitea.db")
            archive.writestr("gitea-db.sql", "INVALID SQL: native database must be used")
            archive.writestr("repos/example.git/HEAD", "ref: refs/heads/main\n")
            archive.writestr("data/lfs/object", "lfs content")
            archive.writestr("data/attachments/photo", "attachment")
            archive.writestr("data/ssh/gitea.rsa", "ssh identity fixture")
            archive.writestr("data/https/git.example.net", "cached certificate and private key fixture")
            archive.writestr("data/https/acme_account", "cached ACME account key fixture")
        destination = self.root / "restored"
        restore_dump(archive_path, self.config(), destination)
        with sqlite3.connect(destination / "data/gitea.db") as connection:
            self.assertEqual(connection.execute("SELECT name FROM users").fetchall(), [("example",)])
        self.assertEqual((destination / "git/repositories/example.git/HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((destination / "git/lfs/object").read_text(), "lfs content")
        self.assertEqual((destination / "data/attachments/photo").read_text(), "attachment")
        self.assertEqual((destination / "ssh/gitea.rsa").read_text(), "ssh identity fixture")
        self.assertEqual((destination / "https/git.example.net").read_text(), "cached certificate and private key fixture")
        self.assertEqual((destination / "https/acme_account").read_text(), "cached ACME account key fixture")

    def test_sql_fallback_when_native_database_is_missing(self):
        archive_path = self.root / "dump.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("gitea-db.sql", "CREATE TABLE users(name TEXT); INSERT INTO users VALUES ('restored');")
        destination = self.root / "restored"
        restore_dump(archive_path, self.config(), destination)
        with sqlite3.connect(destination / "data/gitea.db") as connection:
            self.assertEqual(connection.execute("SELECT name FROM users").fetchone(), ("restored",))

    def test_dump_rejects_archive_traversal(self):
        archive_path = self.root / "dump.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("data/../../escape", "unsafe")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            restore_dump(archive_path, self.config(), self.root / "restored")
        with self.assertRaises(ValueError):
            local_path("/var/lib/gitea/../escape")


if __name__ == "__main__":
    unittest.main()
