#!/usr/bin/env python3
"""Validate a versioned PreviewMesh evaluation matrix without contacting a cluster."""

import argparse
import json
from pathlib import Path
import re
import sys


SCHEMA_VERSION = "previewmesh-matrix-v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PROJECT_SIZES = {"simple", "frontend-backend", "multi-service", "unknown"}
SCENARIOS = {"create", "update", "close", "reopen", "failure"}
FAILURE_TYPES = {"none", "deployment", "http", "cleanup", "unsupported"}
BUILD_POLICIES = {"rebuild", "reuse-cache"}
CONCURRENCY_LEVELS = (1, 5, 10, 20)


class MatrixError(Exception):
    """A safe matrix input error."""


def load(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise MatrixError("could not read matrix JSON") from error


def immutable(value, pattern):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def validate_matrix(matrix):
    errors = []
    warnings = []
    if not isinstance(matrix, dict):
        return {"valid": False, "errors": ["matrix must be an object"], "warnings": []}
    if matrix.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    planned_levels = matrix.get("planned_concurrency_levels")
    if planned_levels != list(CONCURRENCY_LEVELS):
        errors.append("planned_concurrency_levels must be [1, 5, 10, 20]")

    revision = matrix.get("revision")
    if not isinstance(revision, dict):
        errors.append("revision must be an object")
    else:
        for field in ("public_revision", "control_revision", "cluster_version", "image_storage_method"):
            if not revision.get(field):
                errors.append(f"revision missing {field}")
        if revision.get("public_revision") != "unavailable" and not immutable(revision.get("public_revision"), SHA_RE):
            errors.append("revision.public_revision is not an immutable SHA")
        if revision.get("control_revision") != "unavailable" and not immutable(revision.get("control_revision"), SHA_RE):
            errors.append("revision.control_revision is not an immutable SHA")

    defaults = matrix.get("defaults")
    if not isinstance(defaults, dict):
        errors.append("defaults must be an object")
        defaults = {}
    for field in ("build_policy", "metric_interval_seconds", "operation_timeout_seconds", "http_timeout_seconds"):
        if field not in defaults:
            errors.append(f"defaults missing {field}")
    if defaults.get("build_policy") not in BUILD_POLICIES:
        errors.append("defaults.build_policy is invalid")

    fixtures = matrix.get("fixtures")
    fixture_names = set()
    fixture_sizes = {}
    if not isinstance(fixtures, list) or not fixtures:
        errors.append("fixtures must contain at least one fixture")
        fixtures = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            errors.append("each fixture must be an object")
            continue
        name = fixture.get("name")
        if not isinstance(name, str) or not name or name in fixture_names:
            errors.append("fixture names must be unique and non-empty")
            continue
        fixture_names.add(name)
        size = fixture.get("project_size")
        if size not in PROJECT_SIZES:
            errors.append(f"fixture {name} has an invalid project_size")
        fixture_sizes[name] = size
        sha = fixture.get("source_commit_sha")
        if sha != "unavailable" and not immutable(sha, SHA_RE):
            errors.append(f"fixture {name} is missing an immutable source_commit_sha")
        if fixture.get("image_digest") not in (None, "unavailable") and not immutable(fixture.get("image_digest"), DIGEST_RE):
            errors.append(f"fixture {name} has an invalid image_digest")

    conditions = matrix.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        errors.append("conditions must contain at least one condition")
        conditions = []
    condition_ids = set()
    first_level = []
    supported_counts = {}
    policy_groups = {}
    baseline = False
    for condition in conditions:
        if not isinstance(condition, dict):
            errors.append("each condition must be an object")
            continue
        identifier = condition.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in condition_ids:
            errors.append("condition IDs must be unique and non-empty")
            continue
        condition_ids.add(identifier)
        fixture = condition.get("fixture")
        if fixture not in fixture_names:
            errors.append(f"condition {identifier} references an unknown fixture")
        project_size = condition.get("project_size")
        if project_size not in PROJECT_SIZES:
            errors.append(f"condition {identifier} has an invalid project_size")
        if fixture in fixture_sizes and project_size != fixture_sizes[fixture]:
            errors.append(f"condition {identifier} project_size does not match its fixture")
        scenario = condition.get("scenario")
        if scenario not in SCENARIOS:
            errors.append(f"condition {identifier} has an invalid scenario")
        failure_type = condition.get("failure_type")
        if failure_type not in FAILURE_TYPES:
            errors.append(f"condition {identifier} has an invalid failure_type")
        concurrency = condition.get("concurrency")
        if concurrency not in CONCURRENCY_LEVELS:
            errors.append(f"condition {identifier} must use concurrency 1, 5, 10, or 20")
        elif not first_level or first_level[-1] != concurrency:
            first_level.append(concurrency)
        repetitions = condition.get("repetitions")
        support = condition.get("support", "supported")
        if support not in {"supported", "unsupported"}:
            errors.append(f"condition {identifier} has an invalid support value")
        if support == "supported":
            if not isinstance(repetitions, int) or repetitions < 3:
                errors.append(f"condition {identifier} needs at least 3 repetitions")
            commit = condition.get("commit_version")
            if not immutable(commit, SHA_RE):
                errors.append(f"condition {identifier} needs an immutable commit_version")
            supported_counts[concurrency] = supported_counts.get(concurrency, 0) + 1
        else:
            if not isinstance(condition.get("unsupported_reason"), str) or not condition["unsupported_reason"].strip():
                errors.append(f"unsupported condition {identifier} needs unsupported_reason")
        policy = condition.get("build_policy")
        if policy not in BUILD_POLICIES:
            errors.append(f"condition {identifier} has an invalid build_policy")
        group = (fixture, project_size, scenario, failure_type, concurrency)
        policy_groups.setdefault(group, set()).add(policy)
        if concurrency == 1 and scenario == "create" and failure_type == "none" and support == "supported":
            baseline = True
    if first_level != sorted(set(first_level)):
        errors.append("concurrency levels must be listed in ascending order")
    if not baseline:
        errors.append("matrix needs a supported low-concurrency create baseline")
    for group, policies in policy_groups.items():
        if len(policies) > 1:
            errors.append(f"comparison group mixes build policies: {group}")
    for level in CONCURRENCY_LEVELS:
        if level not in supported_counts:
            warnings.append(f"no supported condition is currently assigned to concurrency {level}")
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "condition_count": len(conditions), "supported_condition_count": sum(supported_counts.values()),
            "supported_by_concurrency": supported_counts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_matrix(load(args.matrix))
    except MatrixError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
