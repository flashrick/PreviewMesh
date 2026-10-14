#!/usr/bin/env python3
"""Focused tests for the public failure-scenario contract."""

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "failure_scenarios", ROOT / "scripts" / "failure_scenarios.py"
)
SCENARIOS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCENARIOS)


class FailureScenarioTests(unittest.TestCase):
    def test_public_catalog_is_complete(self):
        with (ROOT / "config" / "failure-scenarios.example.json").open(encoding="utf-8") as stream:
            catalog = json.load(stream)

        result = SCENARIOS.validate_catalog(catalog)

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["scenario_count"], len(SCENARIOS.EXPECTED_SCENARIOS))

    def test_catalog_rejects_unlisted_scenario(self):
        catalog = {
            "schema_version": SCENARIOS.SCHEMA_VERSION,
            "evidence_rule": "evidence required",
            "scenarios": [
                {
                    "id": "unlisted",
                    "trigger_stages": ["deploy"],
                    "recovery_actions": ["cleanup"],
                    "live_evidence_status": "unknown",
                }
            ],
        }

        result = SCENARIOS.validate_catalog(catalog)

        self.assertFalse(result["valid"])
        self.assertTrue(any("unknown failure scenario" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
