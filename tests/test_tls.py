"""TLS mode, safe rollback, certificate validation, and persistent cache coverage."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from settings_io import compose_environment, compose_files, compose_values
from tls_config import web_settings, readiness_version, LocalHTTPSConnection, LE_STAGING
from manager import Manager


def settings(**changes):
    values = {"project": "tls-test", "image": "docker.gitea.com/gitea:28.0.0-rootless",
              "dataVolume": "data", "configVolume": "config", "httpPort": 3000,
              "sshPort": 2222, "sshDomain": "raspberrypi", "keep": 14, "readySeconds": 1,
              "tlsEnabled": True, "publicHostname": "git.example.net", "acmeEmail": "admin@example.net",
              "acmeAcceptTos": True}
    values.update(changes)
    return values


class TLSTests(unittest.TestCase):
    def test_explicit_deployment_fields_are_required(self):
        invalid = ({"publicHostname": ""}, {"publicHostname": "https://git.example.net"},
                   {"publicHostname": "localhost"}, {"acmeEmail": ""}, {"acmeAcceptTos": False},
                   {"tlsEnabled": "true"}, {"httpsPort": 443}, {"httpRedirectPort": 8443},
                   {"httpsPort": 2222}, {"acmeUrl": "http://ca.example.net/directory"})
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                compose_values(settings(**change))

    def test_tls_and_legacy_environment_and_compose_selection(self):
        public = compose_environment(settings(), "/local/project")
        self.assertEqual(public["HTTP_PORT"], "8443")
        self.assertEqual(public["SSH_DOMAIN"], "git.example.net")
        self.assertEqual(public["GITEA_ROOT_URL"], "https://git.example.net/")
        self.assertEqual(public["ACME_DIRECTORY"], "/var/lib/gitea/https")
        self.assertEqual(public["COMPOSE_FILE"], "/local/project/compose.yaml:/local/project/compose.tls.yaml")
        self.assertEqual(len(public), 20)
        plain = compose_environment(settings(tlsEnabled=False), "/local/project")
        self.assertEqual(plain["HTTP_PORT"], "3000")
        self.assertEqual(plain["SSH_DOMAIN"], "raspberrypi")
        self.assertEqual(plain["GITEA_ROOT_URL"], "http://raspberrypi:3000/")
        self.assertEqual(plain["COMPOSE_FILE"], "/local/project/compose.yaml")
        self.assertEqual(plain["GITEA_PROTOCOL"], "http")

    def test_staging_certificates_do_not_pollute_production_cache(self):
        self.assertEqual(web_settings(settings(acmeUrl=LE_STAGING))["cache"], "/var/lib/gitea/https-staging")
        self.assertEqual(web_settings(settings())["cache"], "/var/lib/gitea/https")

    def test_management_and_source_exports_use_identical_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.yaml").write_text(yaml.safe_dump(settings()))
            (root / "compose.tls.yaml").write_text("services: {}")
            calls = []
            manager = Manager(root, lambda program, args, **options: calls.append((args, options)) or "")
            manager.settings()
            expected = compose_environment(manager.c, root)
            for key, value in expected.items():
                self.assertEqual(manager.env()[key], value)
            manager.compose_run("up", "-d")
            self.assertEqual(calls[0][0][:6], ["-p", "tls-test", "-f", str(root / "compose.yaml"),
                                             "-f", str(root / "compose.tls.yaml")])

    def test_tls_connection_keeps_trust_and_hostname_verification(self):
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        raw = Mock()
        connection = LocalHTTPSConnection("git.example.net", port=8443, timeout=10, context=context)
        with patch("tls_config.socket.create_connection", return_value=raw) as connect, \
             patch.object(context, "wrap_socket", return_value=Mock()) as wrap:
            connection.connect()
            connect.assert_called_once_with(("127.0.0.1", 8443), 10)
            wrap.assert_called_once_with(raw, server_hostname="git.example.net")

    def test_untrusted_certificate_is_not_accepted(self):
        raw = Mock()
        context = ssl.create_default_context()
        connection = LocalHTTPSConnection("git.example.net", port=8443, context=context)
        with patch("tls_config.socket.create_connection", return_value=raw), \
             patch.object(context, "wrap_socket", side_effect=ssl.SSLCertVerificationError("wrong hostname")):
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        raw.close.assert_called_once()

    def test_ready_reports_tls_error_and_never_disables_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(directory)
            manager.c = settings()
            output = io.StringIO()
            with redirect_stderr(output), patch("manager.time.monotonic", side_effect=[0, 0, 2]), \
                 patch("manager.time.sleep"), \
                 patch("manager.readiness_version", side_effect=ssl.SSLCertVerificationError("certificate expired")):
                self.assertFalse(manager.ready("28.0.0"))
            self.assertIn("certificate expired", output.getvalue())
            self.assertIn("hostname verified", output.getvalue())

    def test_plain_readiness_still_uses_local_http(self):
        response = io.BytesIO(b'{"version":"28.0.0"}')
        with patch("tls_config.urlopen", return_value=response) as request:
            self.assertEqual(readiness_version(settings(tlsEnabled=False)), "28.0.0")
            request.assert_called_once_with("http://127.0.0.1:3000/api/v1/version", timeout=10)

    def test_compose_rollback_resets_tls_flags_and_preserves_data_volume(self):
        root = Path(__file__).resolve().parents[1]
        base = yaml.safe_load((root / "compose.yaml").read_text())
        tls = yaml.safe_load((root / "compose.tls.yaml").read_text())
        plain_environment = base["services"]["server"]["environment"]
        self.assertEqual(plain_environment["GITEA__server__PROTOCOL"], "http")
        for name in ("ENABLE_ACME", "REDIRECT_OTHER_PORT", "ACME_ACCEPTTOS"):
            self.assertEqual(plain_environment["GITEA__server__" + name], "false")
            self.assertEqual(tls["services"]["server"]["environment"]["GITEA__server__" + name], "true")
        self.assertEqual(base["services"]["server"]["volumes"], ["gitea-data:/var/lib/gitea", "gitea-config:/etc/gitea"])
        self.assertEqual(tls["services"]["server"]["ports"], ["${HTTP_REDIRECT_PORT:-8080}:8080"])


if __name__ == "__main__":
    unittest.main()
