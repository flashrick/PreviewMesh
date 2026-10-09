#!/usr/bin/env python3
"""Run repeatable PreviewMesh evaluations and validate private run records.

The runner uses the existing GitHub Actions workflow as the lifecycle driver.
It keeps conditions, workflow outputs, and downloaded artifacts together in a
private evidence directory, while the validator makes missing observations and
incomplete lifecycle records explicit instead of treating them as zeroes.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


SCHEMA_VERSION = "previewmesh-run-v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$")
POSITIVE_ID_RE = re.compile(r"^[1-9][0-9]*$")
TIMELINE_FIELDS = (
    "event_received_at_utc",
    "deployment_started_at_utc",
    "readiness_started_at_utc",
    "http_verification_started_at_utc",
    "failure_observed_at_utc",
    "recovery_started_at_utc",
    "recovery_verified_at_utc",
    "final_verify_at_utc",
    "cleanup_started_at_utc",
    "cleanup_absent_at_utc",
)
EXPECTED_STAGES = {
    "create": ("event", "build", "deploy", "readiness", "http_verify", "report"),
    "update": ("event", "build", "deploy", "readiness", "http_verify", "report"),
    "reopen": ("event", "build", "deploy", "readiness", "http_verify", "report"),
    "close": ("event", "cleanup", "report"),
    "failure": ("event", "report"),
}
OUTCOMES = {"success", "failure", "unsupported", "invalid"}
RECOVERY_MODES = {
    "none",
    "automatic-rollback",
    "automatic-retry",
    "explicit-deploy",
    "manual",
    "unavailable",
}


class RunnerError(Exception):
    """A safe, user-facing runner error with no command output attached."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def private_root(path):
    """Reject evidence inside the public checkout before creating anything."""
    resolved = Path(path).expanduser().resolve()
    public_root = Path(__file__).resolve().parents[1]
    if resolved == public_root or public_root in resolved.parents:
        raise RunnerError("evidence output must be outside the public checkout")
    return resolved


def mkdir_private(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_text_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def write_json_private(path, document):
    write_text_private(path, json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n")


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise RunnerError("could not read JSON input") from error


def command_error(command, error=None):
    executable = Path(command[0]).name if command else "command"
    detail = " timed out" if isinstance(error, subprocess.TimeoutExpired) else " failed"
    return RunnerError(f"{executable}{detail}; inspect the retained collection error")


def run_command(command, timeout, cwd=None, check=True):
    """Run without a shell and discard stderr, which may contain credentials."""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise command_error(command, error) from error
    if check and completed.returncode:
        raise command_error(command)
    return completed


def validate_config(config):
    if not isinstance(config, dict):
        raise RunnerError("experiment config must be a JSON object")
    control_repository = config.get("control_repository")
    if not isinstance(control_repository, str) or not REPOSITORY_RE.fullmatch(control_repository):
        raise RunnerError("config.control_repository must be owner/name")
    workflow = config.get("workflow", "preview.yml")
    if not isinstance(workflow, str) or not workflow.strip() or "/" in workflow or "\\" in workflow:
        raise RunnerError("config.workflow must be a workflow filename or ID")
    fixtures = config.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise RunnerError("config.fixtures must contain at least one fixture")
    names = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise RunnerError("each fixture must be an object")
        name = fixture.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise RunnerError("fixture names must use letters, digits, dot, dash, or underscore")
        if name in names:
            raise RunnerError("fixture names must be unique")
        names.add(name)
        for field in ("repository_id", "pr_number"):
            if not POSITIVE_ID_RE.fullmatch(str(fixture.get(field, ""))):
                raise RunnerError(f"fixture.{field} must be a canonical positive integer")
        source = fixture.get("source_repository")
        if not isinstance(source, str) or not REPOSITORY_RE.fullmatch(source):
            raise RunnerError("fixture.source_repository must be owner/name")
        if "source_commit_sha" in fixture and fixture["source_commit_sha"] not in (None, "unavailable"):
            if not SHA_RE.fullmatch(str(fixture["source_commit_sha"])):
                raise RunnerError("fixture.source_commit_sha must be a lowercase full SHA")
        if "image_digest" in fixture and fixture["image_digest"] not in (None, "unavailable"):
            if not DIGEST_RE.fullmatch(str(fixture["image_digest"])):
                raise RunnerError("fixture.image_digest must be an immutable digest")
    return config


def fixture_by_name(config, selected):
    fixtures = config["fixtures"]
    if not selected:
        return fixtures
    wanted = set(selected)
    available = {fixture["name"] for fixture in fixtures}
    unknown = wanted - available
    if unknown:
        raise RunnerError("unknown fixture: " + ", ".join(sorted(unknown)))
    return [fixture for fixture in fixtures if fixture["name"] in wanted]


def safe_run_id(fixture, scenario, repetition, concurrency):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{fixture}-s{scenario}-c{concurrency}-n{repetition}-{stamp}"


def first_json(root, filename, predicate=None):
    candidates = sorted(Path(root).rglob(filename))
    candidates.sort(key=lambda path: (0 if "evidence" in path.parts else 1, str(path)))
    for path in candidates:
        try:
            document = load_json(path)
        except RunnerError:
            continue
        if predicate is None or predicate(document):
            return document, path
    return None, None


def all_json(root, filename):
    values = []
    for path in sorted(Path(root).rglob(filename)):
        try:
            values.append((load_json(path), path))
        except RunnerError:
            continue
    return values


def result_files(artifacts):
    names = ("build.json", "deploy.json", "cleanup.json", "outcome.json")
    results = {}
    for name in names:
        document, path = first_json(artifacts, name)
        if document is not None:
            results[Path(name).stem] = document
            results[Path(name).stem + "_path"] = str(path.relative_to(artifacts))
    return results


def normalise_stage(stage):
    stage = str(stage)
    if stage.startswith("queue:"):
        return "report" if stage.endswith(":report") else stage.split(":", 1)[1]
    if stage == "build_push":
        return "build"
    if stage == "cleanup_inspect":
        return "cleanup"
    if stage == "resource_verify":
        return "cleanup"
    return stage


def stage_outputs(artifacts, results):
    outputs = []
    combined = sorted(Path(artifacts).rglob("combined.csv"))
    csv_paths = combined or sorted(Path(artifacts).rglob("*.csv"))
    for path in csv_paths:
        try:
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or "stage" not in reader.fieldnames:
                    continue
                for row in reader:
                    if not row.get("stage"):
                        continue
                    outputs.append({
                        "stage": normalise_stage(row["stage"]),
                        "raw_stage": row["stage"],
                        "started_at_utc": row.get("started_at_utc") or None,
                        "ended_at_utc": row.get("ended_at_utc") or None,
                        "duration_seconds": float(row["duration_seconds"]) if row.get("duration_seconds") else None,
                        "result": row.get("result") or "unavailable",
                        "error": row.get("error") or "",
                        "source": str(path.relative_to(artifacts)),
                    })
        except (OSError, ValueError, csv.Error):
            continue
    for operation in ("build", "deploy", "cleanup"):
        for timing in results.get(operation, {}).get("stage_timings", []) or []:
            if not isinstance(timing, dict) or not timing.get("stage"):
                continue
            outputs.append({
                "stage": normalise_stage(timing["stage"]),
                "raw_stage": timing["stage"],
                "started_at_utc": timing.get("started_at_utc"),
                "ended_at_utc": timing.get("ended_at_utc"),
                "duration_seconds": timing.get("duration_seconds"),
                "result": timing.get("result", "unavailable"),
                "error": "",
                "source": operation + ".json",
            })
    unique = {}
    for output in outputs:
        key = (output["stage"], output.get("started_at_utc"), output.get("ended_at_utc"), output["result"])
        unique.setdefault(key, output)
    return sorted(unique.values(), key=lambda item: (item.get("started_at_utc") or "", item["stage"]))


def timing_for(outputs, stage, last=False):
    matching = [item for item in outputs if item.get("stage") == stage and item.get("started_at_utc")]
    if not matching:
        return None
    return matching[-1 if last else 0]


def infer_timeline(scenario, outputs, workflow_metadata):
    timeline = {field: None for field in TIMELINE_FIELDS}
    reasons = {}
    created = workflow_metadata.get("createdAt") or workflow_metadata.get("created_at")
    if created:
        timeline["event_received_at_utc"] = created
    else:
        reasons["event_received_at_utc"] = "workflow metadata did not include created_at"

    mapping = {
        "deployment_started_at_utc": ("deploy", "started_at_utc"),
        "readiness_started_at_utc": ("readiness", "started_at_utc"),
        "http_verification_started_at_utc": ("http_verify", "started_at_utc"),
        "final_verify_at_utc": ("http_verify", "ended_at_utc"),
        "cleanup_started_at_utc": ("cleanup", "started_at_utc"),
    }
    for field, (stage, moment) in mapping.items():
        item = timing_for(outputs, stage, last=field in {"final_verify_at_utc"})
        if item and item.get(moment):
            timeline[field] = item[moment]
        else:
            reasons[field] = f"stage {stage} was not recorded"

    failures = [item for item in outputs if item.get("result") == "failure" and item.get("ended_at_utc")]
    if failures:
        timeline["failure_observed_at_utc"] = failures[0]["ended_at_utc"]
    else:
        reasons["failure_observed_at_utc"] = "no failed stage was recorded"

    rollback = timing_for(outputs, "rollback")
    cleanup = timing_for(outputs, "cleanup")
    if rollback:
        timeline["recovery_started_at_utc"] = rollback.get("started_at_utc")
        if rollback.get("result") == "success":
            timeline["recovery_verified_at_utc"] = rollback.get("ended_at_utc")
        else:
            reasons["recovery_verified_at_utc"] = "rollback did not finish successfully"
    elif cleanup and scenario in {"close", "reopen", "failure"}:
        timeline["recovery_started_at_utc"] = cleanup.get("started_at_utc")
        if cleanup.get("result") == "success":
            timeline["recovery_verified_at_utc"] = cleanup.get("ended_at_utc")
        else:
            reasons["recovery_verified_at_utc"] = "cleanup did not finish successfully"
    else:
        reasons["recovery_started_at_utc"] = "no recovery stage was recorded"
        reasons["recovery_verified_at_utc"] = "no recovery stage was recorded"

    resource_verify = timing_for(outputs, "cleanup", last=True)
    if resource_verify and resource_verify.get("raw_stage") == "resource_verify":
        timeline["cleanup_absent_at_utc"] = resource_verify.get("ended_at_utc")
    elif cleanup and cleanup.get("result") == "success" and scenario in {"close", "reopen", "failure"}:
        timeline["cleanup_absent_at_utc"] = cleanup.get("ended_at_utc")
    else:
        reasons["cleanup_absent_at_utc"] = "confirmed cleanup absence was not recorded"

    for field, value in timeline.items():
        if value is not None:
            try:
                parse_timestamp(value)
            except ValueError:
                reasons[field] = "recorded timestamp is invalid"
                timeline[field] = None
    return timeline, reasons


def expected_stages(scenario, conditions):
    stages = list(EXPECTED_STAGES.get(scenario, EXPECTED_STAGES["failure"]))
    failure_type = conditions.get("failure_type", "none")
    if scenario == "failure" and failure_type in {"deployment", "http", "cleanup"}:
        stages.insert(1, {"deployment": "deploy", "http": "http_verify", "cleanup": "cleanup"}[failure_type])
    return stages


def lifecycle_record(scenario, conditions, outputs, workflow_metadata, outcome):
    observed = {"event"} if workflow_metadata else set()
    observed.update(item["stage"] for item in outputs)
    expected = expected_stages(scenario, conditions)
    missing = {}
    for stage in expected:
        if stage not in observed:
            missing[stage] = "stage output was not downloaded"
    if scenario not in {"close", "reopen", "failure"}:
        missing.setdefault("cleanup", "not_applicable for an open preview scenario")
    if outcome == "unsupported":
        missing = {stage: "unsupported scenario" for stage in expected if stage not in observed}
    return {
        "expected_stages": expected,
        "observed_stages": sorted(observed),
        "missing_stages": missing,
    }


def resource_metrics(artifacts):
    resource = None
    resource_path = None
    for document, path in all_json(artifacts, "summary.json"):
        if isinstance(document, dict) and "scopes" in document and "samples" in document:
            resource, resource_path = document, path
            break
    metrics = {
        "units": {"cpu": "core", "memory": "byte", "storage": "byte", "resource_minutes": "unit_minute"},
        "coverage_seconds": {},
        "unavailable_reasons": [],
        "measurements": {},
    }
    if resource is None:
        metrics["unavailable_reasons"].append("resource collector summary was not found in workflow artifacts")
        return metrics
    observed = resource.get("observed_duration_seconds")
    if isinstance(observed, (int, float)):
        metrics["coverage_seconds"]["resource_collection"] = observed
    else:
        metrics["unavailable_reasons"].append("resource summary has no observed duration")
    for scope_name, scope in (resource.get("scopes") or {}).items():
        measurements = scope.get("measurements", {}) if isinstance(scope, dict) else {}
        for metric_name, measurement in measurements.items():
            if not isinstance(measurement, dict):
                continue
            key = f"{scope_name}:{metric_name}"
            metrics["measurements"][key] = {
                "available": measurement.get("complete", False),
                "unit": measurement.get("integral_unit") or measurement.get("unit"),
                "value": measurement.get("integral"),
                "covered_seconds": measurement.get("covered_seconds", 0),
                "uncovered_seconds": measurement.get("uncovered_seconds", 0),
            }
            if not measurement.get("complete", False):
                metrics["unavailable_reasons"].append(
                    f"{key} coverage is incomplete or unavailable"
                )
    return metrics


def cleanup_record(results, scenario):
    cleanup = results.get("cleanup", {})
    inspection = cleanup.get("cleanup_inspection") or cleanup.get("inspection") or {}
    state = inspection.get("state")
    confirmed = cleanup.get("cleanup") == "confirmed_absent" or state == "confirmed_absent"
    namespace_uid = (
        inspection.get("namespace_uid")
        or cleanup.get("namespace_uid")
        or "unavailable"
    )
    if scenario not in {"close", "reopen", "failure"}:
        reason = "cleanup is not applicable until the preview is closed"
        return {
            "namespace_uid": "unavailable",
            "uid_guarded": None,
            "namespace_absent": None,
            "related_resources_absent": None,
            "orphan_scope": "unavailable",
            "unavailable_reason": reason,
        }
    return {
        "namespace_uid": namespace_uid,
        "uid_guarded": bool(namespace_uid != "unavailable"),
        "namespace_absent": confirmed,
        "related_resources_absent": confirmed if state == "confirmed_absent" else None,
        "orphan_scope": "namespace" if confirmed else "unavailable",
        "unavailable_reason": "" if confirmed else "cleanup absence was not confirmed",
    }


def image_digest(results, fixture):
    for operation in ("deploy", "build"):
        image = results.get(operation, {}).get("image", "")
        if isinstance(image, str) and "@" in image:
            digest = image.rsplit("@", 1)[1]
            if DIGEST_RE.fullmatch(digest):
                return digest
    configured = fixture.get("image_digest")
    if configured and configured != "unavailable":
        return configured
    return "unavailable"


def source_sha(results, fixture):
    for operation in ("deploy", "build", "outcome"):
        sha = results.get(operation, {}).get("requested_sha", "")
        if isinstance(sha, str) and SHA_RE.fullmatch(sha):
            return sha
    configured = fixture.get("source_commit_sha")
    return configured if configured and configured != "unavailable" else "unavailable"


def outcome_from_results(results, workflow_metadata, unsupported=False):
    if unsupported:
        return "unsupported"
    outcome = results.get("summary", {}).get("outcome") or results.get("outcome", {}).get("outcome")
    if outcome in {"ready", "removed"}:
        return "success"
    if outcome in {"failure", "superseded"}:
        return "failure"
    conclusion = workflow_metadata.get("conclusion")
    if conclusion == "success":
        return "success"
    if conclusion in {"failure", "cancelled", "timed_out"}:
        return "failure"
    return "invalid"


def evidence_manifest(archive):
    archive = Path(archive)
    entries = []
    total_bytes = 0
    for path in sorted(archive.rglob("*")):
        if not path.is_file() or path.name in {"MANIFEST.json", "run-record.json"}:
            continue
        relative = path.relative_to(archive).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        total_bytes += size
        entries.append({"path": relative, "bytes": size, "sha256": digest})
    manifest = {"schema_version": 1, "files": entries, "total_bytes": total_bytes}
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    write_text_private(archive / "MANIFEST.json", manifest_bytes.decode())
    return {"path": str(archive), "bytes": total_bytes, "sha256": manifest_hash, "file_count": len(entries)}


def build_record(spec, archive, results, outputs, workflow_metadata, unsupported=False):
    scenario = spec["scenario"]
    conditions = spec["conditions"]
    outcome = outcome_from_results(results, workflow_metadata, unsupported=unsupported)
    timeline, timeline_reasons = infer_timeline(scenario, outputs, workflow_metadata)
    metrics = resource_metrics(archive / "artifacts")
    cleanup = cleanup_record(results, scenario)
    if cleanup.get("unavailable_reason") and scenario in {"close", "reopen", "failure"}:
        metrics["unavailable_reasons"].append(cleanup["unavailable_reason"])
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": spec["run_id"],
        "scenario": scenario,
        "repetition": spec["repetition"],
        "fixture": spec["fixture"],
        "source_commit_sha": source_sha(results, spec["fixture_config"]),
        "image_digest": image_digest(results, spec["fixture_config"]),
        "conditions": conditions,
        "timeline": timeline,
        "timeline_unavailable_reasons": timeline_reasons,
        "state": {
            "desired": {"status": "unavailable", "reason": "workflow state snapshot not exported"},
            "observed": {"status": "unavailable", "reason": "workflow state snapshot not exported"},
            "action": {"status": "unavailable", "reason": "workflow action snapshot not exported"},
            "final": {"status": "unavailable", "reason": "workflow state snapshot not exported"},
        },
        "outcome": outcome,
        "failure_trigger": (
            conditions.get("failure_type", "none")
            if conditions.get("failure_type", "none") != "none"
            else next((item.get("raw_stage") for item in outputs if item.get("result") == "failure"), "none")
        ),
        "recovery_mode": conditions.get("recovery_mode", "none"),
        "final_served_sha": results.get("deploy", {}).get("served_sha") or "unavailable",
        "cleanup": cleanup,
        "metrics": metrics,
        "stage_outputs": outputs,
        "lifecycle": lifecycle_record(scenario, conditions, outputs, workflow_metadata, outcome),
        "workflow": {
            "run_id": workflow_metadata.get("databaseId") or workflow_metadata.get("database_id") or "unavailable",
            "url": workflow_metadata.get("url", ""),
            "conclusion": workflow_metadata.get("conclusion", ""),
        },
        "evidence": evidence_manifest(archive),
        "valid": False,
        "exclusion_reason": "",
    }
    validation = validate_record(record, require_measurements=True)
    record["valid"] = outcome not in {"invalid", "unsupported"} and not validation["errors"]
    if validation["errors"]:
        record["exclusion_reason"] = "; ".join(validation["errors"])
    elif outcome in {"invalid", "unsupported"}:
        record["exclusion_reason"] = "workflow output did not produce a complete evaluated run"
    return record, validation


def validate_record(record, require_measurements=True):
    errors = []
    warnings = []
    if not isinstance(record, dict):
        return {"errors": ["record is not an object"], "warnings": []}
    required = (
        "schema_version", "run_id", "scenario", "repetition", "fixture",
        "source_commit_sha", "image_digest", "conditions", "timeline", "state",
        "outcome", "failure_trigger", "recovery_mode", "final_served_sha",
        "cleanup", "metrics", "evidence", "valid", "exclusion_reason",
    )
    for field in required:
        if field not in record:
            errors.append(f"missing required field: {field}")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    if not isinstance(record.get("run_id"), str) or not record.get("run_id"):
        errors.append("run_id is missing")
    scenario = record.get("scenario")
    if scenario not in EXPECTED_STAGES:
        errors.append("scenario is unsupported")
    outcome = record.get("outcome")
    if outcome not in OUTCOMES:
        errors.append("outcome is invalid")
    if not isinstance(record.get("repetition"), int) or record.get("repetition", 0) < 1:
        errors.append("repetition must be a positive integer")
    if not isinstance(record.get("fixture"), str) or not record.get("fixture"):
        errors.append("fixture is missing")
    for field, pattern in (("source_commit_sha", SHA_RE), ("image_digest", DIGEST_RE)):
        value = record.get(field)
        if value == "unavailable":
            if outcome not in {"failure", "unsupported", "invalid"} and scenario != "close":
                errors.append(f"{field} is unavailable for a successful run")
            else:
                warnings.append(f"{field} unavailable")
        elif not isinstance(value, str) or not pattern.fullmatch(value):
            errors.append(f"{field} is not an immutable identity")

    conditions = record.get("conditions")
    if not isinstance(conditions, dict):
        errors.append("conditions must be an object")
    else:
        for field in ("project_size", "build_policy", "metric_interval_seconds", "operation_timeout_seconds", "http_timeout_seconds", "concurrency"):
            if field not in conditions:
                errors.append(f"conditions missing {field}")
        if not isinstance(conditions.get("concurrency"), int) or conditions.get("concurrency", 0) < 1:
            errors.append("conditions.concurrency must be positive")

    timeline = record.get("timeline")
    reasons = record.get("timeline_unavailable_reasons", {})
    if not isinstance(timeline, dict):
        errors.append("timeline must be an object")
    elif not isinstance(reasons, dict):
        errors.append("timeline_unavailable_reasons must be an object")
    else:
        for field in TIMELINE_FIELDS:
            if field not in timeline:
                errors.append(f"timeline missing {field}")
            elif timeline[field] is None:
                if not reasons.get(field):
                    errors.append(f"timeline missing reason for {field}")
            else:
                try:
                    parse_timestamp(timeline[field])
                except (TypeError, ValueError):
                    errors.append(f"timeline {field} is not a UTC timestamp")

    state = record.get("state")
    if not isinstance(state, dict):
        errors.append("state must be an object")
    else:
        for field in ("desired", "observed", "action", "final"):
            if not isinstance(state.get(field), dict):
                errors.append(f"state missing {field}")

    if record.get("recovery_mode") not in RECOVERY_MODES:
        errors.append("recovery_mode is invalid")
    if not isinstance(record.get("failure_trigger"), str) or not record.get("failure_trigger"):
        errors.append("failure_trigger is missing")

    cleanup = record.get("cleanup")
    if not isinstance(cleanup, dict):
        errors.append("cleanup must be an object")
    else:
        for field in ("namespace_uid", "uid_guarded", "namespace_absent", "related_resources_absent", "orphan_scope"):
            if field not in cleanup:
                errors.append(f"cleanup missing {field}")
        if cleanup.get("orphan_scope") not in {"namespace", "cluster", "permission-limited", "unavailable"}:
            errors.append("cleanup.orphan_scope is invalid")
        if cleanup.get("namespace_uid") == "unavailable" and not cleanup.get("unavailable_reason"):
            errors.append("cleanup missing unavailable reason")

    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics must be an object")
    else:
        for field in ("units", "coverage_seconds", "unavailable_reasons"):
            if field not in metrics:
                errors.append(f"metrics missing {field}")
        if not isinstance(metrics.get("unavailable_reasons"), list):
            errors.append("metrics.unavailable_reasons must be a list")
        elif metrics["unavailable_reasons"]:
            warnings.append("one or more measurements are unavailable")
            if require_measurements and outcome not in {"unsupported", "invalid"}:
                errors.append("required measurements are missing or incomplete")

    lifecycle = record.get("lifecycle")
    if not isinstance(lifecycle, dict):
        errors.append("lifecycle record is missing")
    else:
        missing = lifecycle.get("missing_stages", {})
        if not isinstance(missing, dict):
            errors.append("lifecycle.missing_stages must be an object")
        elif outcome == "success" and missing:
            real_missing = {stage: reason for stage, reason in missing.items() if reason != "not_applicable for an open preview scenario"}
            if real_missing:
                errors.append("lifecycle record is incomplete: " + ", ".join(sorted(real_missing)))
        if outcome == "failure":
            if not record.get("failure_trigger") or record.get("failure_trigger") == "none":
                errors.append("failed run has no failure trigger")
            if not isinstance(timeline, dict) or not timeline.get("failure_observed_at_utc"):
                errors.append("failed run has no failure_observed_at_utc")

    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        errors.append("evidence must be an object")
    else:
        if not evidence.get("path"):
            errors.append("evidence.path is missing")
        if not isinstance(evidence.get("bytes"), int) or evidence.get("bytes", -1) < 0:
            errors.append("evidence.bytes is invalid")
        if not isinstance(evidence.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("sha256", "")):
            errors.append("evidence.sha256 is invalid")

    if record.get("valid") and errors:
        errors.append("valid=true contradicts validation errors")
    if not record.get("valid") and not record.get("exclusion_reason") and errors:
        warnings.append("invalid record has no exclusion_reason")
    return {"errors": errors, "warnings": warnings}


def collect_workflow(repo, workflow, ref, fixture, archive, timeout, poll_interval, wait_timeout, existing_run_id=None):
    metadata = {}
    if existing_run_id:
        workflow_run_id = str(existing_run_id)
    else:
        dispatch_started = datetime.now(timezone.utc)
        command = [
            "gh", "workflow", "run", workflow, "--repo", repo, "--ref", ref,
            "-f", f"repository_id={fixture['repository_id']}",
            "-f", f"source_repository={fixture['source_repository']}",
            "-f", f"pr_number={fixture['pr_number']}",
        ]
        run_command(command, timeout=timeout)
        workflow_run_id = None
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            listing = run_command([
                "gh", "run", "list", "--repo", repo, "--workflow", workflow,
                "--limit", "50", "--json", "databaseId,createdAt,event,headBranch,status,url",
            ], timeout=timeout)
            try:
                runs = json.loads(listing.stdout)
            except ValueError as error:
                raise RunnerError("gh returned invalid workflow list JSON") from error
            candidates = []
            for item in runs if isinstance(runs, list) else []:
                try:
                    created = parse_timestamp(item.get("createdAt", ""))
                except (TypeError, ValueError):
                    continue
                # GitHub list timestamps can be rounded to whole seconds.
                if created.timestamp() >= dispatch_started.timestamp() - 10:
                    candidates.append(item)
            if candidates:
                candidates.sort(key=lambda item: item.get("createdAt", ""), reverse=True)
                workflow_run_id = str(candidates[0].get("databaseId"))
                break
            time.sleep(min(poll_interval, max(0.1, deadline - time.monotonic())))
        if not workflow_run_id:
            raise RunnerError("workflow dispatch succeeded but its run ID was not found")

    deadline = time.monotonic() + wait_timeout
    while True:
        view = run_command([
            "gh", "run", "view", workflow_run_id, "--repo", repo,
            "--json", "databaseId,status,conclusion,createdAt,updatedAt,headSha,url",
        ], timeout=timeout)
        try:
            metadata = json.loads(view.stdout)
        except ValueError as error:
            raise RunnerError("gh returned invalid workflow metadata JSON") from error
        if metadata.get("status") == "completed":
            break
        if time.monotonic() >= deadline:
            raise RunnerError("workflow run did not finish before the wait timeout")
        time.sleep(poll_interval)

    write_json_private(archive / "workflow.json", metadata)
    log_result = run_command([
        "gh", "run", "view", workflow_run_id, "--repo", repo, "--log",
    ], timeout=timeout, check=False)
    if log_result.returncode == 0:
        write_text_private(archive / "workflow.log", log_result.stdout)
    else:
        write_json_private(archive / "collection-errors.json", {"workflow_log": "workflow log unavailable"})
    artifacts = archive / "artifacts"
    mkdir_private(artifacts)
    download = run_command([
        "gh", "run", "download", workflow_run_id, "--repo", repo, "--dir", str(artifacts),
    ], timeout=timeout, check=False)
    if download.returncode:
        existing = load_json(archive / "collection-errors.json") if (archive / "collection-errors.json").exists() else {}
        existing["artifacts"] = "workflow artifacts unavailable"
        write_json_private(archive / "collection-errors.json", existing)
    return workflow_run_id, metadata


def make_spec(fixture, scenario, repetition, args):
    conditions = dict(fixture.get("conditions", {}))
    conditions.update({
        "project_size": fixture.get("project_size", conditions.get("project_size", "unknown")),
        "commit_version": fixture.get("source_commit_sha", conditions.get("commit_version", "recorded-from-workflow")),
        "concurrency": args.concurrency,
        "build_policy": args.build_policy or fixture.get("build_policy", conditions.get("build_policy", "rebuild")),
        "failure_type": args.failure_type,
        "metric_interval_seconds": args.metric_interval_seconds,
        "operation_timeout_seconds": args.operation_timeout_seconds,
        "http_timeout_seconds": args.http_timeout_seconds,
    })
    return {
        "run_id": safe_run_id(fixture["name"], scenario, repetition, args.concurrency),
        "scenario": scenario,
        "repetition": repetition,
        "fixture": fixture["name"],
        "fixture_config": fixture,
        "conditions": conditions,
    }


def run_one(config, spec, archive_root, args, unsupported_reason=""):
    archive = archive_root / spec["run_id"]
    archive.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(archive, 0o700)
    mkdir_private(archive / "artifacts")
    write_json_private(archive / "conditions.json", {
        "schema_version": SCHEMA_VERSION,
        "run_id": spec["run_id"],
        "fixture": spec["fixture"],
        "scenario": spec["scenario"],
        "repetition": spec["repetition"],
        "conditions": spec["conditions"],
    })
    if unsupported_reason:
        write_json_private(archive / "unsupported.json", {"reason": unsupported_reason})
        manifest = evidence_manifest(archive)
        results = {"summary": {"outcome": "unsupported"}}
        record, validation = build_record(spec, archive, results, [], {}, unsupported=True)
        record["exclusion_reason"] = unsupported_reason
        record["valid"] = False
        write_json_private(archive / "run-record.json", record)
        write_json_private(archive / "validation.json", validation)
        return {"run_id": spec["run_id"], "archive": str(archive), "valid": False, "validation": validation, "manifest": manifest}

    existing_id = args.workflow_run_id if args.workflow_run_id and args.repetitions == 1 else None
    try:
        workflow_run_id, metadata = collect_workflow(
            config["control_repository"], config.get("workflow", "preview.yml"), config.get("ref", "main"),
            spec["fixture_config"], archive, args.command_timeout, args.poll_interval, args.wait_timeout,
            existing_run_id=existing_id,
        )
    except RunnerError as error:
        write_json_private(archive / "collection-errors.json", {"runner": str(error)})
        metadata = {"databaseId": existing_id or "unavailable", "status": "incomplete", "conclusion": ""}
        workflow_run_id = existing_id or "unavailable"

    results = result_files(archive / "artifacts")
    summary, summary_path = first_json(
        archive / "artifacts", "summary.json",
        predicate=lambda item: isinstance(item, dict) and "outcome" in item,
    )
    if summary is not None:
        results["summary"] = summary
        results["summary_path"] = str(summary_path.relative_to(archive / "artifacts"))
    outputs = stage_outputs(archive / "artifacts", results)
    manifest = evidence_manifest(archive)
    record, validation = build_record(spec, archive, results, outputs, metadata)
    record["workflow"]["run_id"] = workflow_run_id
    record["evidence"] = manifest
    write_json_private(archive / "run-record.json", record)
    write_json_private(archive / "validation.json", validation)
    return {
        "run_id": spec["run_id"],
        "workflow_run_id": workflow_run_id,
        "archive": str(archive),
        "valid": record["valid"],
        "validation": validation,
    }


def command_run(args):
    config = validate_config(load_json(args.config))
    archive_root = private_root(args.evidence_root)
    mkdir_private(archive_root)
    fixtures = fixture_by_name(config, args.fixture)
    if args.repetitions < 1:
        raise RunnerError("repetitions must be positive")
    if args.dry_run:
        plan = []
        for fixture in fixtures:
            for repetition in range(1, args.repetitions + 1):
                spec = make_spec(fixture, args.scenario, repetition, args)
                plan.append({
                    "run_id": spec["run_id"],
                    "fixture": spec["fixture"],
                    "scenario": spec["scenario"],
                    "repetition": repetition,
                    "conditions": spec["conditions"],
                })
        print(json.dumps({"dry_run": True, "runs": plan}, indent=2, sort_keys=True))
        return 0
    if args.workflow_run_id and args.repetitions != 1:
        raise RunnerError("--workflow-run-id can only be used with one repetition")
    results = []
    for fixture in fixtures:
        for repetition in range(1, args.repetitions + 1):
            spec = make_spec(fixture, args.scenario, repetition, args)
            results.append(run_one(config, spec, archive_root, args, args.unsupported_reason))
    print(json.dumps({"runs": results}, indent=2, sort_keys=True))
    return 0 if all(not item["validation"]["errors"] for item in results) else 1


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


def command_validate(args):
    records = find_records(args.path)
    if not records:
        raise RunnerError("no run-record.json files found")
    report = []
    for path in records:
        try:
            document = load_json(path)
            validation = validate_record(document, require_measurements=not args.allow_missing_measurements)
        except RunnerError as error:
            validation = {"errors": [str(error)], "warnings": []}
        report.append({"path": str(path), **validation})
    print(json.dumps({"records": report}, indent=2, sort_keys=True))
    return 0 if all(not item["errors"] for item in report) else 1


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="dispatch and archive repeated workflow runs")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--evidence-root", required=True, type=Path)
    run.add_argument("--fixture", action="append", help="fixture name; default is every configured fixture")
    run.add_argument("--scenario", choices=sorted(EXPECTED_STAGES), default="create")
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--failure-type", default="none")
    run.add_argument("--build-policy", default="")
    run.add_argument("--metric-interval-seconds", type=float, default=10)
    run.add_argument("--operation-timeout-seconds", type=float, default=300)
    run.add_argument("--http-timeout-seconds", type=float, default=60)
    run.add_argument("--command-timeout", type=float, default=120)
    run.add_argument("--poll-interval", type=float, default=5)
    run.add_argument("--wait-timeout", type=float, default=3600)
    run.add_argument("--workflow-run-id", help="collect an existing workflow run instead of dispatching")
    run.add_argument("--unsupported-reason", default="", help="record an unsupported condition without dispatching")
    run.add_argument("--dry-run", action="store_true", help="print the run plan without dispatching or writing evidence")
    run.set_defaults(handler=command_run)

    validate = commands.add_parser("validate", help="validate one or more private run records")
    validate.add_argument("path", nargs="+", type=Path)
    validate.add_argument("--allow-missing-measurements", action="store_true")
    validate.set_defaults(handler=command_validate)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except RunnerError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
