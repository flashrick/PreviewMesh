#!/usr/bin/env python3
"""Offline checks for the optional terminal UI and its plain-text fallback."""
import contextlib
import io
import os
import unittest
from unittest.mock import patch

import setup_language
import setup_ui
import setup_ui_bootstrap
import setup_wizard


class FakePrompt:
    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        return self.answer


class FakeChoice:
    def __init__(self, title, value):
        self.title = title
        self.value = value


class FakeQuestionary:
    Choice = FakeChoice

    def __init__(self, answer):
        self.answer = answer
        self.select_calls = []

    @staticmethod
    def Style(styles):
        return styles

    def select(self, prompt, **kwargs):
        self.select_calls.append((prompt, kwargs))
        return FakePrompt(self.answer)


class SetupUITests(unittest.TestCase):
    def test_redirected_and_ci_streams_disable_terminal_controls(self):
        ui = setup_ui.TerminalUI()
        self.assertFalse(ui.interactive)

        class TTY:
            @staticmethod
            def isatty():
                return True

        with patch.object(setup_ui.sys, "stdin", TTY()), \
             patch.object(setup_ui.sys, "stdout", TTY()), \
             patch.dict(os.environ, {"CI": "1", "PREVIEWMESH_INTERACTIVE": ""}):
            self.assertFalse(setup_ui.prompt_capable())
            self.assertFalse(setup_ui.interactive_capable())

    def test_plain_override_keeps_line_prompts_without_terminal_controls(self):
        class TTY:
            @staticmethod
            def isatty():
                return True

        with patch.object(setup_ui.sys, "stdin", TTY()), \
             patch.object(setup_ui.sys, "stdout", TTY()), \
             patch.dict(os.environ, {"PREVIEWMESH_UI": "plain", "TERM": "xterm"}):
            self.assertTrue(setup_ui.prompt_capable())
            self.assertFalse(setup_ui.interactive_capable())

    def test_plain_selection_maps_numbers_without_escape_sequences(self):
        output = io.StringIO()
        with patch("builtins.input", return_value="2"), contextlib.redirect_stdout(output):
            value = setup_ui.TerminalUI(force_plain=True).select("Choose", ["alpha", "beta"])

        self.assertEqual(value, "beta")
        self.assertIn("1. alpha", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())

    def test_questionary_selection_uses_arrow_shortcuts_when_available(self):
        fake = FakeQuestionary("beta")
        with patch.object(setup_ui, "questionary", fake), \
             patch.object(setup_ui, "interactive_capable", return_value=True):
            value = setup_ui.TerminalUI().select("Choose", ["alpha", "beta"], default="alpha")

        self.assertEqual(value, "beta")
        self.assertEqual(len(fake.select_calls), 1)
        self.assertTrue(fake.select_calls[0][1]["use_shortcuts"])
        self.assertEqual(fake.select_calls[0][1]["default"], "alpha")

    def test_questionary_cancel_is_consistent_with_line_input_cancel(self):
        fake = FakeQuestionary(None)
        with patch.object(setup_ui, "questionary", fake), \
             patch.object(setup_ui, "interactive_capable", return_value=True):
            with self.assertRaises(EOFError):
                setup_ui.TerminalUI().select("Choose", ["alpha", "beta"])

        with patch("builtins.input", return_value="q"):
            with self.assertRaises(EOFError):
                setup_wizard.choose("Choose", [], language="en")

    def test_language_selection_works_through_shared_questionary_adapter(self):
        fake = FakeQuestionary("中文")
        with patch.object(setup_ui, "questionary", fake), \
             patch.object(setup_ui, "interactive_capable", return_value=True):
            self.assertEqual(setup_language.choose_language(), "zh-CN")

    def test_wizard_choice_uses_shared_questionary_adapter(self):
        fake = FakeQuestionary("owner/app")
        with patch.object(setup_ui, "questionary", fake), \
             patch.object(setup_ui, "interactive_capable", return_value=True):
            value = setup_wizard.choose("Repository", ["owner/app", "owner/other"])

        self.assertEqual(value, "owner/app")
        self.assertTrue(fake.select_calls)

    def test_installation_plan_is_readable_in_plain_mode(self):
        output = io.StringIO()
        stages = [(1, "Prepare"), (2, "Configure")]
        with contextlib.redirect_stdout(output):
            ui = setup_ui.TerminalUI(force_plain=True)
            ui.installation_plan(stages, completed=[1], current=2)
            ui.status("success", "ready")

        text = output.getvalue()
        self.assertIn("DONE", text)
        self.assertIn("Prepare", text)
        self.assertIn("CURRENT", text)
        self.assertIn("Configure", text)
        self.assertIn("OK     ready", text)
        self.assertNotIn("\x1b", text)

    def test_status_semantics_have_distinct_symbols_and_roles(self):
        self.assertEqual(set(setup_ui._STATUS_META), {
            "success", "warning", "error", "skipped", "reused", "running", "info",
        })
        self.assertEqual(setup_ui._STATUS_META["success"][2], "✓")
        self.assertEqual(setup_ui._STATUS_META["error"][2], "×")
        self.assertEqual(setup_ui._STATUS_META["reused"][3], "reused")

    def test_ui_bootstrap_reuses_base_or_prepares_isolated_venv(self):
        calls = []
        installed = False

        def fake_run(command, **kwargs):
            nonlocal installed
            command = [str(item) for item in command]
            calls.append(command)
            if command[1:3] == ["-c", "import rich, questionary"]:
                return_value = command[0].endswith("/bin/python") and installed
                return setup_ui_bootstrap.subprocess.CompletedProcess(command, int(not return_value))
            if command[1:3] == ["-m", "venv"]:
                venv = setup_ui_bootstrap.Path(command[3])
                (venv / "bin").mkdir(parents=True, exist_ok=True)
                (venv / "bin" / "python").touch()
                return setup_ui_bootstrap.subprocess.CompletedProcess(command, 0)
            if command[1:4] == ["-m", "pip", "install"]:
                installed = True
                return setup_ui_bootstrap.subprocess.CompletedProcess(command, 0)
            self.fail("unexpected bootstrap command: " + repr(command))

        with patch.object(setup_ui_bootstrap.subprocess, "run", side_effect=fake_run):
            with self.subTest("base unavailable, isolated environment is created"):
                with self.temp_directory() as directory:
                    selected = setup_ui_bootstrap.ensure_ui_python(
                        directory, "/usr/bin/python3", directory / "ui-venv")
                    self.assertEqual(selected, str(directory / "ui-venv" / "bin" / "python"))
            self.assertTrue(any(command[1:3] == ["-m", "venv"] for command in calls))
            self.assertTrue(any(command[1:4] == ["-m", "pip", "install"] for command in calls))

    @contextlib.contextmanager
    def temp_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="previewmesh-ui-test-") as directory:
            yield setup_ui_bootstrap.Path(directory)

    @unittest.skipUnless(setup_ui.Console is not None, "Rich is not installed")
    def test_rich_rendering_path_handles_panels_tables_and_activity(self):
        output = io.StringIO()
        with patch.object(setup_ui, "interactive_capable", return_value=True), \
             patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}), \
             contextlib.redirect_stdout(output):
            ui = setup_ui.TerminalUI()
            ui.section(1, 2, "Section", "body")
            ui.installation_plan([(1, "Prepare"), (2, "Finish")], current=1)
            ui.stage(1, 2, "Prepare", "detail")
            ui.status("success", "ready")
            with ui.activity("work"):
                pass
            ui.summary("Summary", [("State", "ready")], success=True)

        text = output.getvalue()
        self.assertIn("Section", text)
        self.assertIn("Prepare", text)
        self.assertIn("Summary", text)


if __name__ == "__main__":
    unittest.main()
