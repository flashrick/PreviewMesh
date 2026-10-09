#!/usr/bin/env python3
"""Focused tests for retained-evidence auditing."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evidence_audit", ROOT / "scripts" / "evidence_audit.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AUDIT)


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")


def write_manifest(archive):
    entries = []
    total = 0
    for path in sorted(archive.rglob("*")):
        if not path.is_file() or path.name in {"MANIFEST.json", "run-record.json"}:
            continue
        data = path.read_bytes()
        total += len(data)
        entries.append(
            {
                "path": path.relative_to(archive).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    write_json(archive / "MANIFEST.json", {"files": entries, "total_bytes": total})


def base_record(scenario="create", outcome="success", valid=True):
    return {
        "run_id": "sensitive-run-id-should-not-be-output",
        "scenario": scenario,
        "outcome": outcome,
        "valid": valid,
        "exclusion_reason": "" if valid else "retained evidence is incomplete",
        "stage_outputs": (
            [{"stage": "http_verify", "result": "success"}]
            if scenario != "close" and outcome != "unsupported"
            else []
        ),
        "lifecycle": {
            "missing_stages": {
                "cleanup": "not_applicable for an open preview scenario"
            }
            if scenario == "create"
            else {}
        },
        "cleanup": {
            "namespace_absent": scenario == "close",
            "related_resources_absent": scenario == "close",
            "unavailable_reason": "cleanup is not applicable until the preview is closed"
            if scenario != "close"
            else "",
        },
        "metrics": {"unavailable_reasons": []},
    }


def prepare_supported(root, scenario="create"):
    archive = root / scenario
    archive.mkdir()
    write_json(archive / "conditions.json", {"conditions": {"project_size": "simple"}})
    write_json(archive / "validation.json", {"errors": [], "warnings": []})
    write_json(archive / "workflow.json", {"conclusion": "success"})
    (archive / "workflow.log").write_text("workflow log\n", encoding="utf-8")
    (archive / "combined.csv").write_text("stage,result\nreport,success\n", encoding="utf-8")
    write_json(archive / "artifacts" / "resource-collection" / "summary.json", {"scopes": {}, "samples": 1})
    (archive / "artifacts" / "resource-collection" / "samples.jsonl").write_text("{}\n", encoding="utf-8")
    if scenario == "close":
        write_json(archive / "artifacts" / "cleanup.json", {"cleanup": "confirmed_absent"})
    write_json(archive / "run-record.json", base_record(scenario=scenario))
    write_manifest(archive)
    return archive


class EvidenceAuditTests(unittest.TestCase):
    def test_supported_create_retains_categories_and_reports_unavailable_kubernetes_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = prepare_supported(Path(temporary))

            result = AUDIT.audit([archive])
            record = result["records"][0]

            self.assertEqual(record["audit_status"], "complete_with_limitations")
            self.assertEqual(record["artifact_categories"]["manifest"], "present")
            self.assertEqual(record["artifact_categories"]["resource_evidence"], "present")
            self.assertEqual(record["artifact_categories"]["http_results"], "present")
            self.assertEqual(record["lifecycle"]["status"], "complete")
            self.assertIn("kubernetes_events_unavailable", record["missing_or_unavailable_reason_categories"])
            self.assertNotIn("sensitive-run-id", json.dumps(result))

    def test_close_marks_http_not_applicable_and_requires_cleanup_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = prepare_supported(Path(temporary), scenario="close")

            record = AUDIT.audit([archive])["records"][0]

            self.assertEqual(record["artifact_categories"]["http_results"], "not_applicable")
            self.assertEqual(record["artifact_categories"]["cleanup_evidence"], "present")
            self.assertEqual(record["lifecycle"]["status"], "complete")

    def test_unsupported_record_is_not_dispatched_and_needs_no_workflow_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsupported"
            archive.mkdir()
            write_json(archive / "conditions.json", {"conditions": {"concurrency": 5}})
            write_json(archive / "validation.json", {"errors": [], "warnings": []})
            write_json(archive / "unsupported.json", {"reason": "fixture not registered"})
            unsupported = base_record(outcome="unsupported", valid=False)
            write_json(archive / "run-record.json", unsupported)
            write_manifest(archive)

            record = AUDIT.audit([archive])["records"][0]

            self.assertEqual(record["audit_status"], "unsupported_record")
            self.assertEqual(record["workflow_dispatch"], "not_dispatched")
            self.assertEqual(record["artifact_categories"]["workflow_raw_log"], "not_applicable")
            self.assertTrue(record["exclusion_reason_present"])

    def test_manifest_mismatch_is_retained_as_audit_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = prepare_supported(Path(temporary))
            (archive / "workflow.log").write_text("changed\n", encoding="utf-8")

            record = AUDIT.audit([archive])["records"][0]

            self.assertEqual(record["artifact_categories"]["manifest"], "invalid")
            self.assertEqual(record["audit_status"], "incomplete_record")
            self.assertIn("manifest_integrity", record["missing_or_unavailable_reason_categories"])


if __name__ == "__main__":
    unittest.main()
