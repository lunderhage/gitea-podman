"""YAML migration must retain restored volume names, pins, and typed settings."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from settings_io import load_settings, save_settings, settings_path, compose_values
from manager import Manager


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.values = {"project": "gitea-test", "image": "docker.gitea.com/gitea@sha256:" + "a" * 64,
                       "dataVolume": "restored-data", "configVolume": "restored-config",
                       "httpPort": 3001, "sshPort": 2223, "sshDomain": "raspberrypi",
                       "sshListenPort": 2222, "keep": 14, "readySeconds": 900,
                       "backupEnabled": False, "remote": "gitea-crypt:test"}

    def test_yaml_round_trip_and_manager_write_preserve_types(self):
        filename = self.root / "settings.yaml"
        save_settings(filename, self.values)
        manager = Manager(self.root)
        manager.settings()
        self.assertEqual(manager.config, filename)
        self.assertEqual(manager.c, self.values)
        manager.c["dataVolume"] = "another-restored-volume"
        manager.write(manager.config, manager.c)
        result = load_settings(filename)
        self.assertIs(result["backupEnabled"], False)
        self.assertEqual(result["sshPort"], 2223)
        self.assertEqual(result["dataVolume"], "another-restored-volume")
        self.assertIn("project: gitea-test", filename.read_text())

    def test_yaml_takes_precedence_and_legacy_json_is_preserved(self):
        legacy = self.root / "settings.json"
        legacy.write_text(json.dumps(self.values))
        self.assertEqual(settings_path(self.root), legacy)
        save_settings(self.root / "settings.yaml", self.values)
        self.assertEqual(settings_path(self.root), self.root / "settings.yaml")
        self.assertEqual(json.loads(legacy.read_text()), self.values)

    def test_legacy_json_manager_updates_still_write_json(self):
        legacy = self.root / "settings.json"
        legacy.write_text(json.dumps(self.values))
        manager = Manager(self.root)
        manager.c["sshPort"] = 2224
        manager.write(manager.config, manager.c)
        self.assertEqual(json.loads(legacy.read_text())["sshPort"], 2224)

    def test_duplicate_keys_and_unsafe_tags_are_rejected(self):
        filename = self.root / "settings.yaml"
        filename.write_text("project: one\nproject: two\n")
        with self.assertRaises(ValueError):
            load_settings(filename)
        filename.write_text("!!python/object/apply:os.system ['echo unsafe']")
        with self.assertRaises(yaml.YAMLError):
            load_settings(filename)

    def test_two_yaml_files_are_ambiguous(self):
        for name in ("settings.yaml", "settings.yml"):
            save_settings(self.root / name, self.values)
        with self.assertRaises(ValueError):
            settings_path(self.root)

    def test_compose_exports_preserve_ssh_and_restore_volumes(self):
        values = compose_values(self.values)
        self.assertEqual(values[1:3], ("restored-data", "restored-config"))
        self.assertEqual(values[5], 2223)
        self.assertEqual(values[7:9], ("raspberrypi", 2222))
        with self.assertRaises(ValueError):
            compose_values(dict(self.values, sshPort="2223"))
