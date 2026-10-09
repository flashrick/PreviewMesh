#!/usr/bin/env python3
"""Build a privacy-safe preliminary evaluation report from private summaries."""

import argparse
import json
from pathlib import Path
import sys


REPORT_SCHEMA_VERSION = "previewmesh-preliminary-report-v1"


class ReportError(Exception):
    """A safe, user-facing report error."""


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise ReportError("could not read summary input") from error


def _availability(document, default="unavailable"):
    return document.get("availability", default) if isinstance(document, dict) else default


def _missing_runs(summary, audit):
    missing = []
    concurrency = (summary.get("concurrency") or {}).get("by_level", [])
    for level in concurrency:
        if level.get("status") != "observed":
            missing.append(
                {
                    "area": "reliability",
                    "condition": f"concurrency level {level.get('level')}",
                    "status": level.get("status", "not_run"),
                    "reason": "independent cohort execution and overlap evidence are unavailable",
                }
            )
    for project in summary.get("project_sizes", []) or []:
        if project.get("unsupported", 0) and not project.get("lifecycle_success", 0):
            missing.append(
                {
                    "area": "resource_efficiency",
                    "condition": f"project size {project.get('project_size')}",
                    "status": "unsupported",
                    "reason": "registered fixture or chart support is not established",
                }
            )
    evaluated = (summary.get("success_rate") or {}).get("evaluated") or {}
    if _availability(evaluated) != "observed":
        missing.append(
            {
                "area": "resource_efficiency",
                "condition": "strict measurement-complete baseline",
                "status": "missing",
                "reason": "selected records retain unavailable resource measurements",
            }
        )
    recovery = summary.get("recovery_time_seconds") or {}
    if _availability(recovery) != "observed":
        missing.append(
            {
                "area": "reliability",
                "condition": "failure and recovery repetitions",
                "status": "missing",
                "reason": "no failure-and-recovery timestamps were selected",
            }
        )
    audit_reasons = set()
    for reason, count in (audit.get("reason_category_counts") or {}).items():
        if count:
            audit_reasons.add(reason)
    if "kubernetes_events_unavailable" in audit_reasons:
        missing.append(
            {
                "area": "feedback_speed",
                "condition": "independent Kubernetes event timeline",
                "status": "missing",
                "reason": "event artifacts were not retained for the supported records",
            }
        )
    return missing


def build_report(summary, audit):
    if not isinstance(summary, dict) or not isinstance(audit, dict):
        raise ReportError("summary and audit inputs must be objects")
    feedback = {
        "availability": "partial",
        "observed_phases": [
            "queueing",
            "build",
            "pod_readiness",
            "service_ingress_readiness",
            "http_verification",
            "total_feedback",
            "cleanup",
        ],
        "unavailable_phases": [
            "scheduling",
            "image_pull",
            "container_start",
            "independent_kubernetes_events",
        ],
        "reason": "workflow timing and stage evidence are available, but several Kubernetes transition boundaries are not",
    }
    reliability = {
        "availability": "partial",
        "lifecycle_success": (summary.get("success_rate") or {}).get("lifecycle"),
        "strict_evaluated_success": (summary.get("success_rate") or {}).get("evaluated"),
        "recovery_time_seconds": summary.get("recovery_time_seconds"),
        "http_verification": (summary.get("http") or {}).get("verification_stages"),
        "failed_http_requests": (summary.get("http") or {}).get("failed_requests"),
    }
    resource_efficiency = {
        "availability": "partial",
        "project_sizes": summary.get("project_sizes", []),
        "resource_aggregates": summary.get("resources", {}),
        "concurrency": summary.get("concurrency", {}),
        "reason": "resource scope and high-water observations are retained, but strict coverage and capacity are incomplete",
    }
    cleanup = summary.get("cleanup") or {}
    cleanup_completeness = {
        "availability": "partial",
        "confirmed_absent": cleanup.get("confirmed_absent"),
        "duplicate_resources": cleanup.get("duplicate_resources"),
        "orphan_resources": cleanup.get("orphan_resources"),
        "audit_status_counts": audit.get("status_counts", {}),
        "reason": "target cleanup is observed, while complete global orphan and duplicate inventories are unavailable",
    }
    limitations = sorted(
        set(summary.get("limitations", []))
        | set(audit.get("reason_category_counts", {}).keys())
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "coverage": {
            "records_summarised": summary.get("selection", {}).get("records_found", 0),
            "records_audited": audit.get("records_found", 0),
        },
        "areas": {
            "feedback_speed": feedback,
            "reliability": reliability,
            "resource_efficiency": resource_efficiency,
            "cleanup_completeness": cleanup_completeness,
        },
        "missing_runs_for_week13": _missing_runs(summary, audit),
        "limitations": limitations,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(load_json(args.summary), load_json(args.audit))
    except ReportError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
