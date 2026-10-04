#!/usr/bin/env python3
"""Exercise the credential-free configuration wizard without external services."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import setup_config as config
import setup_wizard as wizard


ROOT = Path(__file__).resolve().parents[1]


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="previewmesh-wizard-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.config_path = self.work / "config/setup.ini"
        self.control_dir = self.work / "control"
        self.source_dir = self.work / "source"
        self.secrets = self.work / "secrets"

    def config_text(self, port="8080"):
        return f"""[project]
control_repository = owner/control
control_directory = {self.control_dir}
public_repository = flashrick/PreviewMesh
language = auto

[network]
lan_ip = 192.168.1.20
allowed_subnet = auto

[credentials]
dispatch_token_file = {self.secrets / 'dispatch.token'}
ghcr_token_file = {self.secrets / 'ghcr.token'}

[source:app]
repository = owner/app
directory = {self.source_dir}
port = {port}
token_file = {self.secrets / 'app.token'}
"""

    def run_manual_wizard(self, answers):
        output = io.StringIO()
        patches = [
            patch.object(wizard, "discover_repositories", return_value=[]),
            patch.object(wizard, "discover_lan", return_value=[]),
            patch.object(wizard, "discover_ports", return_value=[]),
            patch.object(wizard, "port_available", return_value=True),
            patch("builtins.input", side_effect=answers),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(output):
                result = wizard.run_wizard(self.config_path, ROOT)
        return result, output.getvalue()

    def test_manual_values_when_repository_lan_and_port_are_not_discovered(self):
        dispatch = self.secrets / "dispatch.token"
        ghcr = self.secrets / "ghcr.token"
        source_token = self.secrets / "app.token"
        result, output = self.run_manual_wizard([
            str(self.control_dir), "owner/control", "", "", "192.168.1.20", "",
            str(dispatch), str(ghcr), str(self.source_dir), "owner/app", "8080",
            str(source_token), "", "yes",
        ])

        self.assertTrue(result)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        value = config.load_config(self.config_path, ROOT)
        self.assertEqual(value.control, "owner/control")
        self.assertEqual(value.lan_ip, "192.168.1.20")
        self.assertEqual(value.sources[0].port, 8080)
        self.assertIn("No application port found", output)
        self.assertIn("Review /", output)

    def test_multiple_candidates_are_explicitly_selected(self):
        output = io.StringIO()
        def repositories(directory):
            directory = Path(directory).resolve()
            if directory == self.control_dir.resolve():
                return ["owner/control-a", "owner/control-b"]
            if directory == Path.cwd().resolve():
                return []
            return ["owner/app-a", "owner/app-b"]
        answers = [
            str(self.control_dir), "2", "", "", "2", "", str(self.secrets / "dispatch"),
            str(self.secrets / "ghcr"), str(self.source_dir), "2", "2", str(self.secrets / "app"),
            "", "yes",
        ]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(wizard, "discover_repositories",
                                              side_effect=repositories))
            stack.enter_context(patch.object(wizard, "discover_lan",
                                              return_value=["192.168.1.20", "10.0.0.5"]))
            stack.enter_context(patch.object(wizard, "discover_ports", return_value=[3000, 8080]))
            stack.enter_context(patch.object(wizard, "port_available", return_value=True))
            stack.enter_context(patch("builtins.input", side_effect=answers))
            with contextlib.redirect_stdout(output):
                result = wizard.run_wizard(self.config_path, ROOT)

        self.assertTrue(result)
        value = config.load_config(self.config_path, ROOT)
        self.assertEqual(value.control, "owner/control-b")
        self.assertEqual(value.lan_ip, "10.0.0.5")
        self.assertEqual(value.sources[0].repository, "owner/app-b")
        self.assertEqual(value.sources[0].port, 8080)
        self.assertIn("1. owner/control-a", output.getvalue())
        self.assertIn("2. owner/control-b", output.getvalue())

    def test_occupied_port_requires_confirmation_or_manual_retry(self):
        output = io.StringIO()
        with patch.object(wizard, "discover_ports", return_value=[8080]), \
             patch.object(wizard, "port_available", side_effect=[False, False, True]), \
             patch("builtins.input", side_effect=["", "", "8081"]), \
             contextlib.redirect_stdout(output):
            value = wizard.application_port(self.source_dir)

        self.assertEqual(value, "8081")
        self.assertIn("occupied", output.getvalue())

    def test_existing_configuration_can_be_reviewed_and_reused(self):
        self.config_path.parent.mkdir(parents=True)
        original = self.config_text("8081")
        self.config_path.write_text(original)
        before = config.load_config(self.config_path, ROOT)
        answers = ["yes", *([""] * 13), "yes"]
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(wizard, "discover_repositories", side_effect=lambda directory: []))
            stack.enter_context(patch.object(wizard, "discover_lan", return_value=[]))
            stack.enter_context(patch.object(wizard, "discover_ports", return_value=[]))
            stack.enter_context(patch.object(wizard, "port_available", return_value=True))
            stack.enter_context(patch("builtins.input", side_effect=answers))
            with contextlib.redirect_stdout(output):
                result = wizard.run_wizard(self.config_path, ROOT)

        self.assertTrue(result)
        after = config.load_config(self.config_path, ROOT)
        self.assertEqual(after.control, before.control)
        self.assertEqual(after.control_dir, before.control_dir)
        self.assertEqual(after.lan_ip, before.lan_ip)
        self.assertEqual(after.sources[0].repository, before.sources[0].repository)
        self.assertEqual(after.sources[0].port, before.sources[0].port)
        self.assertIn("Existing configuration found", output.getvalue())

    def test_existing_configuration_cancel_does_not_change_file(self):
        self.config_path.parent.mkdir(parents=True)
        original = self.config_text()
        self.config_path.write_text(original)
        with patch("builtins.input", return_value=""):
            result = wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(result)
        self.assertEqual(self.config_path.read_text(), original)

    def test_final_decline_leaves_no_file_and_q_can_exit_for_retry(self):
        answers = [
            str(self.control_dir), "owner/control", "", "", "192.168.1.20", "",
            str(self.secrets / "dispatch.token"), str(self.secrets / "ghcr.token"),
            str(self.source_dir), "owner/app", "8080", str(self.secrets / "app.token"),
            "", "no",
        ]
        result, output = self.run_manual_wizard(answers)
        self.assertFalse(result)
        self.assertFalse(self.config_path.exists())
        self.assertIn("Cancelled", output)
        with patch("builtins.input", side_effect=["q"]):
            with self.assertRaises(EOFError):
                wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(self.config_path.exists())

    def test_eof_cancels_before_creating_a_file(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(self.config_path.exists())

    def test_lan_discovery_filters_non_lan_and_container_addresses(self):
        addresses = [
            {"ifname": "eth0", "addr_info": [
                {"local": "192.168.1.20", "scope": "global"},
                {"local": "8.8.8.8", "scope": "global"},
            ]},
            {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1", "scope": "global"}]},
            {"ifname": "lo", "addr_info": [{"local": "10.0.0.1", "scope": "global"}]},
            {"ifname": "eth1", "addr_info": [{"local": "10.0.0.5", "scope": "global"}]},
        ]
        with patch.object(wizard.platform, "release", return_value="Linux"), \
             patch.object(wizard, "capture", return_value=json.dumps(addresses)):
            self.assertEqual(wizard.discover_lan(), ["10.0.0.5", "192.168.1.20"])

        with patch.object(wizard.platform, "release", return_value="microsoft-standard-WSL2"), \
             patch.object(wizard, "capture") as capture:
            self.assertEqual(wizard.discover_lan(), [])
        capture.assert_not_called()

    def test_dockerfile_port_discovery_handles_multiple_tcp_ports_and_ignores_udp(self):
        self.source_dir.mkdir()
        (self.source_dir / "Dockerfile").write_text(
            "EXPOSE 8080/tcp 8443/udp 3000 8080/tcp\nEXPOSE 9090/udp 9000\n")
        self.assertEqual(wizard.discover_ports(self.source_dir), [8080, 3000, 9000])

    def test_remote_credentials_are_ignored_and_never_echoed(self):
        secret = "ghp_TEST_ONLY_SECRET"
        remotes = (f"origin https://github.com/owner/public.git (fetch)\n"
                   f"private https://user:{secret}@github.com/owner/private.git (fetch)\n"
                   "ssh git@github.com:team/ssh-repo.git (fetch)\n")
        with patch.object(wizard, "capture", return_value=remotes):
            values = wizard.discover_repositories(self.source_dir)
        self.assertEqual(values, ["owner/public", "team/ssh-repo"])
        self.assertNotIn(secret, " ".join(values))

        output = io.StringIO()
        with patch("builtins.input", side_effect=[secret, "q"]), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(EOFError):
                wizard.ask("Repository")
        self.assertNotIn(secret, output.getvalue())

    def test_token_files_are_references_only(self):
        self.secrets.mkdir()
        for name in ("dispatch.token", "ghcr.token", "app.token"):
            (self.secrets / name).write_text("TOPSECRET_TEST_VALUE")
        result, output = self.run_manual_wizard([
            str(self.control_dir), "owner/control", "", "", "192.168.1.20", "",
            str(self.secrets / "dispatch.token"), str(self.secrets / "ghcr.token"),
            str(self.source_dir), "owner/app", "8080", str(self.secrets / "app.token"),
            "", "yes",
        ])
        self.assertTrue(result)
        self.assertNotIn("TOPSECRET_TEST_VALUE", output)
        self.assertNotIn("TOPSECRET_TEST_VALUE", self.config_path.read_text())


if __name__ == "__main__":
    unittest.main()
