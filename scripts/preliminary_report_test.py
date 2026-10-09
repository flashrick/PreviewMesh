#!/usr/bin/env python3
"""Focused tests for the preliminary evaluation report."""

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "preliminary_report", ROOT / "scripts" / "preliminary_report.py"
)
REPORT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(REPORT)


class PreliminaryReportTests(unittest.TestCase):
    def test_report_covers_four_areas_and_bounded_missing_runs(self):
        summary = {
            "selection": {"records_found": 9},
            "success_rate": {
                "lifecycle": {"availability": "observed", "numerator": 6, "denominator": 6},
                "evaluated": {"availability": "unavailable", "numerator": 0, "denominator": 0},
            },
            "recovery_time_seconds": {"availability": "unavailable"},
            "http": {
                "verification_stages": {"attempted": 4, "success": 4, "failure": 0},
                "failed_requests": {"availability": "unavailable", "value": None},
            },
            "project_sizes": [
                {"project_size": "simple", "unsupported": 0},
                {"project_size": "multi-service", "unsupported": 1},
            ],
            "resources": {"by_scope": [], "unsupported": ["pvc_storage_unavailable"]},
            "concurrency": {
                "by_level": [
                    {"level": 1, "status": "observed"},
                    {"level": 5, "status": "unsupported"},
                    {"level": 10, "status": "not_run"},
                    {"level": 20, "status": "not_run"},
                ]
            },
            "cleanup": {
                "confirmed_absent": {"value": 2},
                "duplicate_resources": {"availability": "unavailable"},
                "orphan_resources": {"availability": "unavailable"},
            },
            "limitations": ["capacity_unavailable"],
        }
        audit = {
            "records_found": 11,
            "status_counts": {"complete_with_limitations": 6, "unsupported_record": 5},
            "reason_category_counts": {
                "kubernetes_events_unavailable": 6,
                "measurement_unavailable": 6,
            },
        }

        report = REPORT.build_report(summary, audit)

        self.assertEqual(
            set(report["areas"]),
            {"feedback_speed", "reliability", "resource_efficiency", "cleanup_completeness"},
        )
        conditions = {item["condition"] for item in report["missing_runs_for_week13"]}
        self.assertIn("concurrency level 5", conditions)
        self.assertIn("project size multi-service", conditions)
        self.assertIn("failure and recovery repetitions", conditions)
        self.assertEqual(report["coverage"], {"records_summarised": 9, "records_audited": 11})
        self.assertNotIn("run-id", json.dumps(report))

    def test_report_rejects_non_object_inputs(self):
        with self.assertRaises(REPORT.ReportError):
            REPORT.build_report([], {})


if __name__ == "__main__":
    unittest.main()
