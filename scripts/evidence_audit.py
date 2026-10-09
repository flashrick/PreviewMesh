#!/usr/bin/env python3
"""Audit retained PreviewMesh evidence without printing private identities."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


AUDIT_SCHEMA_VERSION = "previewmesh-evidence-audit-v1"


class AuditError(Exception):
    """A safe, user-facing evidence audit error."""


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise AuditError("could not read retained evidence") from error


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


def _exists(root, names):
    wanted = set(names)
    return any(path.is_file() and path.name in wanted for path in root.rglob("*"))


def _glob_exists(root, pattern):
    return any(path.is_file() for path in root.rglob(pattern))


def _reason_category(reason):
    lowered = str(reason or "").lower()
    if "measurement" in lowered or "coverage" in lowered:
        return "measurement_unavailable"
    if "event" in lowered:
        return "kubernetes_events_unavailable"
    if "state" in lowered or "snapshot" in lowered:
        return "kubernetes_state_unavailable"
    if "cleanup" in lowered:
        return "cleanup_not_applicable"
    if "stage" in lowered or "timing" in lowered:
        return "stage_timing_unavailable"
    if "manifest" in lowered or "checksum" in lowered:
        return "manifest_integrity"
    return "evidence_unavailable"


def _manifest_status(archive):
    path = archive / "MANIFEST.json"
    if not path.is_file():
        return "missing", "manifest_integrity"
    try:
        manifest = load_json(path)
    except AuditError:
        return "invalid", "manifest_integrity"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return "invalid", "manifest_integrity"
    total_bytes = 0
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return "invalid", "manifest_integrity"
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            return "invalid", "manifest_integrity"
        target = archive / relative
        if not target.is_file():
            return "invalid", "manifest_integrity"
        actual_bytes = target.stat().st_size
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_bytes != entry.get("bytes") or actual_hash != entry.get("sha256"):
            return "invalid", "manifest_integrity"
        total_bytes += actual_bytes
    if total_bytes != manifest.get("total_bytes"):
        return "invalid", "manifest_integrity"
    return "present", None


def _lifecycle_status(record):
    if record.get("outcome") == "unsupported":
        return "not_applicable", []
    lifecycle = record.get("lifecycle") or {}
    missing = lifecycle.get("missing_stages") or {}
    real_missing = [
        stage
        for stage, reason in missing.items()
        if reason != "not_applicable for an open preview scenario"
    ]
    if record.get("outcome") == "success" and real_missing:
        return "incomplete", sorted(real_missing)
    return "complete" if not real_missing else "partial", sorted(real_missing)


def _audit_record(record_path):
    archive = record_path.parent
    record = load_json(record_path)
    scenario = record.get("scenario", "unknown")
    outcome = record.get("outcome", "unknown")
    unsupported = outcome == "unsupported"
    reasons = set()

    manifest_status, manifest_reason = _manifest_status(archive)
    if manifest_reason:
        reasons.add(manifest_reason)

    artifact_categories = {
        "conditions": "present" if (archive / "conditions.json").is_file() else "missing",
        "manifest": manifest_status,
        "validation": "present" if (archive / "validation.json").is_file() else "missing",
        "unsupported_reason": "present" if (archive / "unsupported.json").is_file() else "not_applicable",
    }
    if artifact_categories["conditions"] == "missing":
        reasons.add("conditions_missing")
    if artifact_categories["validation"] == "missing" and not unsupported:
        reasons.add("validation_missing")

    if unsupported:
        for category in (
            "workflow_raw_log",
            "stage_log",
            "resource_evidence",
            "http_results",
            "cleanup_evidence",
        ):
            artifact_categories[category] = "not_applicable"
    else:
        artifact_categories["workflow_raw_log"] = (
            "present" if _exists(archive, ("workflow.log", "workflow.json")) else "missing"
        )
        artifact_categories["stage_log"] = (
            "present" if _glob_exists(archive, "combined.csv") else "missing"
        )
        samples = _glob_exists(archive, "samples.jsonl")
        resource_summary = _glob_exists(archive, "summary.json")
        artifact_categories["resource_evidence"] = (
            "present" if samples and resource_summary else "partial" if samples or resource_summary else "missing"
        )
        has_http_stage = any(
            isinstance(item, dict) and item.get("stage") == "http_verify"
            for item in record.get("stage_outputs", [])
        )
        if scenario == "close":
            artifact_categories["http_results"] = "not_applicable"
        else:
            artifact_categories["http_results"] = (
                "present"
                if has_http_stage or _glob_exists(archive, "http-polls.jsonl")
                else "missing"
            )
        if scenario in {"close", "reopen", "failure"}:
            artifact_categories["cleanup_evidence"] = (
                "present"
                if _exists(archive, ("cleanup-verification.json", "cleanup.json"))
                else "missing"
            )
        else:
            artifact_categories["cleanup_evidence"] = "not_applicable"
        for category in (
            "workflow_raw_log",
            "stage_log",
            "resource_evidence",
            "http_results",
            "cleanup_evidence",
        ):
            if artifact_categories[category] in {"missing", "partial"}:
                reasons.add(f"{category}_missing")

    # These two categories are intentionally separate from the resource collector.
    artifact_categories["kubernetes_events"] = (
        "present"
        if _glob_exists(archive, "events.jsonl") or _glob_exists(archive, "kubernetes-events.jsonl")
        else "not_applicable" if unsupported else "unavailable"
    )
    artifact_categories["kubernetes_state_snapshot"] = (
        "present"
        if _glob_exists(archive, "resource-state.json") or _glob_exists(archive, "inventory.json")
        else "not_applicable" if unsupported else "unavailable"
    )
    if not unsupported:
        if artifact_categories["kubernetes_events"] == "unavailable":
            reasons.add("kubernetes_events_unavailable")
        if artifact_categories["kubernetes_state_snapshot"] == "unavailable":
            reasons.add("kubernetes_state_unavailable")
        for reason in (record.get("metrics") or {}).get("unavailable_reasons", []) or []:
            reasons.add(_reason_category(reason))

    lifecycle, missing_stages = _lifecycle_status(record)
    if lifecycle == "incomplete":
        reasons.add("lifecycle_incomplete")
    exclusion_reason_present = bool(str(record.get("exclusion_reason", "")).strip())
    if not record.get("valid", False) and not exclusion_reason_present:
        reasons.add("exclusion_reason_missing")

    workflow_dispatch = (
        "not_dispatched"
        if unsupported
        else "dispatched"
        if (archive / "workflow.json").is_file()
        else "unknown"
    )
    if unsupported and not exclusion_reason_present:
        reasons.add("exclusion_reason_missing")
    if unsupported and not (archive / "unsupported.json").is_file():
        reasons.add("unsupported_reason_missing")

    if unsupported:
        audit_status = "unsupported_record" if not reasons else "incomplete_record"
    elif lifecycle == "incomplete" or artifact_categories["manifest"] != "present":
        audit_status = "incomplete_record"
    elif reasons:
        audit_status = "complete_with_limitations"
    else:
        audit_status = "complete"
    return {
        "scenario": scenario,
        "outcome": outcome,
        "artifact_categories": artifact_categories,
        "lifecycle": {"status": lifecycle, "missing_stages": missing_stages},
        "missing_or_unavailable_reason_categories": sorted(reasons),
        "exclusion_reason_present": exclusion_reason_present,
        "workflow_dispatch": workflow_dispatch,
        "audit_status": audit_status,
    }


def audit(paths):
    records = find_records(paths)
    if not records:
        raise AuditError("no run-record.json files found")
    audited = []
    for ordinal, path in enumerate(records, start=1):
        item = _audit_record(path)
        item["record_ordinal"] = ordinal
        audited.append(item)
    statuses = {}
    reason_counts = {}
    for item in audited:
        statuses[item["audit_status"]] = statuses.get(item["audit_status"], 0) + 1
        for reason in item["missing_or_unavailable_reason_categories"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "records_found": len(audited),
        "status_counts": statuses,
        "reason_category_counts": reason_counts,
        "records": audited,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="+", type=Path, help="run record or private evidence directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(audit(args.path), indent=2, sort_keys=True))
    except AuditError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
