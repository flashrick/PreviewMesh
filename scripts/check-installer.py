#!/usr/bin/env python3
"""Offline installer checks. Never access GitHub, sudo, a cluster, or Windows settings."""
import contextlib
import copy
import errno
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import setup as installer
import setup_config as config
import setup_downloads as downloads
import setup_maintenance as maintenance
import setup_resume as resume

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

    def run_tty(self, arguments, input_text="", timeout=5):
        """Run the shell entry point with a PTY so its TTY-only language menu is exercised."""
        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["bash", str(ROOT / "scripts/setup.sh"), *arguments],
            cwd=ROOT, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            if input_text:
                os.write(master, input_text.encode())
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        output.extend(os.read(master, 4096))
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                if process.poll() is not None and not ready:
                    break
            if process.poll() is None:
                process.kill()
                self.fail("PTY command timed out: " + repr(arguments))
            return process.wait(), output.decode(errors="replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def checkpoint_inputs(self):
        return {
            "configuration": "configuration-digest",
            "scripts": "scripts-digest",
            "versions": {
                "python": "3.12.3",
                "git": {"version": "2.43.0", "output": "git-output", "binary": {"sha256": "git-binary", "mode": 0o755, "size": 4}},
            },
        }

    def checkpoint_artifact(self, app, name="source"):
        target = self.work / name / ".github/workflows/previewmesh-notify.yml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("workflow\n")
        target.chmod(0o600)

        def artifacts(number):
            self.assertEqual(number, 7)
            return {str(target): resume.file_record(target)}

        app.checkpoints.artifacts = artifacts
        return target

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

    def test_template_language_option_writes_selected_language_without_prompt(self):
        destination = self.work / "zh/setup.ini"
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/setup.sh"), "init", "--template",
             "--language=zh-CN", "--config", str(destination)],
            capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Select language", result.stdout + result.stderr)
        self.assertIn("请编辑", result.stdout)
        self.assertIn("language = zh-CN", destination.read_text())

    def test_tty_init_template_passes_language_once_to_python(self):
        for choice, language, edit_prompt, next_prompt in (
                ("1", "en", "Edit:", "Then run:"),
                ("2", "zh-CN", "请编辑:", "然后执行:")):
            with self.subTest(language=language):
                destination = self.work / ("tty-" + language + "/setup.ini")
                returncode, output = self.run_tty(
                    ["init", "--template", "--config", str(destination)],
                    input_text=choice + chr(10))

                self.assertEqual(returncode, 0)
                self.assertEqual(output.count("Select language / 请选择语言:"), 1)
                self.assertIn(edit_prompt, output)
                self.assertIn(next_prompt, output)
                self.assertIn("language = " + language, destination.read_text())

    def test_tty_entry_prompts_once_before_reading_configuration(self):
        missing = self.work / "missing.ini"
        returncode, output = self.run_tty(
            ["check", "--config", str(missing)], input_text="1" + chr(10))

        self.assertEqual(returncode, 1)
        menu = "Select language / 请选择语言:"
        self.assertEqual(output.count(menu), 1)
        self.assertLess(output.index(menu), output.index("Configuration missing"))

    def test_tty_language_options_skip_prompt_in_both_cli_forms(self):
        missing = self.work / "missing.ini"
        for arguments in (
                ["check", "--language=zh-CN", "--config", str(missing)],
                ["--language", "zh-CN", "check", "--config", str(missing)]):
            with self.subTest(arguments=arguments):
                returncode, output = self.run_tty(arguments)
                self.assertEqual(returncode, 1)
                self.assertNotIn("Select language / 请选择语言:", output)
                self.assertIn("Configuration missing", output)

    def test_tty_help_skips_language_prompt(self):
        returncode, output = self.run_tty(["--help"])

        self.assertEqual(returncode, 0)
        self.assertNotIn("Select language / 请选择语言:", output)
        self.assertIn("--language", output)

    def test_non_tty_check_keeps_config_language_and_does_not_prompt(self):
        loaded = copy.copy(self.c)

        with (
                patch.object(installer, "choose_language", side_effect=AssertionError("unexpected prompt")),
                patch.object(installer, "load_config", return_value=loaded),
                patch.object(installer, "Installer") as installer_class,
                patch.object(installer.sys, "stdin", io.StringIO()),
                patch.object(installer.sys, "argv", [
                    str(ROOT / "scripts/setup.py"), "check", "--config", str(self.config_path)])):
            result = installer.main()

        self.assertEqual(result, 0)
        self.assertEqual(loaded.language, "auto")
        installer_class.return_value.check.assert_called_once_with()

    def test_tty_python_entry_language_choice_precedes_configuration_and_runs_once(self):
        loaded = copy.copy(self.c)
        events = []

        def choose_language():
            events.append("choose")
            return "en"

        def load_config(*args, **kwargs):
            events.append("load")
            return loaded

        class FakeInstaller:
            def __init__(self, config_value, command, verbose):
                events.append("init")

            def check(self):
                events.append("check")

        stdin = Mock()
        stdin.isatty.return_value = True
        with (
                patch.object(installer, "choose_language", side_effect=choose_language),
                patch.object(installer, "load_config", side_effect=load_config),
                patch.object(installer, "Installer", side_effect=FakeInstaller),
                patch.object(installer.sys, "stdin", stdin),
                patch.object(installer.sys, "argv", [
                    str(ROOT / "scripts/setup.py"), "check", "--config", str(self.config_path)])):
            result = installer.main()

        self.assertEqual(result, 0)
        self.assertEqual(events, ["choose", "load", "init", "check"])
        self.assertEqual(loaded.language, "en")

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

    def test_checkpoint_reuses_verified_artifacts(self):
        app = self.instance()
        self.checkpoint_artifact(app)
        inputs = self.checkpoint_inputs()
        artifacts = app.checkpoints.artifacts(7)
        app.checkpoints.record(7, "complete", copy.deepcopy(inputs), artifacts)

        reusable, reason = app.checkpoints.assess(7, inputs)

        self.assertTrue(reusable)
        self.assertIn("verified", reason)

    def test_checkpoint_rejects_missing_corrupt_permission_changed_and_symlink_artifacts(self):
        scenarios = ("missing", "corrupt", "permissions", "symlink")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                app = self.instance()
                target = self.checkpoint_artifact(app, "source-" + scenario)
                inputs = self.checkpoint_inputs()
                app.checkpoints.record(7, "complete", copy.deepcopy(inputs),
                                       app.checkpoints.artifacts(7))
                if scenario == "missing":
                    target.unlink()
                elif scenario == "corrupt":
                    target.write_text("tampered workflow\n")
                elif scenario == "permissions":
                    target.chmod(0o644)
                else:
                    replacement = target.with_name("replacement.yml")
                    replacement.write_text("replacement\n")
                    replacement.chmod(0o600)
                    target.unlink()
                    target.symlink_to(replacement)

                reusable, reason = app.checkpoints.assess(7, inputs)

                self.assertFalse(reusable)
                self.assertIn("artifact", reason)

    def test_checkpoint_rejects_configuration_script_and_dependency_changes(self):
        app = self.instance()
        self.checkpoint_artifact(app)
        original = self.checkpoint_inputs()
        app.checkpoints.record(7, "complete", copy.deepcopy(original),
                               app.checkpoints.artifacts(7))
        changes = {
            "configuration": "new-configuration-digest",
            "scripts": "new-scripts-digest",
            "versions": {
                "python": "3.12.3",
                "git": {"version": "2.44.0", "output": "git-output", "binary": {"sha256": "git-binary", "mode": 0o755, "size": 4}},
            },
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                current = copy.deepcopy(original)
                current[key] = value
                reusable, reason = app.checkpoints.assess(7, current)
                self.assertFalse(reusable)
                expected = {
                    "configuration": "configuration changed",
                    "scripts": "installer or templates changed",
                    "versions": "dependency versions changed",
                }[key]
                self.assertIn(expected, reason)

    def test_checkpoint_records_dependency_version_and_binary_evidence(self):
        app = self.instance()
        tool_dir = self.work / "tools"
        tool_dir.mkdir()
        outputs = {
            "git": "git version 2.43.0\n",
            "go": "go version go1.25.1 linux/amd64\n",
            "gh": "gh version 2.100.0 (2026-09-03)\n",
            "helm": "v3.21.4+g813176c\n",
        }
        for name in outputs:
            binary = tool_dir / name
            binary.write_bytes((name + " binary").encode())
            binary.chmod(0o755)
        app.tool = lambda name: str(tool_dir / name) if name in outputs else None

        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, outputs[args[0]], "")

        app.run = run
        inputs = app.checkpoints.inputs(7)

        for name, output in outputs.items():
            with self.subTest(tool=name):
                version = inputs["versions"][name]
                self.assertIn("version", version)
                self.assertIn("output", version)
                self.assertIn("binary", version)
                self.assertNotEqual(version["output"], output)
                self.assertEqual(version["binary"]["size"], len((name + " binary").encode()))

    def test_old_timestamp_does_not_skip_stage_and_running_checkpoint_cannot_resume(self):
        app = self.instance()
        inputs = self.checkpoint_inputs()
        app.state["completed"] = {"7": 123}
        app.checkpoints.inputs = lambda number: copy.deepcopy(inputs)
        app.checkpoints.artifacts = lambda number: {}
        called = []
        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.step(7, "Stage", "阶段", lambda: called.append(True))

        self.assertEqual(called, [True])
        self.assertEqual(app.stage_results[7][0], "rerun")
        self.assertIn("no verifiable checkpoint", output.getvalue())

        app.state["stages"]["7"] = {
            "schema": resume.SCHEMA,
            "status": "running",
            "inputs": copy.deepcopy(inputs),
            "artifacts": {},
        }
        reusable, reason = app.checkpoints.assess(7, inputs)
        self.assertFalse(reusable)
        self.assertIn("interrupted", reason)

    def test_cross_instance_resume_reuses_valid_stage_and_retries_interrupted_stage(self):
        original = self.instance()
        self.checkpoint_artifact(original, "cross-instance")
        inputs = self.checkpoint_inputs()
        original.checkpoints.record(7, "complete", copy.deepcopy(inputs),
                                    original.checkpoints.artifacts(7))

        resumed = self.instance()
        self.checkpoint_artifact(resumed, "cross-instance")
        resumed.checkpoints.inputs = lambda number: copy.deepcopy(inputs)
        called = []
        with contextlib.redirect_stdout(io.StringIO()):
            resumed.step(7, "Stage", "阶段", lambda: called.append(True))
        self.assertEqual(called, [])
        self.assertEqual(resumed.stage_results[7][0], "reused")

        for interruption in (config.SetupError("failed"), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                failing = self.instance()
                failing.checkpoints.inputs = lambda number: copy.deepcopy(inputs)
                failing.checkpoints.artifacts = lambda number: {}
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(type(interruption)):
                    failing.step(7, "Stage", "阶段", lambda interruption=interruption: (_ for _ in ()).throw(interruption))

                next_run = self.instance()
                next_run.checkpoints.inputs = lambda number: copy.deepcopy(inputs)
                next_run.checkpoints.artifacts = lambda number: {}
                reusable, reason = next_run.checkpoints.assess(7, inputs)
                self.assertFalse(reusable)
                self.assertIn("interrupted", reason)

    def test_dynamic_stages_are_rechecked_instead_of_reused(self):
        app = self.instance()
        inputs = self.checkpoint_inputs()
        app.state.setdefault("stages", {})
        for number in (1, 3, 4, 5, 6, 8):
            app.state["stages"][str(number)] = {
                "schema": resume.SCHEMA,
                "status": "complete",
                "inputs": copy.deepcopy(inputs),
                "artifacts": {},
            }

        for number in (1, 3, 4, 5, 6, 8):
            with self.subTest(stage=number):
                reusable, reason = app.checkpoints.assess(number, inputs)
                self.assertFalse(reusable)
                self.assertIn("live credentials or runtime state", reason)

    def test_control_checkpoint_revalidation_restores_registrations(self):
        app = self.instance()
        app.control_id = "42"
        control = app.c.control_dir
        control.joinpath("config").mkdir(parents=True)
        control.joinpath("config/repositories.json").write_text(json.dumps([
            {"repository_id": "7", "source_repository": "owner/app",
             "port": 8080, "source_secret": "SOURCE_REPO_7"},
        ]))

        def api(path, **kwargs):
            if path == "repos/owner/control":
                return {"private": True, "permissions": {"admin": True},
                        "id": 42, "default_branch": "main"}
            if path == "repos/owner/app":
                return {"full_name": "owner/app", "permissions": {"admin": True}, "id": 7}
            self.fail("unexpected API path: " + path)

        def run(args, **kwargs):
            path = Path(args[2])
            operation = args[3:]
            repository = "owner/control" if path == control else "owner/app"
            if operation == ["remote", "get-url", "origin"]:
                value = f"https://github.com/{repository}.git\n"
            elif operation == ["branch", "--show-current"]:
                value = "main\n"
            elif operation == ["rev-parse", "HEAD"]:
                value = "abc\n"
            elif operation == ["ls-remote", "origin", "refs/heads/main"]:
                value = "abc\trefs/heads/main\n"
            elif operation == ["status", "--porcelain"]:
                value = ""
            else:
                self.fail("unexpected git command: " + repr(args))
            return subprocess.CompletedProcess(args, 0, value, "")

        app.api = api
        app.run = run
        self.assertTrue(app.checkpoints.control_valid())
        self.assertEqual(app.registrations, [{
            "repository_id": "7", "source_repository": "owner/app",
            "port": 8080, "source_secret": "SOURCE_REPO_7",
        }])

        def dirty_run(args, **kwargs):
            if args[3:] == ["status", "--porcelain"]:
                return subprocess.CompletedProcess(args, 0, " M config/repositories.json\n", "")
            return run(args, **kwargs)

        app.run = dirty_run
        self.assertFalse(app.checkpoints.control_valid())

    def test_remote_checkpoint_failure_uses_safe_reason(self):
        app = self.instance()
        inputs = self.checkpoint_inputs()
        artifacts = {"control": {"sha256": "digest", "mode": 0o755, "size": 4}}
        app.checkpoints.artifacts = lambda number: artifacts
        app.checkpoints.record(2, "complete", copy.deepcopy(inputs), artifacts)
        app.checkpoints.control_valid = lambda: (_ for _ in ()).throw(
            config.SetupError("github_pat_sensitive-value"))

        reusable, reason = app.checkpoints.assess(2, inputs)

        self.assertFalse(reusable)
        self.assertIn("validation failed", reason)
        self.assertNotIn("github_pat_sensitive-value", reason)

    def test_damaged_state_has_actionable_recovery_hint_without_echoing_values(self):
        state_path = self.work / ".local/share/previewmesh/install-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text('{"stages": "github_pat_sensitive-value"')

        with patch.object(Path, "home", return_value=self.work), self.assertRaises(config.SetupError) as error:
            installer.Installer(self.c, "check")

        self.assertIn("install-state.json.bak", str(error.exception))
        self.assertIn("rerun the same command", str(error.exception))
        self.assertNotIn("github_pat_sensitive-value", str(error.exception))

    def test_state_backup_is_last_valid_json_and_corruption_does_not_replace_it(self):
        app = self.instance()
        app.save()
        app.state["marker"] = "second"
        app.save()
        backup = app.state_path.with_suffix(".json.bak")
        backup_text = backup.read_text()

        self.assertEqual(json.loads(backup_text), {"control": "owner/control"})
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

        app.state_path.write_text("broken state")
        app.state["marker"] = "third"
        with self.assertRaises(json.JSONDecodeError):
            app.save()
        self.assertEqual(backup.read_text(), backup_text)
        self.assertEqual(app.state_path.read_text(), "broken state")

    def test_recovery_summary_reports_reasons_without_credentials(self):
        app = self.instance()
        app.secrets = ["ghp_SYNTHETIC_SECRET"]
        app.completed_stages = [(2, "Stage 2", "阶段 2")]
        app.stage_results = {
            2: ("reused", "artifacts, inputs and versions verified"),
            7: ("rerun", "artifact missing, incomplete or changed"),
        }

        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.installation_progress()

        text = output.getvalue()
        self.assertIn("Recovery [2/8]: reused", text)
        self.assertIn("Recovery [7/8]: rerun", text)
        self.assertIn("artifact missing", text)
        self.assertNotIn("ghp_SYNTHETIC_SECRET", text)

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
