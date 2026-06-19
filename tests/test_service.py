"""T14/T15 — service unit generation and Docker artefacts."""

import sys
import unittest
from pathlib import Path


class ServiceUnitTests(unittest.TestCase):
    def test_systemd_unit_contents(self):
        from kemi.service import systemd_unit

        unit = systemd_unit(["--peer", "1.2.3.4:7700", "--price", "0.5"])
        self.assertIn("[Service]", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("node --provide", unit)
        self.assertIn("--peer 1.2.3.4:7700", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_launchd_plist_contents(self):
        from kemi.service import launchd_plist

        plist = launchd_plist(["--price", "2.0"])
        self.assertIn("com.kemi.node", plist)
        self.assertIn("<key>KeepAlive</key>", plist)
        self.assertIn("--provide", plist)
        self.assertIn("2.0", plist)

    def test_install_dry_run_picks_platform(self):
        from kemi.service import install

        if sys.platform.startswith("linux"):
            path, content, hint = install([], write=False)
            self.assertTrue(str(path).endswith("kemi.service"))
            self.assertIn("systemctl", hint)
            self.assertIn("[Service]", content)
        elif sys.platform == "darwin":
            path, content, hint = install([], write=False)
            self.assertTrue(str(path).endswith(".plist"))
            self.assertIn("launchctl", hint)
        else:
            self.skipTest("unsupported platform")

    def test_cli_service_command_exists(self):
        from kemi.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["service", "--dry-run"])
        self.assertEqual(args.func.__name__, "_cmd_service")
        args = parser.parse_args(["servis", "--dry-run"])
        self.assertEqual(args.func.__name__, "_cmd_service")


class DockerArtefactTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_dockerfile_present_and_sane(self):
        text = (self.ROOT / "Dockerfile").read_text()
        self.assertIn("FROM python:3.12-slim", text)
        self.assertIn("pip install", text)
        self.assertIn("7700/udp", text)  # the DHT needs UDP exposed

    def test_compose_defines_a_fleet(self):
        text = (self.ROOT / "docker-compose.yml").read_text()
        self.assertIn("bootstrap:", text)
        self.assertIn("provider:", text)
        self.assertIn("--peer bootstrap:7700", text)


if __name__ == "__main__":
    unittest.main()
