#!/usr/bin/env python3
"""Focused unit tests for the repeatable experiment runner."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "experiment_runner", ROOT / "scripts" / "experiment_runner.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
TIMESTAMP = "2026-10-09T00:00:00.000000Z"


def valid_config():
    return {
        "control_repository": "example/previewmesh-control",
        "workflow": "preview.yml",
        "fixtures": [
            {
                "name": "podinfo",
                "repository_id": "123",
                "pr_number": "4",
                "source_repository": "example/preview-fixture",
                "source_commit_sha": SOURCE_SHA,
                "image_digest": IMAGE_DIGEST,
            }
        ],
    }


def success_record():
    """Return a complete create record with explicit non-applicable fields."""
    timeline = {field: TIMESTAMP for field in RUNNER.TIMELINE_FIELDS}
    timeline["failure_observed_at_utc"] = None
    timeline["recovery_started_at_utc"] = None
    timeline["recovery_verified_at_utc"] = None
    timeline["cleanup_started_at_utc"] = None
    timeline["cleanup_absent_at_utc"] = None
    timeline_reasons = {
        field: "not applicable to a successful create run"
        for field in (
            "failure_observed_at_utc",
            "recovery_started_at_utc",
            "recovery_verified_at_utc",
            "cleanup_started_at_utc",
            "cleanup_absent_at_utc",
        )
    }
    return {
        "schema_version": RUNNER.SCHEMA_VERSION,
        "run_id": "podinfo-screate-c1-n1-20261009T000000Z",
        "scenario": "create",
        "repetition": 1,
        "fixture": "podinfo",
        "source_commit_sha": SOURCE_SHA,
        "image_digest": IMAGE_DIGEST,
        "conditions": {
            "project_size": "small",
            "build_policy": "rebuild",
            "metric_interval_seconds": 10,
            "operation_timeout_seconds": 300,
            "http_timeout_seconds": 60,
            "concurrency": 1,
        },
        "timeline": timeline,
        "timeline_unavailable_reasons": timeline_reasons,
        "state": {
            "desired": {"status": "ready"},
            "observed": {"status": "ready"},
            "action": {"status": "deployed"},
            "final": {"status": "ready"},
        },
        "outcome": "success",
        "failure_trigger": "none",
        "recovery_mode": "none",
        "final_served_sha": SOURCE_SHA,
        "cleanup": {
            "namespace_uid": "unavailable",
            "uid_guarded": None,
            "namespace_absent": None,
            "related_resources_absent": None,
            "orphan_scope": "unavailable",
            "unavailable_reason": "cleanup is not applicable until the preview is closed",
        },
        "metrics": {
            "units": {"cpu": "core", "memory": "byte"},
            "coverage_seconds": {"resource_collection": 30},
            "unavailable_reasons": [],
            "measurements": {
                "preview:uid-1:cpu": {
                    "available": True,
                    "unit": "core_minute",
                    "value": 0.5,
                    "covered_seconds": 30,
                    "uncovered_seconds": 0,
                }
            },
        },
        "lifecycle": {
            "expected_stages": [
                "event",
                "build",
                "deploy",
                "readiness",
                "http_verify",
                "report",
            ],
            "observed_stages": [
                "build",
                "deploy",
                "event",
                "http_verify",
                "readiness",
                "report",
            ],
            "missing_stages": {
                "cleanup": "not_applicable for an open preview scenario"
            },
        },
        "evidence": {
            "path": "/tmp/previewmesh-evidence",
            "bytes": 1,
            "sha256": "c" * 64,
        },
        "valid": True,
        "exclusion_reason": "",
    }


class ConfigValidationTests(unittest.TestCase):
    def test_valid_config_is_accepted(self):
        config = valid_config()
        self.assertIs(RUNNER.validate_config(config), config)

    def test_config_rejects_invalid_values(self):
        cases = (
            ("repository", lambda config: config.update(control_repository="not a repo"), "control_repository"),
            ("workflow", lambda config: config.update(workflow="nested/preview.yml"), "config.workflow"),
            (
                "duplicate fixture",
                lambda config: config["fixtures"].append(dict(config["fixtures"][0])),
                "fixture names must be unique",
            ),
            (
                "repository id",
                lambda config: config["fixtures"][0].update(repository_id="0"),
                "fixture.repository_id",
            ),
            (
                "source SHA",
                lambda config: config["fixtures"][0].update(source_commit_sha="ABC"),
                "fixture.source_commit_sha",
            ),
            (
                "image digest",
                lambda config: config["fixtures"][0].update(image_digest="latest"),
                "fixture.image_digest",
            ),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                config = valid_config()
                mutate(config)
                with self.assertRaisesRegex(RUNNER.RunnerError, message):
                    RUNNER.validate_config(config)


class EvidenceBoundaryTests(unittest.TestCase):
    def test_evidence_root_must_be_outside_public_checkout(self):
        for candidate in (ROOT, ROOT / "private-evidence"):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(
                    RUNNER.RunnerError,
                    "outside the public checkout",
                ):
                    RUNNER.private_root(candidate)

    def test_external_evidence_root_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(RUNNER.private_root(temporary), Path(temporary).resolve())


class EvidenceManifestTests(unittest.TestCase):
    def test_manifest_excludes_control_files_and_hashes_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)
            artifact = b"artifact payload\n"
            conditions = b'{"scenario":"create"}\n'
            (archive / "artifacts").mkdir()
            (archive / "artifacts" / "output.txt").write_bytes(artifact)
            (archive / "conditions.json").write_bytes(conditions)
            (archive / "run-record.json").write_bytes(b"must be excluded")
            (archive / "MANIFEST.json").write_bytes(b"old manifest")

            manifest = RUNNER.evidence_manifest(archive)
            document = json.loads((archive / "MANIFEST.json").read_text(encoding="utf-8"))
            manifest_bytes = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()

            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["total_bytes"], len(artifact) + len(conditions))
            self.assertEqual(manifest["bytes"], len(artifact) + len(conditions))
            self.assertEqual(manifest["file_count"], 2)
            self.assertEqual(manifest["sha256"], hashlib.sha256(manifest_bytes).hexdigest())
            self.assertEqual(
                {entry["path"] for entry in document["files"]},
                {"artifacts/output.txt", "conditions.json"},
            )
            self.assertNotIn("run-record.json", {entry["path"] for entry in document["files"]})
            self.assertEqual(RUNNER.evidence_manifest(archive)["sha256"], manifest["sha256"])


class RecordValidationTests(unittest.TestCase):
    def test_missing_measurements_have_an_explicit_error(self):
        record = success_record()
        record["metrics"]["unavailable_reasons"] = [
            "preview:uid-1:cpu coverage is incomplete or unavailable"
        ]
        record["valid"] = False
        record["exclusion_reason"] = "required measurements are unavailable"

        validation = RUNNER.validate_record(record)

        self.assertIn("required measurements are missing or incomplete", validation["errors"])

    def test_incomplete_success_lifecycle_has_an_explicit_error(self):
        record = success_record()
        record["lifecycle"]["missing_stages"] = {
            "deploy": "stage output was not downloaded"
        }
        record["valid"] = False
        record["exclusion_reason"] = "lifecycle record is incomplete"

        validation = RUNNER.validate_record(record)

        self.assertTrue(
            any(
                error == "lifecycle record is incomplete: deploy"
                for error in validation["errors"]
            )
        )

    def test_complete_success_lifecycle_record_validates(self):
        record = success_record()

        validation = RUNNER.validate_record(record)

        self.assertEqual(validation["errors"], [])
        self.assertTrue(record["valid"])

    def test_close_success_record_allows_missing_build_identity(self):
        record = success_record()
        record.update(
            {
                "run_id": "podinfo-sclose-c1-n1-20261009T000000Z",
                "scenario": "close",
                "source_commit_sha": "unavailable",
                "image_digest": "unavailable",
                "final_served_sha": "unavailable",
                "cleanup": {
                    "namespace_uid": "uid-1",
                    "uid_guarded": True,
                    "namespace_absent": True,
                    "related_resources_absent": True,
                    "orphan_scope": "namespace",
                    "unavailable_reason": "",
                },
                "lifecycle": {
                    "expected_stages": ["event", "cleanup", "report"],
                    "observed_stages": ["event", "cleanup", "report"],
                    "missing_stages": {},
                },
            }
        )

        validation = RUNNER.validate_record(record)

        self.assertEqual(validation["errors"], [])
        self.assertTrue(record["valid"])


class UnsupportedRecordTests(unittest.TestCase):
    def test_unsupported_record_is_written_without_dispatching(self):
        reason = "fixture does not support the requested scenario"
        spec = {
            "run_id": "podinfo-sclose-c1-n1-20261009T000000Z",
            "scenario": "close",
            "repetition": 1,
            "fixture": "podinfo",
            "fixture_config": {
                "name": "podinfo",
                "source_commit_sha": "unavailable",
                "image_digest": "unavailable",
            },
            "conditions": {
                "project_size": "small",
                "build_policy": "rebuild",
                "metric_interval_seconds": 10,
                "operation_timeout_seconds": 300,
                "http_timeout_seconds": 60,
                "concurrency": 1,
            },
        }

        with tempfile.TemporaryDirectory() as temporary:
            result = RUNNER.run_one({}, spec, Path(temporary), object(), reason)
            archive = Path(result["archive"])
            record = json.loads((archive / "run-record.json").read_text(encoding="utf-8"))

            self.assertFalse(result["valid"])
            self.assertEqual(record["outcome"], "unsupported")
            self.assertFalse(record["valid"])
            self.assertEqual(record["exclusion_reason"], reason)
            self.assertEqual(
                json.loads((archive / "unsupported.json").read_text(encoding="utf-8")),
                {"reason": reason},
            )
            self.assertEqual(RUNNER.validate_record(record)["errors"], [])


if __name__ == "__main__":
    unittest.main()
