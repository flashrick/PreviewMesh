#!/usr/bin/env python3
"""Focused tests for the credential-free evaluation freeze."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "evaluation_freeze", SCRIPTS / "evaluation_freeze.py"
)
FREEZE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FREEZE)


def create_git_repository(parent, name, marker):
    """Create a committed local repository without contacting a remote."""
    repository = Path(parent) / name
    repository.mkdir()
    commands = (
        ["git", "init", "--quiet", str(repository)],
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(repository), "config", "user.name", "PreviewMesh Test"],
    )
    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True)
    (repository / "marker.txt").write_text(marker, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "marker.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "snapshot fixture"],
        check=True,
        capture_output=True,
        text=True,
    )
    return repository


def write_json(path, document):
    Path(path).write_text(json.dumps(document), encoding="utf-8")


class RepositorySnapshotTests(unittest.TestCase):
    def test_public_and_control_revisions_are_captured_from_local_git_repositories(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = create_git_repository(temporary, "public", "public")
            control = create_git_repository(temporary, "control", "control")

            for label, repository in (("public", public), ("control", control)):
                with self.subTest(repository=label):
                    snapshot = FREEZE.git_snapshot(repository)
                    self.assertTrue(snapshot["available"])
                    self.assertRegex(snapshot["value"]["revision"], r"^[0-9a-f]{40}$")
                    self.assertTrue(snapshot["value"]["committed_at"])
                    self.assertTrue(snapshot["value"]["working_tree_clean"])

            (public / "marker.txt").write_text("working tree change", encoding="utf-8")
            self.assertFalse(FREEZE.git_snapshot(public)["value"]["working_tree_clean"])


class ConfigurationSnapshotTests(unittest.TestCase):
    def test_configuration_snapshot_excludes_registration_and_credential_fields(self):
        configuration = {
            "control_repository": "private/previewmesh-control",
            "workflow": "preview.yml",
            "ref": "main",
            "repository_id": "top-level-id-must-not-escape",
            "source_repository": "top-level/source-must-not-escape",
            "secret": "TOP-LEVEL-SECRET-MUST-NOT-ESCAPE",
            "token": "TOP-LEVEL-TOKEN-MUST-NOT-ESCAPE",
            "fixtures": [
                {
                    "name": "podinfo",
                    "repository_id": "123",
                    "pr_number": "4",
                    "source_repository": "private/preview-fixture",
                    "source_commit_sha": "a" * 40,
                    "image_digest": "sha256:" + "b" * 64,
                    "secret": "FIXTURE-SECRET-MUST-NOT-ESCAPE",
                    "token": "FIXTURE-TOKEN-MUST-NOT-ESCAPE",
                    "conditions": {
                        "project_size": "small",
                        "build_policy": "rebuild",
                        "metric_interval_seconds": 10,
                        "operation_timeout_seconds": 300,
                        "http_timeout_seconds": 60,
                        "concurrency": 1,
                        "secret": "NESTED-SECRET-MUST-NOT-ESCAPE",
                        "token": "NESTED-TOKEN-MUST-NOT-ESCAPE",
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "experiment.json"
            write_json(path, configuration)
            snapshot = FREEZE.config_snapshot(path)

        self.assertTrue(snapshot["available"])
        snapshot_json = json.dumps(snapshot, sort_keys=True)
        forbidden_fields = ("repository_id", "source_repository", "secret", "token")
        for field in forbidden_fields:
            with self.subTest(field=field):
                self.assertNotIn(field, snapshot_json)
        for sensitive_value in (
            "TOP-LEVEL-SECRET-MUST-NOT-ESCAPE",
            "TOP-LEVEL-TOKEN-MUST-NOT-ESCAPE",
            "private/preview-fixture",
            "123",
        ):
            self.assertNotIn(sensitive_value, snapshot_json)


class ResourceDefaultTests(unittest.TestCase):
    def test_resource_defaults_parse_valid_cpu_and_memory_quantities(self):
        values = """
resources:
  requests:
    cpu: 10m
    memory: 32Mi
    gpu: 1
  limits:
    cpu: "1"
    memory: 64Mi
    invalid: not-a-quantity
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "values.yaml"
            path.write_text(values, encoding="utf-8")
            result = FREEZE.chart_resource_defaults(path)

        self.assertTrue(result["available"])
        self.assertEqual(
            result["value"],
            {
                "requests": {"cpu": "10m", "memory": "32Mi"},
                "limits": {"cpu": "1", "memory": "64Mi"},
            },
        )


class ClusterAvailabilityTests(unittest.TestCase):
    def test_kubernetes_permission_failure_is_reported_as_unavailable(self):
        with patch.object(FREEZE, "read_command", return_value=None) as read_command:
            snapshot = FREEZE.cluster_snapshot()

        self.assertEqual(read_command.call_count, 2)
        self.assertFalse(snapshot["version"]["available"])
        self.assertFalse(snapshot["nodes"]["available"])
        self.assertIn("permissions", snapshot["version"]["reason"])
        self.assertIn("permissions", snapshot["nodes"]["reason"])


class FreezeWorkflowTests(unittest.TestCase):
    def test_permission_limited_cluster_produces_partial_freeze_and_manifest(self):
        cluster = {
            "version": FREEZE.unavailable(
                "kubectl version is unavailable; check kubeconfig permissions"
            ),
            "nodes": FREEZE.unavailable(
                "Kubernetes node inventory is unavailable; check kubeconfig permissions"
            ),
        }

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "freeze"
            stdout = io.StringIO()
            with patch.object(FREEZE, "cluster_snapshot", return_value=cluster), contextlib.redirect_stdout(stdout):
                exit_code = FREEZE.main(["--output", str(output)])

            freeze = json.loads((output / "freeze.json").read_text(encoding="utf-8"))
            manifest = json.loads((output / "MANIFEST.json").read_text(encoding="utf-8"))
            summary = json.loads(
                (output / "manifest-summary.json").read_text(encoding="utf-8")
            )
            printed = json.loads(stdout.getvalue())
            freeze_size = (output / "freeze.json").stat().st_size
            manifest_hash = hashlib.sha256((output / "MANIFEST.json").read_bytes()).hexdigest()

        self.assertEqual(exit_code, 1)
        self.assertEqual(freeze["status"], "partial")
        self.assertEqual(printed["status"], "partial")
        self.assertTrue(
            any(item.startswith("cluster.version:") for item in freeze["limitations"])
        )
        self.assertTrue(
            any(item.startswith("cluster.nodes:") for item in freeze["limitations"])
        )
        self.assertEqual([entry["path"] for entry in manifest["files"]], ["freeze.json"])
        self.assertEqual(manifest["total_bytes"], freeze_size)
        self.assertEqual(summary["file_count"], 1)
        self.assertEqual(summary["bytes"], manifest["total_bytes"])
        self.assertEqual(summary["sha256"], manifest_hash)


if __name__ == "__main__":
    unittest.main()
