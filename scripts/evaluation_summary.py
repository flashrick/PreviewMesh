#!/usr/bin/env python3
"""Summarise selected private PreviewMesh run records without exposing identities."""

import argparse
import json
from pathlib import Path
import sys


SUMMARY_SCHEMA_VERSION = "previewmesh-evaluation-summary-v1"
KNOWN_METRIC_NAMES = {
    "cpu",
    "memory",
    "host_disk_bytes",
    "filesystem_usage_bytes",
    "registry_image_bytes",
    "helm_release_payload_bytes",
    "helm_history_count",
    "pvc_requested_bytes",
    "pvc_capacity_bytes",
    "resource_minutes",
}


class SummaryError(Exception):
    """A safe, user-facing summary error."""


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise SummaryError("could not read JSON evidence") from error


def find_records(paths):
    found = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file() and path.name == "run-record.json":
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(path.rglob("run-record.json")))
    unique = []
    seen = set()
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "ok"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    return None


def _parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _metric(value, unit, availability, reason=None, **extra):
    result = {
        "value": value,
        "unit": unit,
        "availability": availability,
    }
    result.update(extra)
    if reason:
        result["reason"] = reason
    return result


def _reason_category(reason):
    """Reduce an evidence reason to a non-identifying category."""
    lowered = str(reason or "").lower()
    if "helm" in lowered:
        return "helm_storage_unavailable"
    if "pvc" in lowered:
        return "pvc_storage_unavailable"
    if "registry" in lowered or "image" in lowered:
        return "image_storage_unavailable"
    if "host" in lowered or "filesystem" in lowered:
        return "host_storage_unavailable"
    if "cleanup" in lowered:
        return "cleanup_evidence_unavailable"
    if "timing" in lowered or "stage" in lowered:
        return "stage_timing_unavailable"
    return "measurement_unavailable"


def _project_size_reason(reason):
    lowered = str(reason or "").lower()
    if "front" in lowered or "backend" in lowered:
        return "frontend_backend_not_supported"
    if "multi" in lowered or "service" in lowered:
        return "multi_service_not_supported"
    if "register" in lowered or "fixture" in lowered:
        return "fixture_not_registered"
    return "unsupported_condition"


def _project_size_summary(records):
    groups = {}
    for _, record in records:
        size = (record.get("conditions") or {}).get("project_size") or "unknown"
        group = groups.setdefault(
            size,
            {
                "records": 0,
                "lifecycle_success": 0,
                "unsupported": 0,
                "valid": 0,
                "unsupported_reasons": set(),
            },
        )
        group["records"] += 1
        if record.get("outcome") == "success":
            group["lifecycle_success"] += 1
        if record.get("outcome") == "unsupported":
            group["unsupported"] += 1
            group["unsupported_reasons"].add(
                _project_size_reason(record.get("exclusion_reason"))
            )
        if record.get("valid") is True:
            group["valid"] += 1
    result = []
    for size in sorted(groups):
        group = groups[size]
        result.append(
            {
                "project_size": size,
                "records": group["records"],
                "lifecycle_success": group["lifecycle_success"],
                "unsupported": group["unsupported"],
                "strict_valid": group["valid"],
                "unsupported_reasons": sorted(group["unsupported_reasons"]),
            }
        )
    return result


def _record_entries(paths):
    entries = []
    by_identity = {}
    duplicate_count = 0
    conflict_count = 0
    for path in find_records(paths):
        try:
            record = load_json(path)
        except SummaryError:
            continue
        if not isinstance(record, dict):
            continue
        run_id = record.get("run_id")
        evidence_hash = (record.get("evidence") or {}).get("sha256")
        identity = (run_id, evidence_hash)
        if identity in by_identity:
            duplicate_count += 1
            continue
        if isinstance(run_id, str) and run_id:
            for previous_run_id, previous_hash in by_identity:
                if previous_run_id == run_id and previous_hash != evidence_hash:
                    conflict_count += 1
                    break
        by_identity[identity] = path
        entries.append((path, record))
    return entries, duplicate_count, conflict_count


def _stage_results(record, stage):
    return [
        item
        for item in record.get("stage_outputs", [])
        if isinstance(item, dict) and item.get("stage") == stage
    ]


def _recovery_values(record):
    timeline = record.get("timeline") or {}
    failure = _parse_timestamp(timeline.get("failure_observed_at_utc"))
    recovered = _parse_timestamp(timeline.get("recovery_verified_at_utc"))
    if not failure or not recovered or recovered < failure:
        return None
    return (recovered - failure).total_seconds()


def _http_stage_summary(records):
    attempted = success = failure = 0
    for _, record in records:
        stages = _stage_results(record, "http_verify")
        attempted += len(stages)
        success += sum(item.get("result") == "success" for item in stages)
        failure += sum(item.get("result") == "failure" for item in stages)
    return {
        "attempted": attempted,
        "success": success,
        "failure": failure,
        "availability": "observed" if attempted else "unavailable",
        **({} if attempted else {"reason": "no HTTP verification stage was recorded"}),
    }


def _http_poll_summary(records):
    poll_paths = []
    for record_path, _ in records:
        poll_paths.extend(sorted(record_path.parent.rglob("http-polls.jsonl")))
    if not poll_paths:
        return _metric(
            None,
            "count",
            "unavailable",
            "request-level HTTP poll evidence was not retained",
        )
    failed = total = malformed = 0
    for path in poll_paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            malformed += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                poll = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(poll, dict):
                malformed += 1
                continue
            total += 1
            status_ok = _as_bool(poll.get("status_ok"))
            http_error = _as_bool(poll.get("http_error"))
            sha_mismatch = _as_bool(poll.get("sha_mismatch"))
            status_code = poll.get("status_code")
            status_failed = False
            if isinstance(status_code, int):
                status_failed = not 200 <= status_code < 300
            elif isinstance(status_code, str) and status_code.isdigit():
                status_failed = not 200 <= int(status_code) < 300
            if status_ok is False or http_error or sha_mismatch or status_failed:
                failed += 1
            elif status_ok is None and not http_error and not sha_mismatch and not status_code:
                malformed += 1
    if total == 0:
        return _metric(None, "count", "unavailable", "HTTP poll files contained no valid polls")
    if malformed:
        return _metric(
            failed,
            "count",
            "partial",
            "some HTTP poll rows were malformed",
            sample_count=total,
        )
    return _metric(failed, "count", "observed", sample_count=total)


def _resource_summaries(record_path):
    summaries = []
    for path in sorted(record_path.parent.rglob("summary.json")):
        try:
            document = load_json(path)
        except SummaryError:
            continue
        if isinstance(document, dict) and "scopes" in document and "samples" in document:
            summaries.append(document)
    return summaries


def _find_numeric(document, names):
    if isinstance(document, dict):
        for name in names:
            value = _number(document.get(name))
            if value is not None:
                return value
        for value in document.values():
            found = _find_numeric(value, names)
            if found is not None:
                return found
    elif isinstance(document, list):
        for value in document:
            found = _find_numeric(value, names)
            if found is not None:
                return found
    return None


def _scope_class(scope):
    if str(scope).startswith("preview:"):
        return "preview"
    return str(scope).split(":", 1)[0] or "unknown"


def _resource_summary(records):
    aggregates = {}
    unsupported = set()
    running_high_water = []
    managed_high_water = []
    for record_path, record in records:
        metrics = record.get("metrics") or {}
        for reason in metrics.get("unavailable_reasons", []) or []:
            unsupported.add(_reason_category(reason))
        for key, measurement in (metrics.get("measurements") or {}).items():
            if not isinstance(measurement, dict) or not isinstance(key, str):
                continue
            metric_name = key.rsplit(":", 1)[-1]
            if metric_name not in KNOWN_METRIC_NAMES:
                continue
            scope = key[: -(len(metric_name) + 1)]
            group_key = (_scope_class(scope), metric_name, measurement.get("unit") or "unavailable")
            group = aggregates.setdefault(
                group_key,
                {"values": [], "covered_seconds": 0.0, "available": 0, "total": 0},
            )
            group["total"] += 1
            if measurement.get("available") and _number(measurement.get("value")) is not None:
                group["available"] += 1
                group["values"].append(float(measurement["value"]))
                covered = _number(measurement.get("covered_seconds"))
                if covered is not None:
                    group["covered_seconds"] += covered
            else:
                unsupported.add(_reason_category(f"{metric_name} unavailable"))
        for summary in _resource_summaries(record_path):
            running = _find_numeric(
                summary,
                {"max_observed_running_preview_count", "max_running_preview_count"},
            )
            managed = _find_numeric(
                summary,
                {"max_observed_managed_namespace_count", "max_managed_namespace_count"},
            )
            if running is not None:
                running_high_water.append(running)
            if managed is not None:
                managed_high_water.append(managed)
    by_scope = []
    for (scope, metric_name, unit), group in sorted(aggregates.items()):
        if group["available"] == group["total"]:
            availability = "observed"
            value = sum(group["values"])
            reason = None
        elif group["available"]:
            availability = "partial"
            value = sum(group["values"])
            reason = "some measurements were unavailable or incomplete"
        else:
            availability = "unavailable"
            value = None
            reason = "all measurements were unavailable or incomplete"
        by_scope.append(
            _metric(
                value,
                unit,
                availability,
                reason,
                scope=scope,
                metric=metric_name,
                sample_count=group["total"],
                available_count=group["available"],
                covered_seconds=group["covered_seconds"] or None,
            )
        )
    return {
        "by_scope": by_scope,
        "unsupported": sorted(unsupported),
        "observed_running_high_water": max(running_high_water) if running_high_water else None,
        "observed_managed_high_water": max(managed_high_water) if managed_high_water else None,
    }


def _explicit_count(record, keys):
    for container_name in ("cleanup", "observations", "state"):
        container = record.get(container_name) or {}
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                return len(value)
            number = _number(value)
            if number is not None:
                return number
    return None


def _cleanup_summary(records):
    applicable = []
    confirmed = 0
    duplicate_counts = []
    orphan_counts = []
    for _, record in records:
        if record.get("scenario") not in {"close", "reopen", "failure"}:
            continue
        applicable.append(record)
        cleanup = record.get("cleanup") or {}
        if cleanup.get("namespace_absent") is True and cleanup.get("related_resources_absent") is True:
            confirmed += 1
        duplicate = _explicit_count(record, ("duplicate_count", "duplicate_resources"))
        orphan = _explicit_count(record, ("orphan_count", "orphan_resources"))
        if duplicate is not None:
            duplicate_counts.append(duplicate)
        if orphan is not None:
            orphan_counts.append(orphan)
    confirmed_metric = _metric(
        confirmed if applicable else None,
        "count",
        "observed" if applicable else "unavailable",
        None if applicable else "no cleanup-applicable records were selected",
        applicable_count=len(applicable),
    )
    duplicate_metric = _metric(
        sum(duplicate_counts) if duplicate_counts else None,
        "count",
        "observed" if duplicate_counts else "unavailable",
        None if duplicate_counts else "resource inventory did not provide duplicate counts",
    )
    orphan_metric = _metric(
        sum(orphan_counts) if orphan_counts else None,
        "count",
        "observed" if orphan_counts else "unavailable",
        None if orphan_counts else "resource inventory did not provide orphan counts",
    )
    return {
        "confirmed_absent": confirmed_metric,
        "duplicate_resources": duplicate_metric,
        "orphan_resources": orphan_metric,
    }


def summarize(paths):
    records, duplicate_count, conflict_count = _record_entries(paths)
    if not records:
        raise SummaryError("no run-record.json files found")
    considered = [
        item for item in records if item[1].get("outcome") in {"success", "failure"}
    ]
    excluded = {
        "invalid": sum(record.get("outcome") == "invalid" for _, record in records),
        "unsupported": sum(record.get("outcome") == "unsupported" for _, record in records),
        "incomplete": sum(
            record.get("outcome") in {"success", "failure"} and not record.get("valid", False)
            for _, record in records
        ),
    }
    lifecycle_success = sum(record.get("outcome") == "success" for _, record in considered)
    evaluated = [item for item in considered if item[1].get("valid") is True]
    evaluated_success = sum(record.get("outcome") == "success" for _, record in evaluated)

    recovery_values = [value for _, record in considered if (value := _recovery_values(record)) is not None]
    recovery_metric = _metric(
        sum(recovery_values) / len(recovery_values) if recovery_values else None,
        "second",
        "observed" if recovery_values else "unavailable",
        None if recovery_values else "no failure and recovery timestamps were selected",
        sample_count=len(recovery_values),
    )
    if recovery_values:
        recovery_metric.update({"minimum": min(recovery_values), "maximum": max(recovery_values)})

    resources = _resource_summary(records)
    http_summary = _http_poll_summary(records)
    cleanup_summary = _cleanup_summary(records)
    project_sizes = _project_size_summary(records)
    planned = [_number(record.get("conditions", {}).get("concurrency")) for _, record in records]
    planned = [value for value in planned if value is not None]
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "selection": {
            "records_found": len(find_records(paths)),
            "records_considered": len(considered),
            "excluded": excluded,
            "duplicates_removed": duplicate_count,
            "identity_conflicts": conflict_count,
        },
        "project_sizes": project_sizes,
        "success_rate": {
            "lifecycle": _metric(
                lifecycle_success / len(considered) if considered else None,
                "ratio",
                "observed" if considered else "unavailable",
                None if considered else "no lifecycle records were selected",
                numerator=lifecycle_success,
                denominator=len(considered),
            ),
            "evaluated": _metric(
                evaluated_success / len(evaluated) if evaluated else None,
                "ratio",
                "observed" if evaluated else "unavailable",
                None if evaluated else "no strictly valid records were selected",
                numerator=evaluated_success,
                denominator=len(evaluated),
            ),
        },
        "recovery_time_seconds": recovery_metric,
        "http": {
            "verification_stages": _http_stage_summary(records),
            "failed_requests": http_summary,
        },
        "cleanup": cleanup_summary,
        "resources": {
            "by_scope": resources["by_scope"],
            "unsupported": resources["unsupported"],
        },
        "concurrency": {
            "planned": max(planned) if planned else None,
            "planned_availability": "observed" if planned else "unavailable",
            "observed_running_high_water": resources["observed_running_high_water"],
            "observed_managed_high_water": resources["observed_managed_high_water"],
            "capacity": None,
            "capacity_availability": "unavailable",
            "capacity_reason": "the selected runs do not establish cluster capacity",
        },
        "limitations": sorted(
            set(resources["unsupported"])
            | ({"request_level_http_unavailable"} if http_summary["availability"] == "unavailable" else set())
            | ({"recovery_time_unavailable"} if recovery_metric["availability"] == "unavailable" else set())
            | ({"duplicate_inventory_unavailable"} if cleanup_summary["duplicate_resources"]["availability"] == "unavailable" else set())
            | ({"orphan_inventory_unavailable"} if cleanup_summary["orphan_resources"]["availability"] == "unavailable" else set())
            | ({"unsupported_project_size"} if any(item["unsupported"] for item in project_sizes) else set())
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="+", type=Path, help="run record or private evidence directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(summarize(args.path), indent=2, sort_keys=True))
    except SummaryError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
