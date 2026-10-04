#!/usr/bin/env python3
"""Offline installer checks. Never access GitHub, sudo, a cluster, or Windows settings."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import setup as installer
import setup_config as config
import setup_downloads as downloads
import setup_maintenance as maintenance

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="previewmesh-installer-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.template = (ROOT / "config/setup.example.ini").read_text()
        self.config_path = self.work / "setup.ini"
        self.text = (self.template.replace("YOUR_GITHUB_OWNER/previewmesh-control", "owner/control")
                     .replace("YOUR_GITHUB_OWNER/your-application", "owner/app")
                     .replace("YOUR_LAN_IPV4", "192.168.1.20")
                     .replace("~/workspace/previewmesh-control", str(self.work / "control"))
                     .replace("~/workspace/your-application", str(self.work / "source"))
                     .replace("~/.config/previewmesh/secrets", str(self.work / "secrets")))
        self.config_path.write_text(self.text)
        self.c = config.load_config(self.config_path, ROOT)

    def instance(self):
        with patch.object(Path, "home", return_value=self.work):
            return installer.Installer(self.c, "check")

    def test_config_defaults_and_multiple_sources(self):
        self.assertEqual(self.c.domain_suffix, "auto")
        self.assertEqual(self.c.suffix, "192.168.1.20.sslip.io")
        custom_path = self.work / "custom.ini"
        custom_path.write_text(self.text.replace("domain_suffix = auto", "domain_suffix = preview.example.internal"))
        custom = config.load_config(custom_path, ROOT)
        self.assertEqual(custom.domain_suffix, "preview.example.internal")
        self.assertEqual(custom.suffix, "preview.example.internal")
        self.config_path.write_text(self.text + "\n[source:two]\nrepository=team/second\ndirectory=second\nport=3000\ntoken_file=secrets/two.token\n")
        value = config.load_config(self.config_path, ROOT)
        self.assertEqual(len(value.sources), 2)
        self.assertEqual(value.sources[1].directory, self.work / "second")

    def test_domain_suffix_rejects_ambiguous_or_unsafe_values(self):
        self.assertEqual(config.validate_domain_suffix("auto"), "auto")
        for suffix in ("", "Preview.example.internal", "https://example.internal",
                       "example..internal", "-example.internal", "example-.internal",
                       "a" * 200):
            with self.subTest(suffix=suffix), self.assertRaises(config.SetupError):
                config.validate_domain_suffix(suffix)

    def test_invalid_inputs_before_side_effects(self):
        changes = [
            ("owner/control", "owner/app"), ("port = 8080", "port = 70000"),
            ("192.168.1.20", "127.0.0.1"), ("192.168.1.20", "8.8.8.8"),
            ("allowed_subnet = auto", "allowed_subnet = 10.0.0.0/8"),
            ("language = auto", "language = made-up"),
            (str(self.work / "source"), str(self.work / "control/nested")),
            ("port = 8080", "potr = 8080"),
            (str(self.work / "secrets/app.token"), str(self.work / "control/token")),
        ]
        for before, after in changes:
            self.config_path.write_text(self.text.replace(before, after))
            with self.subTest(after=after), self.assertRaises(config.SetupError):
                config.load_config(self.config_path, ROOT)
        self.assertFalse((self.work / "control").exists())

    def test_init_does_not_overwrite(self):
        destination = self.work / "new/setup.ini"
        command = ["bash", str(ROOT / "scripts/setup.sh"), "init", "--template", "--config", str(destination)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        original = destination.read_text()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(destination.read_text(), original)

    def test_tokens_must_be_private_regular_files(self):
        token = self.work / "secret.token"
        config.atomic_write(token, "synthetic-secret-value\n")
        self.assertEqual(config.read_token(token), "synthetic-secret-value")
        token.chmod(0o644)
        with self.assertRaises(config.SetupError):
            config.read_token(token)
        token.chmod(0o600)
        link = self.work / "link"
        link.symlink_to(token)
        with self.assertRaises(config.SetupError):
            config.read_token(link)
        (self.work / ".git").mkdir()
        (self.work / ".git/HEAD").write_text("ref: refs/heads/main\n")
        with self.assertRaises(config.SetupError):
            config.read_token(token)

    def test_registry_merge_preserves_unrelated_and_names(self):
        original = [{"repository_id": "12", "source_repository": "owner/app", "port": 80, "source_secret": "ESTABLISHED"},
                    {"repository_id": "13", "source_repository": "team/other", "port": 3000, "source_secret": "OTHER"}]
        desired = [{"repository_id": "12", "source_repository": "owner/app", "port": 8080, "source_secret": "SOURCE_REPO_12"}]
        merged = config.merge_registry(original, desired)
        self.assertEqual(merged[0]["source_secret"], "ESTABLISHED")
        self.assertEqual(merged[0]["port"], 8080)
        self.assertEqual(merged[1], original[1])
        self.assertEqual(original[0]["port"], 80)
        with self.assertRaises(config.SetupError):
            config.merge_registry(original, [dict(desired[0], repository_id="99")])
        with self.assertRaises(config.SetupError):
            config.merge_registry([dict(original[0], source_secret="GHCR_READ_TOKEN")], [])

    def test_generated_source_has_no_git_side_effects_and_conflict_is_preserved(self):
        app = self.instance()
        # Any external command, including git, is a failure in this stage.
        app.run = lambda *args, **kwargs: self.fail("source stage must not run commands")
        with contextlib.redirect_stdout(io.StringIO()):
            app.sources()
            app.sources()
        target = self.c.sources[0].directory / ".github/workflows/previewmesh-notify.yml"
        text = target.read_text()
        self.assertIn("PREVIEWMESH_CONTROL_OWNER: owner", text)
        self.assertIn("PREVIEWMESH_CONTROL_REPOSITORY: control", text)
        self.assertNotIn("OWNER: YOUR_", text)
        self.assertNotIn("REPOSITORY: YOUR_", text)
        target.write_text("user's workflow\n")
        with self.assertRaises(config.SetupError):
            app.sources()
        self.assertEqual(target.read_text(), "user's workflow\n")

    def test_managed_file_resume_and_user_edits(self):
        app = self.instance()
        target = self.work / "managed.conf"
        self.assertTrue(app.managed(target, "first"))
        self.assertFalse(app.managed(target, "first"))
        self.assertTrue(app.managed(target, "second"))
        resumed = self.instance()
        self.assertFalse(resumed.managed(target, "second"))
        target.write_text("user modification")
        with self.assertRaises(config.SetupError):
            resumed.managed(target, "third")
        self.assertEqual(target.read_text(), "user modification")

    def test_failure_does_not_mark_stage_completed(self):
        app = self.instance()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(config.SetupError):
            app.step(1, "Step", "步骤", lambda: (_ for _ in ()).throw(config.SetupError("failed")))
        self.assertNotIn("1", app.state.get("completed", {}))

    def test_secrets_never_in_log_or_verbose_output(self):
        app = self.instance()
        app.command = "install"
        app.verbose = True
        app.home.mkdir(parents=True)
        app.secrets = ["synthetic-sensitive-value"]
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "synthetic-sensitive-value", "github_pat_example")
        with patch.object(subprocess, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(config.SetupError) as error:
                app.run(["gh", "secret", "set", "EXAMPLE"], input=app.secrets[0])
        text = output.getvalue() + str(error.exception) + app.log_path.read_text()
        self.assertNotIn("synthetic-sensitive-value", text)
        self.assertNotIn("github_pat_example", text)

    def test_secret_routes_and_upload_failure(self):
        app = self.instance()
        app.registrations = [{"source_secret": "SOURCE_APP"}]
        for path in (self.c.sources[0].token_file, self.c.dispatch_file, self.c.ghcr_file):
            config.atomic_write(path, path.name + "-synthetic")
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs.get("input")))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        app.run = run
        with contextlib.redirect_stdout(io.StringIO()):
            app.secrets_step()
        uploads = [(args[3], args[5], value) for args, value in calls if args[:3] == ["gh", "secret", "set"]]
        self.assertEqual(uploads, [("SOURCE_APP", "owner/control", "app.token-synthetic"),
                                  ("PREVIEWMESH_DISPATCH_TOKEN", "owner/app", "dispatch.token-synthetic"),
                                  ("GHCR_READ_TOKEN", "owner/control", "ghcr.token-synthetic")])
        def failing(args, **kwargs):
            if args[:3] == ["gh", "secret", "set"]:
                raise config.SetupError("upload failed")
            return subprocess.CompletedProcess(args, 0, "{}", "")
        app.run = failing
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(config.SetupError):
            app.secrets_step()

    def test_dns_failure_has_no_hosts_fallback(self):
        app = self.instance()
        with patch.object(installer.socket, "getaddrinfo", side_effect=OSError("blocked")), self.assertRaises(config.SetupError) as error:
            app.check_dns()
        message = str(error.exception)
        self.assertIn("previewmesh-check.192.168.1.20.sslip.io", message)
        self.assertIn("hosts", message)
        self.assertIn("access.md", message)
        self.assertFalse(app.state_path.exists())

    def test_custom_suffix_dns_probe_requires_configured_lan_ip(self):
        app = self.instance()
        app.c.domain_suffix = "preview.example.internal"
        queries = []

        def resolve(name, port, family, socktype):
            queries.append((name, port, family, socktype))
            return [(family, socktype, 6, "", (app.c.lan_ip, port))]

        with patch.object(installer.socket, "getaddrinfo", side_effect=resolve):
            app.check_dns()
        self.assertEqual(queries, [(
            "previewmesh-check.preview.example.internal", 18080,
            installer.socket.AF_INET, installer.socket.SOCK_STREAM,
        )])

        def resolve_wrong_ip(name, port, family, socktype):
            return [(family, socktype, 6, "", ("192.168.1.21", port))]

        with patch.object(installer.socket, "getaddrinfo", side_effect=resolve_wrong_ip), self.assertRaises(config.SetupError):
            app.check_dns()

    def test_resume_enable_publishes_current_domain_suffix(self):
        app = self.instance()
        app.c.domain_suffix = "preview.example.internal"
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        app.run = run
        app.api = lambda *args, **kwargs: {"enabled": True}
        app.doctor = lambda: None
        app.enable()
        self.assertIn(["gh", "variable", "set", "PREVIEWMESH_DOMAIN_SUFFIX",
                       "--repo", "owner/control", "--body", "preview.example.internal"], calls)
        self.assertIn(["gh", "variable", "set", "PREVIEWMESH_ENABLED",
                       "--repo", "owner/control", "--body", "true"], calls)

    def test_renewal_timing_and_atomic_failure(self):
        self.assertFalse(maintenance.needs_renewal(10000, 1000, now=2000))
        self.assertTrue(maintenance.needs_renewal(10000, 1000, now=7500))
        self.assertTrue(maintenance.needs_renewal(10000, 1000, now=11000))
        target = self.work / "runner.json"
        target.write_text("old credential")
        def permissions(args, accepted):
            if "clusterroles" in args:
                return subprocess.CompletedProcess(args, 1, "no\n", "")
            return subprocess.CompletedProcess(args, 0, "yes\n", "")
        with patch.object(maintenance, "capture", side_effect=permissions):
            maintenance.replace_credential(target, {"users": ["synthetic"]}, ["fake-kubectl"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        old = target.read_text()
        with patch.object(maintenance, "capture", side_effect=RuntimeError("offline")), self.assertRaises(RuntimeError):
            maintenance.replace_credential(target, {"users": ["new"]}, ["fake-kubectl"])
        self.assertEqual(target.read_text(), old)
        self.assertFalse(list(self.work.glob(".runner-*")))
        with patch.object(maintenance, "capture", return_value=subprocess.CompletedProcess([], 0, "yes\n", "")), self.assertRaises(RuntimeError):
            maintenance.replace_credential(target, {"users": ["overprivileged"]}, ["fake-kubectl"])
        self.assertEqual(target.read_text(), old)

    def test_checked_download_rejects_corruption_and_traversal(self):
        for filename, checksum_ok in (("safe", False), ("../outside", True)):
            blob = io.BytesIO()
            with tarfile.open(fileobj=blob, mode="w:gz") as archive:
                member = tarfile.TarInfo(filename)
                member.size = 3
                archive.addfile(member, io.BytesIO(b"abc"))
            data = blob.getvalue()
            checksum = downloads.hashlib.sha256(data).hexdigest() if checksum_ok else "0" * 64
            destination = self.work / ("extract-" + str(checksum_ok))
            destination.mkdir()
            with patch.object(downloads, "fetch", return_value=data), self.assertRaises(config.SetupError):
                downloads.download_archive("https://example.invalid/tool.tar.gz", checksum, destination)
        self.assertFalse((self.work / "outside").exists())

    def test_busy_runner_restart_survives_retry(self):
        app = self.instance()
        app.control_id = "42"
        directory = app.home / "runner"
        directory.mkdir(parents=True)
        (directory / ".runner").write_text(json.dumps({"gitHubUrl": "https://github.com/owner/control",
                                                      "agentName": "existing", "agentId": 2}))
        (directory / ".service").write_text("actions.runner.owner-control.existing.service")
        runner = {"id": 2, "name": "existing", "busy": True, "status": "online",
                  "labels": [{"name": name} for name in ("Linux", "X64", "self-hosted", "previewmesh")]}
        app.all_runners = lambda: [runner]
        app.managed = lambda *args, **kwargs: True
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        app.run = run
        with self.assertRaises(config.SetupError):
            app.runner()
        self.assertTrue(app.state["runner_restart_pending"])
        self.assertFalse(any("restart" in call for call in calls))
        app.managed = lambda *args, **kwargs: False
        runner["busy"] = False
        app.runner()
        self.assertTrue(any("restart" in call for call in calls))
        self.assertFalse(app.state["runner_restart_pending"])

    def test_partial_control_clone_recovery_does_not_change_source_origin(self):
        app = self.instance()
        self.c.control_dir.mkdir()
        (self.c.control_dir / "marker").touch()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[-2:] == ["get-url", "origin"] and len(calls) == 1:
                return subprocess.CompletedProcess(args, 2, "", "missing")
            if args[-1] == "upstream":
                return subprocess.CompletedProcess(args, 0, "https://github.com/flashrick/PreviewMesh.git", "")
            return subprocess.CompletedProcess(args, 0, "https://github.com/owner/control.git", "")
        app.run = run
        app.checkout(self.c.control_dir, self.c.control)
        self.assertTrue(any("add" in call and "origin" in call for call in calls))
        self.assertTrue(all(str(self.c.sources[0].directory) not in map(str, call) for call in calls))

    def test_firewall_only_replaces_its_own_chain(self):
        rules = maintenance.firewall_rules("192.168.1.20", "192.168.1.0/24")
        self.assertEqual(rules.splitlines()[0], "*filter")
        self.assertEqual(rules.splitlines()[-1], "COMMIT")
        self.assertIn("-F PREVIEWMESH-IN", rules)
        self.assertNotIn("-F INPUT", rules)
        self.assertIn("-i lo -j ACCEPT", rules)
        self.assertIn("-s 192.168.1.0/24 -d 192.168.1.20 -j ACCEPT", rules)
        self.assertIn("previewmesh-managed -j DROP", rules)
        with self.assertRaises(RuntimeError):
            maintenance.firewall_rules("192.168.1.20", "0.0.0.0/0")

    def test_systemd_escaping(self):
        app = self.instance()
        self.assertEqual(app.systemd_value('PATH=/home/a b/%x'), '"PATH=/home/a b/%%x"')
        with self.assertRaises(config.SetupError):
            app.systemd_value("KUBECONFIG=a\nExecStart=bad")

    def test_completion_reports_stage_summary_and_source_onboarding_actions(self):
        app = self.instance()
        app.c.sources.append(config.Source(
            "second", "team/second", self.work / "second", 3000,
            self.work / "secrets/second.token"))
        app.completed_stages = [(number, f"Stage {number}", f"阶段 {number}")
                                for number in range(1, 9)]
        app.source_status = {"owner/app": "pending", "team/second": "merged"}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.completion()
        text = output.getvalue()
        self.assertIn("Installation complete: all 8 infrastructure/setup stages passed.", text)
        for number in range(1, 9):
            self.assertIn(f"Completed [{number}/8]: Stage {number}", text)
        self.assertIn("owner/app: source onboarding is NOT complete", text)
        self.assertIn("setup.sh onboard-source", text)
        self.assertIn("--source owner/app", text)
        self.assertIn("--create-pr", text)
        self.assertIn("team/second: notification merged; test PR verification remains.", text)
        self.assertIn("Preview URL format: http://pm-r<repository_id>-pr<PR>.192.168.1.20.sslip.io:18080", text)
        self.assertIn("entry port, not application port", text)
        self.assertIn("Recommended sslip.io", text)
        self.assertIn(config.ACCESS_GUIDE, text)

        app.c.domain_suffix = "preview.example.internal"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.completion()
        manual = output.getvalue()
        self.assertIn("Preview URL format: http://pm-r<repository_id>-pr<PR>.preview.example.internal:18080", manual)
        self.assertIn("Manual suffix:", manual)
        self.assertIn(config.ACCESS_GUIDE, manual)

    def test_failed_install_progress_ignores_stale_checkpoints_and_redacts_secrets(self):
        app = self.instance()
        app.state["completed"] = {str(number): 123 for number in range(1, 9)}
        app.completed_stages = [(1, "Stage 1", "阶段 1"), (2, "Stage 2", "阶段 2")]
        app.secrets = ["ghp_SYNTHETIC_SECRET"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.installation_progress()
        text = output.getvalue()
        self.assertIn("Completed [1/8]: Stage 1", text)
        self.assertIn("Completed [2/8]: Stage 2", text)
        self.assertIn("Installation incomplete. Stages still requiring completion: 3, 4, 5, 6, 7, 8.", text)
        self.assertNotIn("ghp_SYNTHETIC_SECRET", text)

    def test_interrupted_install_prints_progress_summary(self):
        for interruption in (KeyboardInterrupt, EOFError):
            with self.subTest(interruption=interruption):
                app = self.instance()
                app.completed_stages = [(1, "Stage 1", "阶段 1")]
                with patch.object(installer, "load_config", return_value=self.c), \
                     patch.object(installer, "Installer", return_value=app), \
                     patch.object(app, "install", side_effect=interruption), \
                     patch.object(installer.sys, "argv", [
                         str(ROOT / "scripts/setup.py"), "install", "--config", str(self.config_path)
                     ]), contextlib.redirect_stdout(io.StringIO()) as stdout, \
                     contextlib.redirect_stderr(io.StringIO()) as stderr:
                    result = installer.main()
                self.assertEqual(result, 130)
                self.assertIn("Installation incomplete", stdout.getvalue())
                self.assertIn("Stopped; rerun the same command", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
