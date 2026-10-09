#!/usr/bin/env python3
"""Generate privacy-safe tables, charts, and a Markdown report from run records.

The input is a set of private ``run-record.json`` files.  Records are validated
before they are analysed, while failed lifecycle records remain in the
observed reliability cohort.  Missing and not-applicable measurements are
kept as explicit counts instead of being converted to zeroes.
"""

import argparse
import csv
from datetime import datetime
import html
import json
import math
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_runner import TIMING_PHASES, validate_record  # noqa: E402


ANALYSIS_SCHEMA_VERSION = "previewmesh-analysis-report-v1"
DEFAULT_SMALL_SAMPLE_THRESHOLD = 5
RELIABILITY_SCENARIOS = {"close", "reopen", "failure"}
RESOURCE_METRIC_NAMES = {
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


class AnalysisError(Exception):
    """A safe, user-facing analysis error."""


def find_records(paths):
    """Find unique run records without exposing their paths in output."""
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


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise AnalysisError("could not read run-record evidence") from error


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def quantile(values, probability):
    """Return a linearly interpolated quantile using the (n-1) method."""
    ordered = sorted(float(value) for value in values if _number(value) is not None)
    if not ordered:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def descriptive_stats(
    values,
    sample_count=None,
    not_applicable_count=0,
    small_sample_threshold=DEFAULT_SMALL_SAMPLE_THRESHOLD,
):
    """Calculate explicit descriptive statistics for one metric.

    ``sample_count`` is the number of attempted observations.  Values that
    are absent or malformed are counted as missing, and not-applicable values
    are kept separate so that neither category is mistaken for zero.
    """
    if small_sample_threshold < 1:
        raise ValueError("small_sample_threshold must be positive")
    observed = [float(value) for value in values if _number(value) is not None]
    attempted = len(observed) if sample_count is None else max(int(sample_count), 0)
    not_applicable = max(int(not_applicable_count), 0)
    missing = max(attempted - len(observed) - not_applicable, 0)
    if observed and missing == 0 and not_applicable == 0:
        availability = "observed"
    elif observed:
        availability = "partial"
    elif not_applicable and missing == 0:
        availability = "not_applicable"
    elif attempted:
        availability = "unavailable"
    else:
        availability = "unavailable"

    result = {
        "sample_count": attempted,
        "available_count": len(observed),
        "missing_count": missing,
        "not_applicable_count": not_applicable,
        "availability": availability,
        "mean": None,
        "median": None,
        "p90": None,
        "p95": None,
        "p99": None,
        "maximum": None,
        "stddev": None,
        "small_sample": len(observed) < small_sample_threshold,
    }
    notes = []
    if not observed:
        notes.append(
            "no observed values"
            if availability != "not_applicable"
            else "all attempted observations were not applicable"
        )
    else:
        mean = sum(observed) / len(observed)
        result.update(
            {
                "mean": mean,
                "median": quantile(observed, 0.50),
                "p90": quantile(observed, 0.90),
                "p95": quantile(observed, 0.95),
                "p99": quantile(observed, 0.99),
                "maximum": max(observed),
            }
        )
        if len(observed) >= 2:
            result["stddev"] = math.sqrt(
                sum((value - mean) ** 2 for value in observed) / (len(observed) - 1)
            )
        else:
            notes.append("standard deviation unavailable for one observed value")
    if result["small_sample"]:
        notes.append(f"small sample: fewer than {small_sample_threshold} observed values")
    if missing:
        notes.append(f"{missing} missing value(s) excluded from statistics")
    if not_applicable:
        notes.append(f"{not_applicable} not-applicable value(s) excluded from statistics")
    if notes:
        result["notes"] = notes
    return result


def _new_bucket():
    return {"values": [], "sample_count": 0, "not_applicable_count": 0}


def _add_bucket(bucket, value=None, availability="unavailable"):
    bucket["sample_count"] += 1
    if availability == "not_applicable":
        bucket["not_applicable_count"] += 1
    elif _number(value) is not None:
        bucket["values"].append(float(value))


def _scope_class(scope):
    text = str(scope or "")
    if text.startswith("preview:"):
        return "preview"
    head = text.split(":", 1)[0]
    return head if head in {"cluster", "host", "registry", "unknown"} else "other"


def _safe_metric_name(name):
    if name in RESOURCE_METRIC_NAMES:
        return name
    return "other"


def _safe_group_value(value, fallback="unknown"):
    if value is None:
        return fallback
    text = str(value)
    return text if text in {"simple", "frontend-backend", "multi-service", "unknown"} else fallback


def _record_entries(paths):
    """Load and validate records, returning accepted records and safe counts."""
    candidates = find_records(paths)
    if not candidates:
        raise AnalysisError("no run-record.json files found")
    entries = []
    seen = set()
    exclusions = {}
    duplicates = 0
    conflicts = 0
    for path in candidates:
        try:
            record = load_json(path)
        except AnalysisError:
            exclusions["unreadable"] = exclusions.get("unreadable", 0) + 1
            continue
        if not isinstance(record, dict):
            exclusions["not_an_object"] = exclusions.get("not_an_object", 0) + 1
            continue
        validation = validate_record(record, require_measurements=False)
        if validation["errors"]:
            exclusions["validation_failed"] = exclusions.get("validation_failed", 0) + 1
            continue
        run_id = record.get("run_id")
        evidence_hash = (record.get("evidence") or {}).get("sha256")
        identity = (run_id, evidence_hash)
        if identity in seen:
            duplicates += 1
            continue
        if any(previous[0] == run_id and previous[1] != evidence_hash for previous in seen):
            conflicts += 1
        seen.add(identity)
        entries.append((path, record))
    if not entries:
        raise AnalysisError("no validated run records were found")
    return {
        "candidates": len(candidates),
        "entries": entries,
        "exclusions": exclusions,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


def _timing_buckets(entries):
    overall = {phase: _new_bucket() for phase in TIMING_PHASES}
    by_project = {}
    by_concurrency = {}
    for _, record in entries:
        project = _safe_group_value((record.get("conditions") or {}).get("project_size"))
        concurrency_value = (record.get("conditions") or {}).get("concurrency")
        concurrency = str(int(concurrency_value)) if isinstance(concurrency_value, int) and concurrency_value > 0 else "unknown"
        project_buckets = by_project.setdefault(project, {phase: _new_bucket() for phase in TIMING_PHASES})
        concurrency_buckets = by_concurrency.setdefault(concurrency, {phase: _new_bucket() for phase in TIMING_PHASES})
        phases = (record.get("timings") or {}).get("phases") or {}
        for phase in TIMING_PHASES:
            phase_entries = phases.get(phase)
            if not isinstance(phase_entries, list) or not phase_entries:
                phase_entries = [{"availability": "unavailable", "duration_seconds": None}]
            for item in phase_entries:
                if not isinstance(item, dict):
                    item = {"availability": "unavailable", "duration_seconds": None}
                availability = item.get("availability", "unavailable")
                value = item.get("duration_seconds")
                _add_bucket(overall[phase], value, availability)
                _add_bucket(project_buckets[phase], value, availability)
                _add_bucket(concurrency_buckets[phase], value, availability)
    return overall, by_project, by_concurrency


def _resource_buckets(entries):
    buckets = {}
    by_project = {}
    by_concurrency = {}
    for _, record in entries:
        project = _safe_group_value((record.get("conditions") or {}).get("project_size"))
        concurrency_value = (record.get("conditions") or {}).get("concurrency")
        concurrency = str(int(concurrency_value)) if isinstance(concurrency_value, int) and concurrency_value > 0 else "unknown"
        project_buckets = by_project.setdefault(project, {})
        concurrency_buckets = by_concurrency.setdefault(concurrency, {})
        measurements = (record.get("metrics") or {}).get("measurements") or {}
        for key, measurement in measurements.items():
            if not isinstance(key, str) or not isinstance(measurement, dict):
                continue
            parts = key.rsplit(":", 1)
            if len(parts) != 2:
                continue
            scope, raw_metric = parts
            metric = _safe_metric_name(raw_metric)
            bucket_key = f"{_scope_class(scope)}:{metric}"
            bucket = buckets.setdefault(bucket_key, _new_bucket())
            project_bucket = project_buckets.setdefault(bucket_key, _new_bucket())
            concurrency_bucket = by_concurrency.setdefault(concurrency, {}).setdefault(bucket_key, _new_bucket())
            available = measurement.get("available") is True
            value = measurement.get("value") if available else None
            availability = "observed" if available and _number(value) is not None else "unavailable"
            _add_bucket(bucket, value, availability)
            _add_bucket(project_bucket, value, availability)
            _add_bucket(concurrency_bucket, value, availability)
    return buckets, by_project, by_concurrency


def _poll_failures(record_path):
    poll_paths = sorted(record_path.parent.rglob("http-polls.jsonl"))
    if not poll_paths:
        return None
    failures = 0
    valid_rows = 0
    for path in poll_paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            valid_rows += 1
            status_code = item.get("status_code")
            status_failed = isinstance(status_code, int) and not 200 <= status_code < 300
            if item.get("status_ok") is False or item.get("http_error") is True or item.get("sha_mismatch") is True or status_failed:
                failures += 1
    return failures if valid_rows else None


def _recovery_seconds(record):
    timeline = record.get("timeline") or {}
    started = timeline.get("failure_observed_at_utc")
    ended = timeline.get("recovery_verified_at_utc")
    if not isinstance(started, str) or not isinstance(ended, str):
        return None
    try:
        start_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not start_at.tzinfo or not end_at.tzinfo or end_at < start_at:
        return None
    return (end_at - start_at).total_seconds()


def _reliability_rows(entries, threshold):
    considered = [(path, record) for path, record in entries if record.get("outcome") in {"success", "failure"}]
    success_bucket = _new_bucket()
    for _, record in considered:
        _add_bucket(success_bucket, 1 if record.get("outcome") == "success" else 0, "observed")
    rows = [_stats_row("lifecycle_success_rate", "ratio", success_bucket, threshold)]
    failure_records = [(path, record) for path, record in considered if record.get("outcome") == "failure"]
    recovery_bucket = _new_bucket()
    for _, record in failure_records:
        _add_bucket(recovery_bucket, _recovery_seconds(record), "observed" if _recovery_seconds(record) is not None else "unavailable")
    rows.append(_stats_row("recovery_time", "second", recovery_bucket, threshold))

    http_bucket = _new_bucket()
    for path, _ in considered:
        failures = _poll_failures(path)
        _add_bucket(http_bucket, failures, "observed" if failures is not None else "unavailable")
    rows.append(_stats_row("failed_http_requests_per_run", "count", http_bucket, threshold))

    cleanup_records = [(path, record) for path, record in considered if record.get("scenario") in RELIABILITY_SCENARIOS]
    duplicate_bucket = _new_bucket()
    orphan_bucket = _new_bucket()
    cleanup_bucket = _new_bucket()
    for _, record in cleanup_records:
        cleanup = record.get("cleanup") or {}
        duplicate = cleanup.get("duplicate_count", cleanup.get("duplicate_resources"))
        orphan = cleanup.get("orphan_count", cleanup.get("orphan_resources"))
        _add_bucket(duplicate_bucket, duplicate, "observed" if _number(duplicate) is not None else "unavailable")
        _add_bucket(orphan_bucket, orphan, "observed" if _number(orphan) is not None else "unavailable")
        confirmed = cleanup.get("namespace_absent") is True and cleanup.get("related_resources_absent") is True
        if "namespace_absent" in cleanup and "related_resources_absent" in cleanup:
            _add_bucket(cleanup_bucket, 1 if confirmed else 0, "observed")
        else:
            _add_bucket(cleanup_bucket, None, "unavailable")
    rows.extend(
        [
            _stats_row("duplicate_resources_per_cleanup", "count", duplicate_bucket, threshold),
            _stats_row("orphaned_resources_per_cleanup", "count", orphan_bucket, threshold),
            _stats_row("cleanup_namespace_verified", "ratio", cleanup_bucket, threshold),
        ]
    )
    return rows


def _stats_row(metric, unit, bucket, threshold, **extra):
    row = {"metric": metric, "unit": unit}
    row.update(
        descriptive_stats(
            bucket["values"],
            sample_count=bucket["sample_count"],
            not_applicable_count=bucket["not_applicable_count"],
            small_sample_threshold=threshold,
        )
    )
    row.update(extra)
    return row


def _timing_rows(overall, by_project, by_concurrency, threshold):
    rows = []
    for phase in TIMING_PHASES:
        rows.append(_stats_row(f"phase:{phase}", "second", overall[phase], threshold))
    comparisons = []
    for dimension, groups in (("project_size", by_project), ("concurrency", by_concurrency)):
        for group, phase_buckets in sorted(groups.items()):
            for phase in TIMING_PHASES:
                comparisons.append(
                    _stats_row(
                        f"phase:{phase}",
                        "second",
                        phase_buckets[phase],
                        threshold,
                        dimension=dimension,
                        group=group,
                    )
                )
    return rows, comparisons


def _resource_rows(buckets, by_project, by_concurrency, threshold):
    rows = []
    for metric, bucket in sorted(buckets.items()):
        rows.append(_stats_row(f"resource:{metric}", "recorded", bucket, threshold))
    comparisons = []
    for dimension, groups in (("project_size", by_project), ("concurrency", by_concurrency)):
        for group, metric_buckets in sorted(groups.items()):
            for metric, bucket in sorted(metric_buckets.items()):
                comparisons.append(
                    _stats_row(
                        f"resource:{metric}",
                        "recorded",
                        bucket,
                        threshold,
                        dimension=dimension,
                        group=group,
                    )
                )
    return rows, comparisons


def _selection(document):
    entries = document["entries"]
    considered = [(path, record) for path, record in entries if record.get("outcome") in {"success", "failure"}]
    failed = [record for _, record in considered if record.get("outcome") == "failure"]
    unsupported = [record for _, record in entries if record.get("outcome") == "unsupported"]
    invalid = [record for _, record in entries if record.get("outcome") == "invalid"]
    incomplete = [record for _, record in considered if record.get("valid") is not True]
    exclusions = dict(document["exclusions"])
    if unsupported:
        exclusions["unsupported"] = len(unsupported)
    if invalid:
        exclusions["invalid_outcome"] = len(invalid)
    return {
        "records_found": document["candidates"],
        "validated_records": len(entries),
        "records_included": len(considered),
        "failed_runs_included": len(failed),
        "incomplete_included": len(incomplete),
        "excluded": dict(sorted(exclusions.items())),
        "duplicates_removed": document["duplicates"],
        "identity_conflicts": document["conflicts"],
    }


def _warning_rows(rows):
    warnings = []
    for row in rows:
        if row.get("small_sample"):
            warnings.append(f"{row['metric']} has a small observed sample")
        if row.get("availability") in {"partial", "unavailable"}:
            warnings.append(f"{row['metric']} has missing or incomplete values")
    return warnings


def analyze(paths, small_sample_threshold=DEFAULT_SMALL_SAMPLE_THRESHOLD):
    if small_sample_threshold < 1:
        raise AnalysisError("small sample threshold must be positive")
    document = _record_entries(paths)
    entries = document["entries"]
    considered = [(path, record) for path, record in entries if record.get("outcome") in {"success", "failure"}]
    if not considered:
        # Unsupported-only cohorts still produce a useful exclusion report.
        timing_entries = []
    else:
        timing_entries = considered
    overall_timing, project_timing, concurrency_timing = _timing_buckets(timing_entries)
    timing_rows, timing_comparisons = _timing_rows(
        overall_timing, project_timing, concurrency_timing, small_sample_threshold
    )
    resources, project_resources, concurrency_resources = _resource_buckets(timing_entries)
    resource_rows, resource_comparisons = _resource_rows(
        resources, project_resources, concurrency_resources, small_sample_threshold
    )
    reliability_rows = _reliability_rows(entries, small_sample_threshold)
    all_rows = timing_rows + timing_comparisons + resource_rows + resource_comparisons + reliability_rows
    warnings = _warning_rows(all_rows)
    selection = _selection(document)
    if selection["incomplete_included"]:
        warnings.append("incomplete lifecycle records remain included for available measurements")
    if selection["excluded"]:
        warnings.append("excluded records are reported by category and omitted from metric denominators")
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "generated_by": "scripts/analysis_report.py",
        "selection": selection,
        "definitions": {
            "quantile_method": "linear interpolation at position (n - 1) * probability",
            "standard_deviation": "sample standard deviation using n - 1; unavailable for n < 2",
            "missing_values": "unavailable or malformed values are counted and omitted from statistics",
            "not_applicable_values": "not-applicable values are counted separately and omitted from statistics",
            "success_rate": "mean of one for success and zero for failure over included lifecycle records",
            "small_sample_threshold": small_sample_threshold,
            "timing_sample_unit": "one sample per timing entry; partial entries remain labelled partial",
        },
        "tables": {
            "timing_statistics": timing_rows,
            "comparisons": timing_comparisons + resource_comparisons,
            "resource_statistics": resource_rows,
            "reliability": reliability_rows,
        },
        "charts": [
            {"path": "charts/timing-phases.svg", "title": "Observed timing phases"},
            {"path": "charts/project-size-total-feedback.svg", "title": "Total feedback by project size"},
            {"path": "charts/concurrency-total-feedback.svg", "title": "Total feedback by concurrency"},
        ],
        "warnings": sorted(set(warnings)),
    }


def _chart_svg(title, points, y_label):
    width, height = 760, 430
    left, right, top, bottom = 72, 28, 58, 82
    plot_width = width - left - right
    plot_height = height - top - bottom
    numeric = [(label, value, count) for label, value, count in points if _number(value) is not None]
    maximum = max((value for _, value, _ in numeric), default=0.0)
    if maximum <= 0:
        maximum = 1.0
    bar_width = plot_width / max(len(points), 1) * 0.62
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2:.1f}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="18" y="{top + plot_height / 2:.1f}" transform="rotate(-90 18 {top + plot_height / 2:.1f})" text-anchor="middle" font-family="sans-serif" font-size="12">{html.escape(y_label)}</text>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#555"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#555"/>',
    ]
    for index, (label, value, count) in enumerate(points):
        x = left + (index + 0.5) * plot_width / max(len(points), 1)
        if _number(value) is not None:
            bar_height = float(value) / maximum * plot_height
            y = top + plot_height - bar_height
            pieces.append(
                f'<rect x="{x - bar_width / 2:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="#276749"/>'
            )
            pieces.append(
                f'<text x="{x:.1f}" y="{max(y - 7, top + 12):.1f}" text-anchor="middle" font-family="sans-serif" font-size="11">{float(value):.2f}</text>'
            )
        else:
            pieces.append(
                f'<text x="{x:.1f}" y="{top + plot_height / 2:.1f}" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#777">n/a</text>'
            )
        pieces.append(
            f'<text x="{x:.1f}" y="{top + plot_height + 22}" text-anchor="middle" font-family="sans-serif" font-size="11" transform="rotate(25 {x:.1f} {top + plot_height + 22})">{html.escape(str(label))}</text>'
        )
        if count:
            pieces.append(
                f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#555">n={int(count)}</text>'
            )
    if not numeric:
        pieces.append(
            f'<text x="{width / 2:.1f}" y="{top + plot_height / 2:.1f}" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#777">No observed values</text>'
        )
    pieces.append("</svg>")
    return "\n".join(pieces) + "\n"


def _row_value(row, key="mean"):
    value = row.get(key)
    return _number(value)


def _chart_points(report, dimension, metric):
    return [
        (row.get("group", "unknown"), _row_value(row), row.get("available_count", 0))
        for row in report["tables"]["comparisons"]
        if row.get("dimension") == dimension and row.get("metric") == metric
    ]


def _write_json(path, document):
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path, rows):
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or ["metric"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            serialised = {
                key: json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else "" if value is None else value
                for key, value in row.items()
            }
            writer.writerow(serialised)


def _display(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _markdown_table(rows):
    columns = [
        "metric", "dimension", "group", "unit", "sample_count", "available_count",
        "missing_count", "not_applicable_count", "availability", "mean", "median",
        "p90", "p95", "p99", "maximum", "stddev", "small_sample",
    ]
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(_display(row.get(column)) for column in columns) + " |")
    return "\n".join([header, separator] + body) if body else "No rows."


def _markdown_report(report):
    selection = report["selection"]
    lines = [
        "# Evaluation analysis",
        "",
        "This report is generated from validated run records. It contains aggregate values only; record identities and input paths are not emitted.",
        "",
        "## Selection and exclusions",
        "",
        f"- Records found: {selection['records_found']}",
        f"- Validated records: {selection['validated_records']}",
        f"- Included lifecycle records: {selection['records_included']}",
        f"- Failed runs included: {selection['failed_runs_included']}",
        f"- Incomplete included records: {selection['incomplete_included']}",
        f"- Exclusions by category: `{json.dumps(selection['excluded'], sort_keys=True)}`",
        f"- Duplicate identities removed: {selection['duplicates_removed']}",
        f"- Identity conflicts retained: {selection['identity_conflicts']}",
        "",
        "## Definitions",
        "",
    ]
    for key, value in report["definitions"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Timing statistics", "", _markdown_table(report["tables"]["timing_statistics"]), ""])
    lines.extend(["## Reliability", "", _markdown_table(report["tables"]["reliability"]), ""])
    cleanup_rows = [
        row
        for row in report["tables"]["reliability"]
        if row["metric"] in {
            "duplicate_resources_per_cleanup",
            "orphaned_resources_per_cleanup",
            "cleanup_namespace_verified",
        }
    ]
    lines.extend(["## Cleanup completeness", "", _markdown_table(cleanup_rows), ""])
    lines.extend(["## Resource statistics", "", _markdown_table(report["tables"]["resource_statistics"]), ""])
    lines.extend(["## Comparisons", "", _markdown_table(report["tables"]["comparisons"]), ""])
    lines.extend(["## Charts", ""])
    for chart in report["charts"]:
        lines.append(f"- [{chart['title']}]({chart['path']})")
    if report["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def write_outputs(report, output_dir):
    output_dir = Path(output_dir).expanduser()
    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "analysis.json", report)
    _write_csv(output_dir / "timing-statistics.csv", report["tables"]["timing_statistics"])
    _write_csv(output_dir / "comparisons.csv", report["tables"]["comparisons"])
    _write_csv(output_dir / "resource-statistics.csv", report["tables"]["resource_statistics"])
    _write_csv(output_dir / "reliability.csv", report["tables"]["reliability"])
    timing_points = []
    for row in report["tables"]["timing_statistics"]:
        if row["metric"] in {"phase:build", "phase:pod_readiness", "phase:http_verification", "phase:total_feedback", "phase:cleanup"}:
            timing_points.append((row["metric"].split(":", 1)[1], row.get("mean"), row.get("available_count", 0)))
    (charts_dir / "timing-phases.svg").write_text(
        _chart_svg("Observed timing phases", timing_points, "mean seconds"), encoding="utf-8"
    )
    (charts_dir / "project-size-total-feedback.svg").write_text(
        _chart_svg(
            "Total feedback by project size",
            _chart_points(report, "project_size", "phase:total_feedback"),
            "mean seconds",
        ),
        encoding="utf-8",
    )
    (charts_dir / "concurrency-total-feedback.svg").write_text(
        _chart_svg(
            "Total feedback by concurrency",
            _chart_points(report, "concurrency", "phase:total_feedback"),
            "mean seconds",
        ),
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(_markdown_report(report), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="+", type=Path, help="run record or private evidence directory")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--small-sample-threshold",
        type=int,
        default=DEFAULT_SMALL_SAMPLE_THRESHOLD,
        help="mark metric rows with fewer observed values as small samples",
    )
    args = parser.parse_args(argv)
    try:
        report = analyze(args.path, small_sample_threshold=args.small_sample_threshold)
        write_outputs(report, args.output_dir)
    except (AnalysisError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps({"output_dir": str(args.output_dir), "schema_version": ANALYSIS_SCHEMA_VERSION}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
