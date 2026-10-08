#!/usr/bin/env python3
"""Exercise the credential-free configuration wizard without external services."""
import contextlib
import configparser
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

    def run_manual_wizard(self, answers, *, language=None):
        output = io.StringIO()
        patches = [
            patch.object(wizard, "discover_repositories", return_value=[]),
            patch.object(wizard, "discover_lan", return_value=[]),
            patch.object(wizard, "discover_windows_lan", return_value=[]),
            patch.object(wizard, "discover_ports", return_value=[]),
            patch.object(wizard, "port_available", return_value=True),
            patch("builtins.input", side_effect=answers),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(output):
                result = wizard.run_wizard(self.config_path, ROOT, language=language)
        return result, output.getvalue()

    def test_manual_values_when_repository_lan_and_port_are_not_discovered(self):
        dispatch = self.secrets / "dispatch.token"
        ghcr = self.secrets / "ghcr.token"
        source_token = self.secrets / "app.token"
        result, output = self.run_manual_wizard([
            "1", str(self.control_dir), "owner/control", "", "1", "192.168.1.20", "",
            "", str(dispatch), str(ghcr), str(self.source_dir), "owner/app", "8080",
            str(source_token), "", "yes",
        ])

        self.assertTrue(result)
        self.assertTrue(self.config_path.exists())
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        value = config.load_config(self.config_path, ROOT)
        self.assertEqual(value.control, "owner/control")
        self.assertEqual(value.language, "en")
        self.assertEqual(value.lan_ip, "192.168.1.20")
        self.assertEqual(value.domain_suffix, "auto")
        self.assertEqual(value.sources[0].port, 8080)
        self.assertIn("No Ubuntu LAN address was found", output)
        self.assertIn("No application port found", output)
        self.assertIn("Recommended: auto uses <LAN IPv4>.sslip.io", output)
        self.assertIn("http://pm-r<repository_id>-pr<PR>.192.168.1.20.sslip.io:18080", output)
        self.assertIn("Review:", output)

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
            "2", str(self.control_dir), "2", "", "1", "2", "", "", str(self.secrets / "dispatch"),
            str(self.secrets / "ghcr"), str(self.source_dir), "2", "2", str(self.secrets / "app"),
            "", "yes",
        ]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(wizard, "discover_repositories",
                                              side_effect=repositories))
            stack.enter_context(patch.object(wizard, "discover_lan",
                                              return_value=["192.168.1.20", "10.0.0.5"]))
            stack.enter_context(patch.object(wizard, "discover_windows_lan", return_value=[]))
            stack.enter_context(patch.object(wizard, "discover_ports", return_value=[3000, 8080]))
            stack.enter_context(patch.object(wizard, "port_available", return_value=True))
            stack.enter_context(patch("builtins.input", side_effect=answers))
            with contextlib.redirect_stdout(output):
                result = wizard.run_wizard(self.config_path, ROOT)

        self.assertTrue(result)
        value = config.load_config(self.config_path, ROOT)
        self.assertEqual(value.control, "owner/control-b")
        self.assertEqual(value.language, "zh-CN")
        self.assertEqual(value.lan_ip, "10.0.0.5")
        self.assertEqual(value.sources[0].repository, "owner/app-b")
        self.assertEqual(value.sources[0].port, 8080)
        self.assertEqual(value.domain_suffix, "auto")
        self.assertIn("已自动获取局域网 IPv4 地址", output.getvalue())
        self.assertIn("确认摘要", output.getvalue())
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

    def test_language_choice_retries_and_accepts_aliases(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["invalid", "zh-CN"]), \
             contextlib.redirect_stdout(output):
            value = wizard.choose_language()

        self.assertEqual(value, "zh-CN")
        self.assertIn("Invalid choice", output.getvalue())
        self.assertIn("1. English", output.getvalue())
        self.assertIn("2. 中文", output.getvalue())

    def test_network_environment_defaults_aliases_retry_and_q(self):
        for release, expected in (("Linux", "ubuntu"), ("microsoft-standard-WSL2", "wsl")):
            with self.subTest(release=release), \
                 patch.object(wizard.platform, "release", return_value=release), \
                 patch("builtins.input", return_value=""):
                self.assertEqual(wizard.choose_network_environment(wizard.Prompts("en")), expected)

        with patch.object(wizard.platform, "release", return_value="Linux"), \
             patch("builtins.input", side_effect=["invalid", "WSL"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wizard.choose_network_environment(wizard.Prompts("en")), "wsl")
        self.assertIn("Invalid value", output.getvalue())

        with patch.object(wizard.platform, "release", return_value="microsoft-standard-WSL2"), \
             patch("builtins.input", return_value="Ubuntu"):
            self.assertEqual(wizard.choose_network_environment(wizard.Prompts("en")), "ubuntu")

        with patch.object(wizard.platform, "release", return_value="Linux"), \
             patch("builtins.input", side_effect=["q"]):
            with self.assertRaises(EOFError):
                wizard.choose_network_environment(wizard.Prompts("zh-CN"))

    def test_single_candidate_shows_actual_default_and_enter_selects_it(self):
        output = io.StringIO()
        prompts = []

        def accept_default(prompt):
            prompts.append(prompt)
            return ""

        with patch("builtins.input", side_effect=accept_default), \
             contextlib.redirect_stdout(output):
            value = wizard.choose(
                "PreviewMesh code repository (public)",
                ["flashrick/PreviewMesh"],
                config.validate_repo,
                language="zh-CN",
            )

        self.assertEqual(value, "flashrick/PreviewMesh")
        self.assertIn("1. flashrick/PreviewMesh", output.getvalue())
        self.assertEqual(
            prompts,
            ["PreviewMesh code repository (public) (编号或直接填写) [flashrick/PreviewMesh]: "],
        )

    def test_english_and_chinese_wizards_preserve_same_configuration_values(self):
        answers = [
            str(self.control_dir), "owner/control", "", "1", "192.168.1.20", "auto", "auto",
            str(self.secrets / "dispatch.token"), str(self.secrets / "ghcr.token"),
            str(self.source_dir), "owner/app", "8080", str(self.secrets / "app.token"),
            "no", "yes",
        ]
        snapshots = {}
        for language in ("en", "zh-CN"):
            with self.subTest(language=language):
                self.config_path = self.work / language / "setup.ini"
                result, _ = self.run_manual_wizard(answers, language=language)
                self.assertTrue(result)
                value = config.load_config(self.config_path, ROOT)
                ini = configparser.ConfigParser(interpolation=None)
                ini.read(self.config_path)
                self.assertEqual(set(ini.sections()), {"project", "network", "credentials", "source:app1"})
                self.assertEqual(set(ini["project"]), {
                    "control_repository", "control_directory", "public_repository", "language",
                })
                self.assertEqual(set(ini["network"]), {"lan_ip", "allowed_subnet", "domain_suffix"})
                self.assertEqual(set(ini["credentials"]), {"dispatch_token_file", "ghcr_token_file"})
                self.assertEqual(set(ini["source:app1"]), {"repository", "directory", "port", "token_file"})
                self.assertNotIn("environment", ini["network"])
                snapshots[language] = (
                    value.control,
                    value.control_dir,
                    value.public,
                    value.lan_ip,
                    value.subnet,
                    value.domain_suffix,
                    value.dispatch_file,
                    value.ghcr_file,
                    [(source.repository, source.directory, source.port, source.token_file)
                     for source in value.sources],
                )

        self.assertEqual(snapshots["en"], snapshots["zh-CN"])

    def test_existing_configuration_can_be_reviewed_and_reused(self):
        self.config_path.parent.mkdir(parents=True)
        original = self.config_text("8081").replace("language = auto", "language = zh-CN")
        self.config_path.write_text(original)
        before = config.load_config(self.config_path, ROOT)
        answers = ["1", "yes", *([""] * 14), "yes"]
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(wizard, "discover_repositories", side_effect=lambda directory: []))
            stack.enter_context(patch.object(wizard, "discover_lan", return_value=[]))
            stack.enter_context(patch.object(wizard, "discover_windows_lan", return_value=[]))
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
        self.assertEqual(after.language, "en")
        self.assertIn("Previously configured LAN IPv4", output.getvalue())
        self.assertIn("Existing configuration found", output.getvalue())

    def test_existing_configuration_cancel_does_not_change_file(self):
        self.config_path.parent.mkdir(parents=True)
        original = self.config_text()
        self.config_path.write_text(original)
        with patch("builtins.input", side_effect=["1", ""]):
            result = wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(result)
        self.assertEqual(self.config_path.read_text(), original)

    def test_final_decline_leaves_no_file_and_q_can_exit_for_retry(self):
        answers = [
            "1", str(self.control_dir), "owner/control", "", "1", "192.168.1.20", "",
            "", str(self.secrets / "dispatch.token"), str(self.secrets / "ghcr.token"),
            str(self.source_dir), "owner/app", "8080", str(self.secrets / "app.token"),
            "", "no",
        ]
        result, output = self.run_manual_wizard(answers)
        self.assertFalse(result)
        self.assertFalse(self.config_path.exists())
        self.assertIn("Cancelled", output)
        with patch("builtins.input", side_effect=["1", "q"]):
            with self.assertRaises(EOFError):
                wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(self.config_path.exists())

    def test_eof_cancels_before_creating_a_file(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(self.config_path.exists())

    def test_language_q_cancels_before_creating_a_file(self):
        with patch("builtins.input", side_effect=["q"]):
            with self.assertRaises(EOFError):
                wizard.run_wizard(self.config_path, ROOT)
        self.assertFalse(self.config_path.exists())

    def test_lan_discovery_filters_non_lan_and_container_addresses(self):
        addresses = [
            {"ifname": "eth0", "operstate": "UP", "addr_info": [
                {"local": "192.168.1.20", "scope": "global"},
                {"local": "8.8.8.8", "scope": "global"},
            ]},
            {"ifname": "docker0", "operstate": "UP", "addr_info": [{"local": "172.17.0.1", "scope": "global"}]},
            {"ifname": "lo", "operstate": "UP", "addr_info": [{"local": "10.0.0.1", "scope": "global"}]},
            {"ifname": "tun0", "operstate": "UP", "addr_info": [{"local": "10.0.0.2", "scope": "global"}]},
            {"ifname": "eth1", "operstate": "DOWN", "addr_info": [{"local": "10.0.0.5", "scope": "global"}]},
            {"ifname": "wlan0", "operstate": "UP", "addr_info": [{"local": "10.0.0.5", "scope": "global"}]},
        ]
        with patch.object(wizard.platform, "release", return_value="Linux"), \
             patch.object(wizard, "capture", return_value=json.dumps(addresses)):
            self.assertEqual(wizard.discover_lan(), ["10.0.0.5", "192.168.1.20"])

        with patch.object(wizard.platform, "release", return_value="microsoft-standard-WSL2"), \
             patch.object(wizard, "capture") as capture:
            self.assertEqual(wizard.discover_lan(), [])
        capture.assert_not_called()

    def test_windows_lan_discovery_parses_and_filters_addresses_without_running_powershell(self):
        payload = json.dumps([
            "192.168.1.20", "192.168.1.20", "10.0.0.5", "127.0.0.1",
            "8.8.8.8", "::1", "not-an-ip", 42,
        ])
        with patch.object(wizard, "capture", return_value=payload) as capture:
            values = wizard.discover_windows_lan()

        self.assertEqual(values, ["10.0.0.5", "192.168.1.20"])
        command = capture.call_args.args[0]
        self.assertEqual(capture.call_args.kwargs, {"timeout": 20})
        self.assertIn("Get-NetAdapter -Physical", command[-1])
        self.assertIn("SkipAsSource", command[-1])
        self.assertFalse(command[-1].lstrip().startswith("+"))

        with patch.object(wizard, "capture", return_value=json.dumps("192.168.1.20")):
            self.assertEqual(wizard.discover_windows_lan(), ["192.168.1.20"])
        for output in ("", "not-json", json.dumps({"IPAddress": "192.168.1.20"})):
            with self.subTest(output=output), patch.object(wizard, "capture", return_value=output):
                self.assertEqual(wizard.discover_windows_lan(), [])

    def test_wizard_uses_selected_environment_discovery_and_does_not_store_mode(self):
        answers = [
            str(self.control_dir), "owner/control", "", None, "", "auto", "auto",
            str(self.secrets / "dispatch.token"), str(self.secrets / "ghcr.token"),
            str(self.source_dir), "owner/app", "8080", str(self.secrets / "app.token"),
            "no", "yes",
        ]
        for environment, expected, discovery in (
                ("1", "192.168.1.20", "discover_lan"),
                ("2", "10.0.0.5", "discover_windows_lan")):
            with self.subTest(environment=environment):
                self.config_path = self.work / f"{environment}/setup.ini"
                values = list(answers)
                values[3] = environment
                output = io.StringIO()
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(wizard, "discover_repositories", return_value=[]))
                    ubuntu = stack.enter_context(patch.object(wizard, "discover_lan",
                                                               return_value=["192.168.1.20"]))
                    windows = stack.enter_context(patch.object(wizard, "discover_windows_lan",
                                                                return_value=["10.0.0.5"]))
                    stack.enter_context(patch.object(wizard, "discover_ports", return_value=[]))
                    stack.enter_context(patch.object(wizard, "port_available", return_value=True))
                    stack.enter_context(patch("builtins.input", side_effect=values))
                    with contextlib.redirect_stdout(output):
                        self.assertTrue(wizard.run_wizard(self.config_path, ROOT, language="en"))

                loaded = config.load_config(self.config_path, ROOT)
                self.assertEqual(loaded.lan_ip, expected)
                self.assertNotIn("environment", self.config_path.read_text())
                if discovery == "discover_lan":
                    ubuntu.assert_called_once_with()
                    windows.assert_not_called()
                else:
                    windows.assert_called_once_with()
                    ubuntu.assert_not_called()

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
            "1", str(self.control_dir), "owner/control", "", "1", "192.168.1.20", "",
            "preview.example.internal", str(self.secrets / "dispatch.token"), str(self.secrets / "ghcr.token"),
            str(self.source_dir), "owner/app", "8080", str(self.secrets / "app.token"),
            "", "yes",
        ])
        self.assertTrue(result)
        value = config.load_config(self.config_path, ROOT)
        self.assertEqual(value.domain_suffix, "preview.example.internal")
        self.assertEqual(value.suffix, "preview.example.internal")
        self.assertIn("Alternative: enter a custom DNS suffix", output)
        self.assertIn("http://pm-r<repository_id>-pr<PR>.preview.example.internal:18080", output)
        self.assertNotIn("TOPSECRET_TEST_VALUE", output)
        self.assertNotIn("TOPSECRET_TEST_VALUE", self.config_path.read_text())


if __name__ == "__main__":
    unittest.main()
