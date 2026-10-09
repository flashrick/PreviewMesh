#!/usr/bin/env python3
"""Focused tests for the reproducible analysis report."""

import importlib.util
import json
from math import sqrt
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analysis_report", ROOT / "scripts" / "analysis_report.py"
)
REPORT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(REPORT)


SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
EVIDENCE_HASH = "c" * 64
TIMESTAMP = "2026-10-09T00:00:00Z"
SCHEMA_VERSION = "previewmesh-run-v1"
TIMING_SCHEMA_VERSION = 1
TIMELINE_FIELDS = (
    "event_received_at_utc",
    "deployment_started_at_utc",
    "readiness_started_at_utc",
    "http_verification_started_at_utc",
    "failure_observed_at_utc",
    "recovery_started_at_utc",
    "recovery_verified_at_utc",
    "final_verify_at_utc",
    "cleanup_started_at_utc",
    "cleanup_absent_at_utc",
)


def _timeline(outcome):
    fields = {field: None for field in TIMELINE_FIELDS}
    reasons = {field: "not captured by this fixture" for field in fields}
    fields["event_received_at_utc"] = TIMESTAMP
    fields["deployment_started_at_utc"] = TIMESTAMP
    fields["readiness_started_at_utc"] = TIMESTAMP
    fields["http_verification_started_at_utc"] = TIMESTAMP
    fields["final_verify_at_utc"] = TIMESTAMP
    if outcome == "failure":
        fields["failure_observed_at_utc"] = "2026-10-09T00:00:02Z"
        fields["recovery_started_at_utc"] = "2026-10-09T00:00:04Z"
        fields["recovery_verified_at_utc"] = "2026-10-09T00:00:05Z"
    return fields, reasons


def _timing_entry(availability="unavailable", duration=None):
    if availability == "observed":
        return {
            "started_at_utc": TIMESTAMP,
            "ended_at_utc": "2026-10-09T00:00:01Z",
            "duration_seconds": duration,
            "availability": "observed",
            "result": "success",
            "raw_stage": "test",
            "source": "test",
        }
    return {
        "started_at_utc": None,
        "ended_at_utc": None,
        "duration_seconds": None,
        "availability": availability,
        "result": "not_applicable" if availability == "not_applicable" else "unavailable",
        "unavailable_reason": "not captured by this fixture",
    }


def _timings(
    total_feedback=1.0,
    build_availability="unavailable",
    build_seconds=None,
    scheduling_availability="unavailable",
):
    phases = {
        phase: [_timing_entry()]
        for phase in REPORT.TIMING_PHASES
    }
    phases["total_feedback"] = [_timing_entry("observed", total_feedback)]
    phases["build"] = [_timing_entry(build_availability, build_seconds)]
    phases["scheduling"] = [_timing_entry(scheduling_availability)]
    return {"schema_version": TIMING_SCHEMA_VERSION, "phases": phases}


def run_record(
    run_id,
    *,
    outcome="success",
    evidence_hash=EVIDENCE_HASH,
    total_feedback=1.0,
    build_availability="unavailable",
    build_seconds=None,
    scheduling_availability="unavailable",
):
    timeline, unavailable_reasons = _timeline(outcome)
    unsupported = outcome == "unsupported"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scenario": "create",
        "repetition": 1,
        "fixture": "fixture",
        "source_commit_sha": "unavailable" if unsupported else SOURCE_SHA,
        "image_digest": "unavailable" if unsupported else IMAGE_DIGEST,
        "conditions": {
            "project_size": "simple",
            "commit_version": SOURCE_SHA,
            "concurrency": 1,
            "build_policy": "rebuild",
            "failure_type": "none",
            "metric_interval_seconds": 10,
            "operation_timeout_seconds": 300,
            "http_timeout_seconds": 60,
        },
        "timeline": timeline,
        "timeline_unavailable_reasons": unavailable_reasons,
        "state": {
            "desired": {"status": "ready"},
            "observed": {"status": "ready"},
            "action": {"status": "deployed"},
            "final": {"status": "ready"},
        },
        "outcome": outcome,
        "failure_trigger": "deploy" if outcome == "failure" else "unsupported" if unsupported else "none",
        "recovery_mode": "manual" if outcome == "failure" else "unavailable" if unsupported else "none",
        "final_served_sha": "unavailable" if unsupported else SOURCE_SHA,
        "cleanup": {
            "namespace_uid": "unavailable",
            "uid_guarded": None,
            "namespace_absent": None,
            "related_resources_absent": None,
            "orphan_scope": "unavailable",
            "unavailable_reason": "cleanup is not applicable until the preview is closed",
        },
        "metrics": {
            "units": {"cpu": "core"},
            "coverage_seconds": {},
            "unavailable_reasons": [],
            "measurements": {},
        },
        "stage_outputs": [],
        "timings": _timings(
            total_feedback,
            build_availability,
            build_seconds,
            scheduling_availability,
        ),
        "lifecycle": {
            "expected_stages": ["event", "build", "deploy", "readiness", "http_verify", "report"],
            "observed_stages": [],
            "missing_stages": {"cleanup": "not_applicable for an open preview scenario"},
        },
        "workflow": {"run_id": "workflow", "url": "", "conclusion": "success"},
        "evidence": {"path": "private-evidence", "bytes": 0, "sha256": evidence_hash},
        "valid": not unsupported,
        "exclusion_reason": "unsupported fixture" if unsupported else "",
    }


def write_record(root, name, document):
    archive = root / name
    archive.mkdir()
    (archive / "run-record.json").write_text(
        json.dumps(document) + "\n", encoding="utf-8"
    )
    return archive


def row(report, metric):
    return next(item for item in report["tables"]["reliability"] if item["metric"] == metric)


class AnalysisReportTests(unittest.TestCase):
    def test_quantiles_and_sample_standard_deviation_use_documented_rules(self):
        stats = REPORT.descriptive_stats([1, 2, 3, 4, 5], small_sample_threshold=5)

        self.assertEqual(stats["sample_count"], 5)
        self.assertEqual(stats["median"], 3.0)
        self.assertAlmostEqual(stats["p90"], 4.6)
        self.assertAlmostEqual(stats["p95"], 4.8)
        self.assertAlmostEqual(stats["p99"], 4.96)
        self.assertEqual(stats["maximum"], 5.0)
        self.assertAlmostEqual(stats["mean"], 3.0)
        self.assertAlmostEqual(stats["stddev"], sqrt(2.5))
        self.assertFalse(stats["small_sample"])

    def test_failed_runs_remain_included_in_reliability_statistics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "success", run_record("run-success"))
            write_record(
                root,
                "failure",
                run_record("run-failure", outcome="failure"),
            )

            report = REPORT.analyze([root], small_sample_threshold=1)

            self.assertEqual(report["selection"]["records_included"], 2)
            self.assertEqual(report["selection"]["failed_runs_included"], 1)
            success_rate = row(report, "lifecycle_success_rate")
            self.assertEqual(success_rate["sample_count"], 2)
            self.assertEqual(success_rate["available_count"], 2)
            self.assertAlmostEqual(success_rate["mean"], 0.5)
            recovery = row(report, "recovery_time")
            self.assertEqual(recovery["available_count"], 1)
            self.assertEqual(recovery["mean"], 3.0)

    def test_unsupported_records_are_reported_and_excluded_from_metric_cohorts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "supported", run_record("run-supported"))
            write_record(
                root,
                "unsupported",
                run_record("run-unsupported", outcome="unsupported"),
            )

            report = REPORT.analyze([root], small_sample_threshold=1)

            self.assertEqual(report["selection"]["validated_records"], 2)
            self.assertEqual(report["selection"]["records_included"], 1)
            self.assertEqual(report["selection"]["excluded"], {"unsupported": 1})
            timing = next(
                item
                for item in report["tables"]["timing_statistics"]
                if item["metric"] == "phase:total_feedback"
            )
            self.assertEqual(timing["sample_count"], 1)

    def test_missing_and_not_applicable_values_remain_explicit(self):
        missing = REPORT.descriptive_stats(
            [2], sample_count=3, not_applicable_count=1, small_sample_threshold=2
        )
        not_applicable = REPORT.descriptive_stats(
            [], sample_count=2, not_applicable_count=2, small_sample_threshold=1
        )

        self.assertEqual(missing["availability"], "partial")
        self.assertEqual(missing["missing_count"], 1)
        self.assertEqual(missing["not_applicable_count"], 1)
        self.assertEqual(not_applicable["availability"], "not_applicable")
        self.assertEqual(not_applicable["available_count"], 0)
        self.assertIsNone(not_applicable["mean"])

    def test_small_sample_flag_and_warning_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "single", run_record("run-single"))

            report = REPORT.analyze([root], small_sample_threshold=2)

            timing = next(
                item
                for item in report["tables"]["timing_statistics"]
                if item["metric"] == "phase:total_feedback"
            )
            self.assertTrue(timing["small_sample"])
            self.assertTrue(any("phase:total_feedback" in item for item in report["warnings"]))

    def test_report_outputs_are_complete_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(
                root,
                "single",
                run_record(
                    "sensitive-run-id",
                    build_availability="observed",
                    build_seconds=2.0,
                    scheduling_availability="not_applicable",
                ),
            )
            report = REPORT.analyze([root], small_sample_threshold=1)
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            REPORT.write_outputs(report, first)
            REPORT.write_outputs(report, second)

            relative_files = (
                "analysis.json",
                "timing-statistics.csv",
                "comparisons.csv",
                "resource-statistics.csv",
                "reliability.csv",
                "report.md",
                "charts/timing-phases.svg",
                "charts/project-size-total-feedback.svg",
                "charts/concurrency-total-feedback.svg",
            )
            for relative in relative_files:
                first_path = first / relative
                second_path = second / relative
                self.assertTrue(first_path.is_file(), relative)
                self.assertEqual(first_path.read_bytes(), second_path.read_bytes(), relative)

            document = json.loads((first / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], REPORT.ANALYSIS_SCHEMA_VERSION)
            self.assertNotIn("sensitive-run-id", json.dumps(document))
            self.assertIn("phase:build", (first / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
