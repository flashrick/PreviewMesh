#!/usr/bin/env python3
"""Focused tests for the privacy-safe evaluation summary."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluation_summary", ROOT / "scripts" / "evaluation_summary.py"
)
SUMMARY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SUMMARY)


def record(
    run_id,
    scenario="create",
    outcome="success",
    valid=False,
    evidence_hash="a" * 64,
    concurrency=1,
    project_size="simple",
    http_result="success",
    cleanup=None,
    timeline=None,
    execution=None,
    capacity=None,
    metrics_window=None,
):
    document = {
        "run_id": run_id,
        "scenario": scenario,
        "outcome": outcome,
        "valid": valid,
        "evidence": {"sha256": evidence_hash},
        "conditions": {"concurrency": concurrency, "project_size": project_size},
        "stage_outputs": (
            [
                {
                    "stage": "http_verify",
                    "result": http_result,
                }
            ]
            if http_result is not None
            else []
        ),
        "timeline": timeline or {},
        "cleanup": cleanup or {},
        "metrics": {
            "unavailable_reasons": [],
            "storage_boundaries": {
                "host_disk_bytes": {
                    "unit": "byte",
                    "scope": "cluster",
                    "availability": "unsupported",
                    "reason": "host filesystem measurement requires a separate collector",
                }
            },
            "measurements": {
                "preview:uid-1:cpu": {
                    "available": True,
                    "unit": "core_minute",
                    "value": 1.5,
                    "covered_seconds": 20,
                }
            },
        },
    }
    if execution is not None:
        document["execution"] = execution
    if capacity is not None:
        document["metrics"]["baseline_kind"] = "idle"
        document["metrics"]["cluster_capacity"] = capacity
    if metrics_window is not None:
        document["metrics"]["metrics_window"] = metrics_window
    return document


def write_record(root, name, document):
    archive = root / name
    archive.mkdir()
    (archive / "run-record.json").write_text(
        json.dumps(document) + "\n",
        encoding="utf-8",
    )
    return archive


class EvaluationSummaryTests(unittest.TestCase):
    def test_summary_separates_lifecycle_and_strict_success_rates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "create", record("run-create"))
            close = write_record(
                root,
                "close",
                record(
                    "run-close",
                    scenario="close",
                    http_result=None,
                    cleanup={
                        "namespace_absent": True,
                        "related_resources_absent": True,
                    },
                ),
            )
            (close / "summary.json").write_text(
                json.dumps(
                    {
                        "scopes": {},
                        "samples": 3,
                        "max_observed_running_preview_count": 2,
                        "max_observed_managed_namespace_count": 2,
                    }
                ),
                encoding="utf-8",
            )

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["selection"]["records_considered"], 2)
            self.assertEqual(summary["success_rate"]["lifecycle"]["numerator"], 2)
            self.assertEqual(summary["success_rate"]["lifecycle"]["denominator"], 2)
            self.assertEqual(summary["success_rate"]["evaluated"]["availability"], "unavailable")
            self.assertEqual(summary["cleanup"]["confirmed_absent"]["value"], 1)
            self.assertEqual(summary["concurrency"]["observed_running_high_water"], 2.0)
            self.assertNotIn("uid-1", json.dumps(summary))

    def test_summary_counts_recovery_and_request_level_http_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failure = write_record(
                root,
                "failure",
                record(
                    "run-failure",
                    scenario="failure",
                    outcome="failure",
                    timeline={
                        "failure_observed_at_utc": "2026-10-09T00:00:01Z",
                        "recovery_verified_at_utc": "2026-10-09T00:00:04Z",
                    },
                    http_result="failure",
                ),
            )
            (failure / "http-polls.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"status_code": 200, "status_ok": True}),
                        json.dumps({"status_code": 404, "status_ok": False}),
                        json.dumps({"http_error": True}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["recovery_time_seconds"]["value"], 3.0)
            self.assertEqual(summary["http"]["verification_stages"]["failure"], 1)
            self.assertEqual(summary["http"]["failed_requests"]["value"], 2)
            self.assertEqual(summary["http"]["failed_requests"]["availability"], "observed")

    def test_missing_request_and_inventory_data_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(
                root,
                "create",
                record("run-create", http_result=None),
            )

            summary = SUMMARY.summarize([root])

            self.assertIsNone(summary["http"]["failed_requests"]["value"])
            self.assertEqual(summary["http"]["failed_requests"]["availability"], "unavailable")
            self.assertIsNone(summary["cleanup"]["orphan_resources"]["value"])
            self.assertEqual(summary["cleanup"]["orphan_resources"]["availability"], "unavailable")
            self.assertEqual(summary["concurrency"]["capacity_availability"], "unavailable")
            self.assertEqual(
                summary["resources"]["storage_boundaries"]["host_disk_bytes"]["availability"],
                "unsupported",
            )

    def test_summary_separates_capacity_from_preview_high_water(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(
                root,
                "capacity",
                record(
                    "run-capacity",
                    capacity={
                        "availability": "observed",
                        "allocatable_cpu_cores": {"minimum": 2, "maximum": 2, "unit": "core"},
                        "allocatable_memory_bytes": {
                            "minimum": 4 * 1024 ** 3,
                            "maximum": 4 * 1024 ** 3,
                            "unit": "byte",
                        },
                    },
                ),
            )

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["concurrency"]["capacity_availability"], "observed")
            self.assertEqual(summary["concurrency"]["capacity"]["baseline_kinds"], ["idle"])
            self.assertEqual(summary["resources"]["cluster_capacity"]["allocatable_cpu_cores"]["maximum"], 2.0)

    def test_summary_reports_resource_peaks_and_window_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = record(
                "run-window",
                metrics_window={
                    "availability": "observed",
                    "observed_duration_seconds": 20,
                    "actual_interval_seconds": {"minimum": 9, "maximum": 11, "mean": 10},
                    "gap_count": 0,
                },
            )
            document["metrics"]["measurements"]["preview:uid-1:cpu"]["maximum"] = 3.5
            write_record(root, "window", document)

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["resources"]["metrics_window"]["availability"], "observed")
            self.assertEqual(summary["resources"]["metrics_window"]["actual_interval_seconds"]["maximum"], 11.0)
            resource = next(item for item in summary["resources"]["by_scope"] if item["metric"] == "cpu")
            self.assertEqual(resource["peak_value"], 3.5)
            self.assertEqual(resource["peak_availability"], "observed")

    def test_zero_failed_requests_remains_an_observed_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = write_record(root, "create", record("run-create", http_result=None))
            (archive / "http-polls.jsonl").write_text(
                json.dumps({"status_code": 200, "status_ok": True}) + "\n",
                encoding="utf-8",
            )

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["http"]["failed_requests"]["value"], 0)
            self.assertEqual(summary["http"]["failed_requests"]["availability"], "observed")
            self.assertNotIn("request_level_http_unavailable", summary["limitations"])

    def test_external_application_test_stage_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unavailable = record("run-without-test")
            unavailable["test_stage"] = {
                "owner": "source-ci",
                "execution": "external",
                "availability": "unavailable",
                "result": "not_run",
                "unavailable_reason": "source CI artifact was not supplied",
            }
            observed = record("run-with-test")
            observed["test_stage"] = {
                "owner": "source-ci",
                "execution": "external",
                "availability": "observed",
                "result": "success",
                "started_at_utc": "2026-10-09T00:00:00Z",
                "ended_at_utc": "2026-10-09T00:00:01Z",
                "duration_seconds": 1.0,
            }
            write_record(root, "without-test", unavailable)
            write_record(root, "with-test", observed)

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["testing"]["owner"], "source-ci")
            self.assertEqual(summary["testing"]["availability"], "partial")
            self.assertEqual(summary["testing"]["observed"], 1)
            self.assertEqual(summary["testing"]["failed"], 0)

    def test_same_run_identity_is_counted_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "first", record("run-same"))
            write_record(root, "duplicate", record("run-same"))

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["selection"]["records_found"], 2)
            self.assertEqual(summary["selection"]["records_considered"], 1)
            self.assertEqual(summary["selection"]["duplicates_removed"], 1)

    def test_project_size_breakdown_keeps_unsupported_out_of_success_rate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "simple", record("run-simple"))
            write_record(
                root,
                "multi",
                record(
                    "run-multi",
                    project_size="multi-service",
                    outcome="unsupported",
                    valid=False,
                    http_result=None,
                ),
            )
            unsupported_path = root / "multi" / "run-record.json"
            unsupported = json.loads(unsupported_path.read_text(encoding="utf-8"))
            unsupported["exclusion_reason"] = "multi-service support is not established"
            unsupported_path.write_text(json.dumps(unsupported) + "\n", encoding="utf-8")

            summary = SUMMARY.summarize([root])

            sizes = {item["project_size"]: item for item in summary["project_sizes"]}
            self.assertEqual(sizes["simple"]["lifecycle_success"], 1)
            self.assertEqual(sizes["multi-service"]["unsupported"], 1)
            self.assertEqual(
                sizes["multi-service"]["unsupported_reasons"],
                ["multi_service_not_supported"],
            )
            self.assertEqual(summary["success_rate"]["lifecycle"]["denominator"], 1)
            self.assertIn("unsupported_project_size", summary["limitations"])

    def test_concurrency_breakdown_keeps_unrun_levels_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "level-one", record("run-one", concurrency=1))
            write_record(
                root,
                "level-five",
                record(
                    "run-five",
                    concurrency=5,
                    outcome="unsupported",
                    valid=False,
                    http_result=None,
                ),
            )

            summary = SUMMARY.summarize([root])
            levels = {item["level"]: item for item in summary["concurrency"]["by_level"]}

            self.assertEqual(levels[1]["status"], "observed")
            self.assertEqual(levels[1]["lifecycle_success"], 1)
            self.assertEqual(levels[5]["status"], "unsupported")
            self.assertEqual(levels[10]["status"], "not_run")
            self.assertEqual(levels[20]["status"], "not_run")
            self.assertEqual(summary["success_rate"]["lifecycle"]["denominator"], 1)

    def test_concurrency_summary_reports_observed_cohort_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(
                root,
                "first",
                record(
                    "run-first",
                    concurrency=5,
                    execution={
                        "cohort_id": "cohort-test",
                        "availability": "observed",
                        "started_at_utc": "2026-10-09T00:00:00Z",
                        "ended_at_utc": "2026-10-09T00:00:03Z",
                    },
                ),
            )
            write_record(
                root,
                "second",
                record(
                    "run-second",
                    concurrency=5,
                    execution={
                        "cohort_id": "cohort-test",
                        "availability": "observed",
                        "started_at_utc": "2026-10-09T00:00:01Z",
                        "ended_at_utc": "2026-10-09T00:00:04Z",
                    },
                ),
            )

            summary = SUMMARY.summarize([root])

            self.assertEqual(summary["concurrency"]["actual_overlap"], 2)
            self.assertEqual(summary["concurrency"]["actual_overlap_availability"], "observed")
            level = next(
                item for item in summary["concurrency"]["by_level"] if item["level"] == 5
            )
            self.assertEqual(level["actual_overlap"], 2)

    def test_concurrency_summary_keeps_overlap_unavailable_without_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_record(root, "run", record("run-without-window", concurrency=5))

            summary = SUMMARY.summarize([root])

            self.assertIsNone(summary["concurrency"]["actual_overlap"])
            self.assertEqual(summary["concurrency"]["actual_overlap_availability"], "unavailable")


if __name__ == "__main__":
    unittest.main()
