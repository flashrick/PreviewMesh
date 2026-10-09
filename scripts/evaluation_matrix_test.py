#!/usr/bin/env python3
"""Focused tests for evaluation matrix validation."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evaluation_matrix", ROOT / "scripts" / "evaluation_matrix.py")
MATRIX = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MATRIX)


SHA = "a" * 40


def base_matrix():
    return {
        "schema_version": MATRIX.SCHEMA_VERSION,
        "revision": {
            "public_revision": SHA,
            "control_revision": "unavailable",
            "cluster_version": "unavailable",
            "image_storage_method": "ghcr",
        },
        "planned_concurrency_levels": [1, 5, 10, 20],
        "defaults": {
            "build_policy": "rebuild",
            "metric_interval_seconds": 10,
            "operation_timeout_seconds": 300,
            "http_timeout_seconds": 60,
        },
        "fixtures": [{"name": "simple", "project_size": "simple", "source_commit_sha": SHA}],
        "conditions": [{
            "id": "baseline", "fixture": "simple", "project_size": "simple", "scenario": "create",
            "commit_version": SHA, "concurrency": 1, "failure_type": "none", "repetitions": 3,
            "build_policy": "rebuild", "support": "supported",
        }],
    }


class MatrixTests(unittest.TestCase):
    def test_baseline_matrix_is_valid_with_warnings_for_unrun_levels(self):
        result = MATRIX.validate_matrix(base_matrix())
        self.assertTrue(result["valid"])
        self.assertEqual(result["supported_condition_count"], 1)
        self.assertTrue(any("concurrency 5" in warning for warning in result["warnings"]))

    def test_supported_condition_requires_three_repetitions_and_commit(self):
        matrix = base_matrix()
        matrix["conditions"][0]["repetitions"] = 2
        matrix["conditions"][0]["commit_version"] = "unavailable"
        result = MATRIX.validate_matrix(matrix)
        self.assertFalse(result["valid"])
        self.assertIn("condition baseline needs at least 3 repetitions", result["errors"])
        self.assertIn("condition baseline needs an immutable commit_version", result["errors"])

    def test_policy_mixing_in_a_comparison_group_is_rejected(self):
        matrix = base_matrix()
        matrix["conditions"].append({
            **matrix["conditions"][0],
            "id": "baseline-cache",
            "build_policy": "reuse-cache",
        })
        result = MATRIX.validate_matrix(matrix)
        self.assertFalse(result["valid"])
        self.assertTrue(any("mixes build policies" in error for error in result["errors"]))

    def test_unsupported_condition_requires_reason(self):
        matrix = base_matrix()
        matrix["conditions"].append({
            "id": "unsupported", "fixture": "simple", "project_size": "simple", "scenario": "create",
            "commit_version": "unavailable", "concurrency": 1, "failure_type": "unsupported",
            "repetitions": 0, "build_policy": "rebuild", "support": "unsupported",
        })
        result = MATRIX.validate_matrix(matrix)
        self.assertFalse(result["valid"])
        self.assertIn("unsupported condition unsupported needs unsupported_reason", result["errors"])


if __name__ == "__main__":
    unittest.main()
