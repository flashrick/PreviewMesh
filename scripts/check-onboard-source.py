#!/usr/bin/env python3
"""Offline checks for source onboarding; no GitHub or checkout is touched."""
import base64
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import setup_onboard as onboard_source
from setup_config import SetupError


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = onboard_source.WORKFLOW
BRANCH = onboard_source.BRANCH


def completed(value=None, *, code=0, stderr=""):
    return subprocess.CompletedProcess(
        [], code, "" if value is None else json.dumps(value), stderr
    )


class GitHubMock:
    """Small in-memory GitHub Git-data API model used by all onboarding tests."""

    def __init__(self, *, old="", prs=None, branch_head=None, extra_files=None,
                 fail_branch=None, fail_pull=False, base_ref_sequence=None,
                 branch_ref_sequence=None, parent_symlink=False):
        self.expected = onboard_source.notification(ROOT, "owner/control")
        self.old = old
        self.prs = list(prs or [])
        self.branch_head = branch_head
        self.extra_files = list(extra_files or [])
        self.fail_branch = fail_branch
        self.fail_pull = fail_pull
        self.base_ref_sequence = list(base_ref_sequence or ["base"])
        self.branch_ref_sequence = (list(branch_ref_sequence)
                                    if branch_ref_sequence is not None else None)
        self.parent_symlink = parent_symlink
        self.base_ref_calls = 0
        self.branch_ref_calls = 0
        self.calls = []
        self.writes = []
        self.tree_payload = None
        self.commit_payload = None
        self.ref_payload = None
        self.pull_payload = None

    def _tree(self, sha):
        if sha in ("base-tree", "branch-tree"):
            child = "base-gh" if sha == "base-tree" else "branch-gh"
            if self.parent_symlink and sha == "base-tree":
                return {"tree": [{"path": ".github", "type": "blob",
                                   "mode": "120000", "sha": "symlink-blob"}]}
            return {"tree": [{"path": ".github", "type": "tree", "sha": child}]}
        if sha in ("base-gh", "branch-gh"):
            child = "base-workflows" if sha == "base-gh" else "branch-workflows"
            return {"tree": [{"path": "workflows", "type": "tree", "sha": child}]}
        if sha == "base-workflows":
            if not self.old:
                return {"tree": []}
            return {"tree": [{"path": "previewmesh-notify.yml", "type": "blob",
                               "mode": "100644", "sha": "old-blob"}]}
        if sha == "branch-workflows":
            return {"tree": [{"path": "previewmesh-notify.yml", "type": "blob",
                               "mode": "100644", "sha": "branch-blob"}]}
        raise AssertionError(f"unexpected tree {sha}")

    def _endpoint(self, args):
        return next(value for value in args if value.startswith("repos/"))

    def run(self, args, *, input=None, **_kwargs):
        endpoint = self._endpoint(args)
        method = args[args.index("--method") + 1]
        data = json.loads(input) if input is not None else None
        self.calls.append((method, endpoint, data))
        prefix = "repos/owner/app/"
        if endpoint == "repos/owner/app":
            return completed({"default_branch": "main"})
        if endpoint == prefix + "git/ref/heads/main":
            index = min(self.base_ref_calls, len(self.base_ref_sequence) - 1)
            self.base_ref_calls += 1
            return completed({"object": {"sha": self.base_ref_sequence[index]}})
        if endpoint == prefix + "git/commits/base":
            return completed({"tree": {"sha": "base-tree"}, "parents": [{"sha": "parent"}]})
        if endpoint == prefix + "git/commits/new-head":
            return completed({"tree": {"sha": "branch-tree"},
                              "parents": [{"sha": "base"}]})
        if endpoint == prefix + "git/trees/base-tree":
            return completed(self._tree("base-tree"))
        if endpoint == prefix + "git/trees/base-gh":
            return completed(self._tree("base-gh"))
        if endpoint == prefix + "git/trees/base-workflows":
            return completed(self._tree("base-workflows"))
        if endpoint == prefix + "git/trees/branch-tree":
            return completed(self._tree("branch-tree"))
        if endpoint == prefix + "git/trees/branch-gh":
            return completed(self._tree("branch-gh"))
        if endpoint == prefix + "git/trees/branch-workflows":
            return completed(self._tree("branch-workflows"))
        if endpoint == prefix + "git/blobs/old-blob":
            return completed({"content": base64.b64encode(self.old.encode()).decode()})
        if endpoint == prefix + "git/blobs/branch-blob":
            return completed({"content": base64.b64encode(self.expected.encode()).decode()})
        if endpoint.startswith(prefix + "pulls?") and method == "GET":
            return completed(self.prs)
        if endpoint == prefix + "git/ref/heads/previewmesh%2Fonboard-source":
            if self.fail_branch:
                return completed(code=1, stderr=self.fail_branch)
            if self.branch_ref_sequence is not None:
                index = min(self.branch_ref_calls, len(self.branch_ref_sequence) - 1)
                head = self.branch_ref_sequence[index]
                self.branch_ref_calls += 1
                if head is None:
                    return completed(code=1, stderr="gh: HTTP 404 Not Found")
                return completed({"object": {"sha": head}})
            if self.branch_head is None:
                return completed(code=1, stderr="gh: HTTP 404 Not Found")
            return completed({"object": {"sha": self.branch_head}})
        if endpoint == prefix + "compare/base...new-head":
            files = [{"filename": WORKFLOW}, *
                     [{"filename": name} for name in self.extra_files]]
            return completed({"status": "ahead", "files": files})
        if endpoint == prefix + "git/trees" and method == "POST":
            self.writes.append((method, endpoint, data))
            self.tree_payload = data
            return completed({"sha": "new-tree"})
        if endpoint == prefix + "git/commits" and method == "POST":
            self.writes.append((method, endpoint, data))
            self.commit_payload = data
            return completed({"sha": "new-head"})
        if endpoint == prefix + "git/refs" and method == "POST":
            self.writes.append((method, endpoint, data))
            self.ref_payload = data
            self.branch_head = data["sha"]
            return completed({})
        if endpoint == prefix + "pulls" and method == "POST":
            self.writes.append((method, endpoint, data))
            self.pull_payload = data
            if self.fail_pull:
                self.fail_pull = False
                return completed(code=1, stderr="gh: HTTP 500 ghp_SYNTHETIC_SECRET")
            value = {"html_url": "https://github.com/owner/app/pull/42"}
            self.prs = [value]
            return completed(value)
        raise AssertionError(f"unexpected GitHub API call: {method} {endpoint}")


class FakeInstaller:
    def __init__(self, server, source_directory):
        self.server = server
        self.c = SimpleNamespace(
            control="owner/control",
            path=Path("/tmp/previewmesh-check/setup.ini"),
            sources=[SimpleNamespace(repository="owner/app", directory=source_directory)],
        )
        self.verbose = False
        self.secrets = ["ghp_SYNTHETIC_SECRET"]
        self.verification_calls = 0

    def tool(self, name):
        return "/usr/bin/gh" if name == "gh" else None

    def run(self, args, **kwargs):
        return self.server.run(args, **kwargs)

    def say(self, en, _zh):
        print(en)

    def redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def source_verification(self):
        self.verification_calls += 1


class OnboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="previewmesh-onboard-")
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.source.mkdir()
        (self.source / "unrelated-local-change.txt").write_text("must stay local\n")

    def invoke(self, server, *, create=False, answer=None):
        installer = FakeInstaller(server, self.source)
        output = io.StringIO()
        input_patch = patch("builtins.input", return_value=answer or "")
        with contextlib.redirect_stdout(output):
            with input_patch if create else contextlib.nullcontext():
                onboard_source.onboard(installer, ROOT, "owner/app", create)
        return installer, output.getvalue()

    def test_preview_diff_is_read_only_and_leaves_local_checkout_unchanged(self):
        server = GitHubMock()
        before = sorted(path.relative_to(self.source).as_posix() for path in self.source.rglob("*"))
        installer, output = self.invoke(server)
        after = sorted(path.relative_to(self.source).as_posix() for path in self.source.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(server.writes, [])
        self.assertTrue(any("Proposed complete diff" in line for line in output.splitlines()))
        self.assertEqual(installer.verification_calls, 0)

    def test_create_pr_writes_one_workflow_tree_and_defaults_commit_parent(self):
        server = GitHubMock()
        _, output = self.invoke(server, create=True, answer="create")
        self.assertEqual(len(server.writes), 4)
        self.assertEqual(server.tree_payload["base_tree"], "base-tree")
        self.assertEqual(server.tree_payload["tree"], [{
            "path": WORKFLOW, "mode": "100644", "type": "blob", "content": server.expected,
        }])
        self.assertEqual(server.commit_payload["parents"], ["base"])
        self.assertEqual(server.commit_payload["tree"], "new-tree")
        self.assertEqual(server.ref_payload, {"ref": "refs/heads/" + BRANCH, "sha": "new-head"})
        self.assertEqual(server.pull_payload["head"], BRANCH)
        self.assertIn("PR created", output)

    def test_create_pr_cancellation_does_not_write(self):
        server = GitHubMock()
        self.invoke(server, create=True, answer="cancel")
        self.assertEqual(server.writes, [])
        self.assertIsNone(server.branch_head)

    def test_matching_default_workflow_is_a_noop(self):
        server = GitHubMock(old=onboard_source.notification(ROOT, "owner/control"))
        _, output = self.invoke(server)
        self.assertIn("already merged", output)
        self.assertEqual(server.writes, [])
        self.assertFalse(any(endpoint.endswith("/pulls") for _, endpoint, _ in server.calls))

    def test_existing_open_pr_is_reused_without_branch_or_write(self):
        server = GitHubMock(prs=[{"html_url": "https://github.com/owner/app/pull/9"}])
        installer, output = self.invoke(server)
        self.assertIn("https://github.com/owner/app/pull/9", output)
        self.assertEqual(server.writes, [])
        self.assertEqual(installer.verification_calls, 1)

    def test_partial_branch_failure_is_retryable_and_reuses_branch(self):
        server = GitHubMock(fail_pull=True)
        with self.assertRaises(SetupError):
            self.invoke(server, create=True, answer="create")
        self.assertEqual(server.branch_head, "new-head")
        writes_after_failure = len(server.writes)
        installer, output = self.invoke(server, create=True, answer="create")
        self.assertEqual(server.branch_head, "new-head")
        self.assertGreater(len(server.writes), writes_after_failure)
        self.assertEqual(len([item for item in server.writes if item[1].endswith("/git/trees")]), 1)
        self.assertEqual(len([item for item in server.writes if item[1].endswith("/git/commits")]), 1)
        self.assertIn("PR created", output)
        self.assertEqual(installer.verification_calls, 1)

    def test_existing_branch_with_unrelated_file_is_rejected_without_write(self):
        server = GitHubMock(branch_head="new-head", extra_files=["README.md"])
        with self.assertRaisesRegex(SetupError, "differs from this proposal"):
            self.invoke(server, create=True, answer="create")
        self.assertEqual(server.writes, [])

    def test_existing_branch_change_after_confirmation_is_rejected_without_write(self):
        server = GitHubMock(branch_head="new-head",
                            branch_ref_sequence=["new-head", "changed-head"])
        with self.assertRaisesRegex(SetupError, "Onboarding branch changed"):
            self.invoke(server, create=True, answer="create")
        self.assertEqual(server.writes, [])

    def test_symlink_workflow_parent_is_rejected(self):
        server = GitHubMock(parent_symlink=True)
        with self.assertRaisesRegex(SetupError, "parent is not a directory"):
            self.invoke(server)
        self.assertEqual(server.writes, [])

    def test_default_branch_change_after_confirmation_aborts_without_write(self):
        server = GitHubMock(base_ref_sequence=["base", "changed"])
        with self.assertRaisesRegex(SetupError, "Default branch changed"):
            self.invoke(server, create=True, answer="create")
        self.assertEqual(server.writes, [])

    def test_non_404_branch_error_is_not_treated_as_missing(self):
        server = GitHubMock(fail_branch="gh: HTTP 500 ghp_SYNTHETIC_SECRET")
        with self.assertRaises(SetupError) as error:
            self.invoke(server, create=True, answer="create")
        self.assertIn("GitHub GET request failed (HTTP 500)", str(error.exception))
        self.assertNotIn("ghp_SYNTHETIC_SECRET", str(error.exception))
        self.assertEqual(server.writes, [])

    def test_diff_and_api_errors_redact_credentials(self):
        old = "name: old\n# ghp_SYNTHETIC_SECRET\n"
        server = GitHubMock(old=old)
        _, output = self.invoke(server)
        self.assertNotIn("ghp_SYNTHETIC_SECRET", output)
        self.assertIn("[REDACTED]", output)

        failing = GitHubMock(fail_branch="gh: HTTP 500 ghp_SYNTHETIC_SECRET")
        with self.assertRaises(SetupError) as error:
            self.invoke(failing, create=True, answer="create")
        self.assertNotIn("ghp_SYNTHETIC_SECRET", str(error.exception))


class CliValidationTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/setup.py"), *arguments],
            cwd=ROOT, capture_output=True, text=True,
        )

    def test_onboard_source_requires_source(self):
        result = self.run_cli("onboard-source", "--config", "/tmp/does-not-matter.ini")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --source", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_onboard_flags_are_rejected_for_other_commands(self):
        result = self.run_cli("check", "--source", "owner/app", "--config", "/tmp/does-not-matter.ini")
        self.assertEqual(result.returncode, 2)
        self.assertIn("require onboard-source", result.stderr)


if __name__ == "__main__":
    unittest.main()
