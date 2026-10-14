#!/usr/bin/env python3
"""Validate the public, credential-free failure-scenario contract."""

import argparse
import json
from pathlib import Path
import sys


SCHEMA_VERSION = "previewmesh-failure-scenarios-v1"
EXPECTED_SCENARIOS = {
    "deployment",
    "http",
    "cleanup",
    "missing-resource",
    "endpoint-refused",
    "pod-crash",
    "worker-restart",
    "control-plane-outage",
    "hosted-pr-update",
}
LIVE_EVIDENCE_STATUSES = {"observed", "unknown", "unsupported"}


class FailureScenarioError(Exception):
    """A safe failure-scenario input error."""


def load(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise FailureScenarioError("could not read failure-scenario JSON") from error


def validate_catalog(catalog):
    errors = []
    if not isinstance(catalog, dict):
        return {"valid": False, "errors": ["catalog must be an object"]}
    if catalog.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    if not isinstance(catalog.get("evidence_rule"), str) or not catalog["evidence_rule"].strip():
        errors.append("evidence_rule is required")
    scenarios = catalog.get("scenarios")
    if not isinstance(scenarios, list):
        errors.append("scenarios must be a list")
        scenarios = []
    identifiers = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            errors.append("each scenario must be an object")
            continue
        identifier = scenario.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            errors.append("scenario IDs must be unique and non-empty")
            continue
        identifiers.add(identifier)
        if identifier not in EXPECTED_SCENARIOS:
            errors.append(f"unknown failure scenario {identifier}")
        for field in ("trigger_stages", "recovery_actions"):
            values = scenario.get(field)
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                errors.append(f"scenario {identifier} needs non-empty {field}")
        if scenario.get("live_evidence_status") not in LIVE_EVIDENCE_STATUSES:
            errors.append(f"scenario {identifier} has an invalid live_evidence_status")
    missing = EXPECTED_SCENARIOS - identifiers
    if missing:
        errors.append("catalog is missing scenarios: " + ", ".join(sorted(missing)))
    return {"valid": not errors, "errors": errors, "scenario_count": len(scenarios)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_catalog(load(args.catalog))
    except FailureScenarioError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
