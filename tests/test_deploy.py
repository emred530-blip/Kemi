"""The server deployment package: units, launcher and reverse proxy.

These files are only exercised on a real server, which means a typo in them
surfaces as a service that will not start — hours after the change was made.
The launcher's output is checked against the real argument parser, so a flag
renamed in the CLI breaks a test here instead of a deployment there.
"""

import configparser
import os
import re
import shutil
import subprocess
import unittest

from kemi.cli import build_parser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
UNITS = ("kemi-node.service", "kemi-portal.service", "kemi-gateway.service")


def _read(*parts: str) -> str:
    with open(os.path.join(DEPLOY, *parts), encoding="utf-8") as fh:
        return fh.read()


def _launch(role: str, **env) -> list[str]:
    result = subprocess.run(
        [os.path.join(DEPLOY, "kemi-launch.sh"), role],
        env={**os.environ, "KEMI_BIN": "echo", **env},
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(f"kemi-launch {role} failed: {result.stderr}")
    return result.stdout.split()


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class LauncherTests(unittest.TestCase):
    """Every command line the units produce must parse as a real command."""

    def _parsed(self, role: str, **env):
        return build_parser().parse_args(_launch(role, **env))

    def test_default_settings_produce_valid_commands(self):
        self.assertEqual(self._parsed("node").func.__name__, "_cmd_node")
        self.assertEqual(self._parsed("portal").func.__name__, "_cmd_web")
        self.assertEqual(self._parsed("gateway").func.__name__, "_cmd_serve")

    def test_optional_settings_are_omitted_rather_than_passed_empty(self):
        # An empty KEMI_AI_MODEL must not become a bare --ai-model.
        args = self._parsed("node", KEMI_AI_MODEL="", KEMI_ADVERTISE="")
        self.assertIsNone(args.ai_model)
        self.assertIsNone(args.advertise_host)
        args = self._parsed("node", KEMI_AI_BACKEND="ollama",
                            KEMI_AI_MODEL="llama3.2",
                            KEMI_ADVERTISE="kemi.example.com")
        self.assertEqual(args.ai_backend, "ollama")
        self.assertEqual(args.ai_model, "llama3.2")
        self.assertEqual(args.advertise_host, "kemi.example.com")

    def test_peer_list_accepts_commas_and_spaces(self):
        for value in ("a.example:7700,b.example:7700", "a.example:7700 b.example:7700"):
            args = self._parsed("gateway", KEMI_PEERS=value)
            self.assertIn(("a.example", 7700), args.peer)
            self.assertIn(("b.example", 7700), args.peer)

    def test_portal_is_proxy_aware_and_bound_to_loopback(self):
        args = self._parsed("portal", KEMI_ADMIN_KEY="secret",
                            KEMI_FAUCET="20", KEMI_GUESTS_PER_IP="3")
        self.assertEqual(args.web_host, "127.0.0.1")   # nginx is the only way in
        self.assertTrue(args.trust_proxy)              # so forwarded IPs are real
        self.assertEqual(args.admin_key, "secret")
        self.assertEqual(args.faucet, 20.0)
        self.assertEqual(args.guests_per_ip, 3)

    def test_an_unknown_role_is_refused(self):
        result = subprocess.run(
            [os.path.join(DEPLOY, "kemi-launch.sh"), "definitely-not-a-role"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)


class UnitFileTests(unittest.TestCase):
    def test_units_are_well_formed_and_confined(self):
        for name in UNITS:
            with self.subTest(unit=name):
                cfg = configparser.ConfigParser(strict=False)
                cfg.optionxform = str
                cfg.read_string(_read("systemd", name))
                service = cfg["Service"]
                self.assertEqual(service["User"], "kemi")
                self.assertIn("kemi-launch", service["ExecStart"])
                self.assertEqual(service["EnvironmentFile"], "/etc/kemi/kemi.env")
                self.assertEqual(service["Restart"], "always")
                for guard in ("NoNewPrivileges", "ProtectHome", "PrivateTmp"):
                    self.assertEqual(service[guard], "yes", guard)
                self.assertEqual(service["ProtectSystem"], "strict")
                self.assertEqual(cfg["Install"]["WantedBy"], "multi-user.target")

    def test_each_service_keeps_its_own_writable_state_directory(self):
        homes = set()
        for name in UNITS:
            text = _read("systemd", name)
            line = [l for l in text.splitlines() if l.startswith("Environment=KEMI_HOME=")]
            self.assertTrue(line, f"{name} does not pin KEMI_HOME")
            home = line[0].split("=", 2)[2]
            homes.add(home)
            # ProtectSystem=strict makes the filesystem read-only, so the one
            # directory this service must write has to be named explicitly —
            # and it has to be the same one KEMI_HOME points at.
            paths = [l.split("=", 1)[1] for l in text.splitlines()
                     if l.startswith("ReadWritePaths=")]
            self.assertEqual(paths, [home], f"{name} cannot write its own state")
        # separate identities and ledgers, or the three would fight over one file
        self.assertEqual(len(homes), len(UNITS))

    def test_settings_template_covers_every_variable_the_launcher_reads(self):
        launcher = _read("kemi-launch.sh")
        template = _read("kemi.env.example")
        used = set(re.findall(r"\$\{(KEMI_[A-Z_]+)", launcher))
        self.assertGreater(len(used), 5)
        for name in used - {"KEMI_BIN"}:
            self.assertIn(f"{name}=", template, f"{name} is undocumented")


class ReverseProxyTests(unittest.TestCase):
    def setUp(self):
        self.conf = _read("nginx", "kemi.conf")

    def test_proxy_forwards_the_client_address(self):
        # --trust-proxy in the portal unit is only safe because of these.
        self.assertIn("X-Real-IP         $remote_addr", self.conf)
        self.assertIn("X-Forwarded-For   $proxy_add_x_forwarded_for", self.conf)

    def test_upstreams_match_the_defaults_in_the_settings_template(self):
        template = _read("kemi.env.example")
        for key, port in (("KEMI_PORTAL_PORT", "8090"), ("KEMI_GATEWAY_PORT", "11434")):
            self.assertIn(f"{key}={port}", template)
            self.assertIn(f"127.0.0.1:{port}", self.conf)

    def test_streaming_endpoint_is_unbuffered(self):
        v1 = self.conf.split("location /v1/")[1]
        self.assertIn("proxy_buffering off", v1)

    def test_the_console_is_rate_limited(self):
        admin = self.conf.split("location /admin")[1].split("location")[0]
        self.assertIn("limit_req zone=kemi_admin", admin)


if __name__ == "__main__":
    unittest.main()
