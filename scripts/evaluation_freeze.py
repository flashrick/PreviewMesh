#!/usr/bin/env python3
"""Capture an immutable, credential-free evaluation configuration snapshot."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

import experiment_runner as runner


FREEZE_SCHEMA = "previewmesh-freeze-v1"
QUANTITY_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?[A-Za-z]*$")
CONFIG_FIELDS = (
    "project_size",
    "commit_version",
    "build_policy",
    "failure_type",
    "recovery_mode",
    "concurrency",
    "metric_interval_seconds",
    "operation_timeout_seconds",
    "http_timeout_seconds",
)


def unavailable(reason):
    return {"available": False, "reason": reason}


def available(value):
    return {"available": True, "value": value}


def read_command(command, cwd=None, timeout=10):
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.strip()


def git_snapshot(directory):
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return unavailable("repository directory is absent")
    revision = read_command(["git", "rev-parse", "HEAD"], cwd=directory)
    if not revision or not re.fullmatch(r"[0-9a-f]{40}", revision):
        return unavailable("repository revision could not be read")
    timestamp = read_command(["git", "show", "-s", "--format=%cI", "HEAD"], cwd=directory)
    status = read_command(["git", "status", "--porcelain"], cwd=directory)
    return available({
        "revision": revision,
        "committed_at": timestamp or "unavailable",
        "working_tree_clean": status == "",
    })


def text_version(path, key):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return unavailable(f"{path} could not be read")
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*[\"']?([^\"'\s]+)", text)
    if not match:
        return unavailable(f"{key} is not declared")
    return available(match.group(1))


def module_version(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return unavailable(f"{path} could not be read")
    match = re.search(r"(?m)^\s*go\s+(\S+)", text)
    if not match:
        return unavailable("go version is not declared")
    return available(match.group(1))


def chart_resource_defaults(path):
    try:
        import yaml
    except ImportError:
        return unavailable("PyYAML is unavailable; resource defaults were not parsed")
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return unavailable("chart values could not be parsed")
    resources = document.get("resources")
    if not isinstance(resources, dict):
        return unavailable("chart resource defaults are not declared")
    result = {}
    for section in ("requests", "limits"):
        values = resources.get(section, {})
        if not isinstance(values, dict):
            return unavailable(f"chart resource {section} is malformed")
        result[section] = {
            key: value for key, value in values.items()
            if key in {"cpu", "memory"} and isinstance(value, (str, int, float))
            and QUANTITY_RE.fullmatch(str(value))
        }
    return available(result)


def node_summary(document):
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        return unavailable("Kubernetes node response was invalid")
    nodes = []
    for item in document["items"]:
        status = item.get("status", {})
        info = status.get("nodeInfo", {})
        capacity = status.get("capacity", {})
        allocatable = status.get("allocatable", {})
        if not isinstance(capacity, dict) or not isinstance(allocatable, dict):
            return unavailable("Kubernetes node capacity was invalid")
        nodes.append({
            "kubelet_version": info.get("kubeletVersion", "unavailable"),
            "architecture": info.get("architecture", "unavailable"),
            "os_image": info.get("osImage", "unavailable"),
            "capacity": {key: capacity.get(key, "unavailable") for key in ("cpu", "memory", "ephemeral-storage")},
            "allocatable": {key: allocatable.get(key, "unavailable") for key in ("cpu", "memory", "ephemeral-storage")},
        })
    return available({"node_count": len(nodes), "nodes": nodes})


def cluster_snapshot():
    version_raw = read_command(["kubectl", "version", "--output=json"], timeout=15)
    nodes_raw = read_command(["kubectl", "get", "nodes", "-o", "json"], timeout=15)
    if version_raw is None:
        version = unavailable("kubectl version is unavailable; check kubeconfig permissions")
    else:
        try:
            document = json.loads(version_raw)
            server = document.get("serverVersion") or {}
            version = available({
                "git_version": server.get("gitVersion", "unavailable"),
                "major": server.get("major", "unavailable"),
                "minor": server.get("minor", "unavailable"),
            })
        except ValueError:
            version = unavailable("kubectl version returned invalid JSON")
    if nodes_raw is None:
        nodes = unavailable("Kubernetes node inventory is unavailable; check kubeconfig permissions")
    else:
        try:
            nodes = node_summary(json.loads(nodes_raw))
        except ValueError:
            nodes = unavailable("Kubernetes node inventory returned invalid JSON")
    return {"version": version, "nodes": nodes}


def config_snapshot(path):
    if not path:
        return unavailable("experiment config path was not supplied")
    try:
        config = runner.validate_config(runner.load_json(path))
    except runner.RunnerError:
        return unavailable("experiment config could not be validated")
    fixtures = []
    for fixture in config["fixtures"]:
        item = {"name": fixture["name"]}
        for field in ("project_size", "source_commit_sha", "image_digest", "build_policy"):
            if field in fixture:
                item[field] = fixture[field]
        conditions = fixture.get("conditions", {})
        if isinstance(conditions, dict):
            item["conditions"] = {key: conditions[key] for key in CONFIG_FIELDS if key in conditions}
        fixtures.append(item)
    return available({
        "workflow": config.get("workflow", "preview.yml"),
        "ref": config.get("ref", "main"),
        "fixtures": fixtures,
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new private freeze directory")
    parser.add_argument("--control-directory", type=Path, help="private control checkout")
    parser.add_argument("--experiment-config", type=Path, help="private runner config")
    parser.add_argument("--image-storage-method", default="", help="evaluated image storage, for example ghcr")
    args = parser.parse_args(argv)

    try:
        output = runner.private_root(args.output)
        if output.exists() and any(output.iterdir()):
            raise runner.RunnerError("freeze output must be a new empty directory")
        runner.mkdir_private(output)
    except runner.RunnerError as error:
        print(str(error), file=sys.stderr)
        return 2

    public_root = Path(__file__).resolve().parents[1]
    chart = public_root / "charts" / "preview"
    freeze = {
        "schema_version": FREEZE_SCHEMA,
        "captured_at_utc": runner.utc_now(),
        "source_repository": git_snapshot(public_root),
        "control_repository": git_snapshot(args.control_directory) if args.control_directory else unavailable("private control directory was not supplied"),
        "toolchain": {
            "go_module": module_version(public_root / "go.mod"),
            "chart_version": text_version(chart / "Chart.yaml", "version"),
            "chart_app_version": text_version(chart / "Chart.yaml", "appVersion"),
        },
        "cluster": cluster_snapshot(),
        "image_storage": available(args.image_storage_method) if args.image_storage_method else unavailable("image storage method was not supplied"),
        "resource_allocation": chart_resource_defaults(chart / "values.yaml"),
        "experiment_configuration": config_snapshot(args.experiment_config),
        "limitations": [],
    }
    for name, value in (
        ("control_repository", freeze["control_repository"]),
        ("cluster.version", freeze["cluster"]["version"]),
        ("cluster.nodes", freeze["cluster"]["nodes"]),
        ("experiment_configuration", freeze["experiment_configuration"]),
    ):
        if not value.get("available"):
            freeze["limitations"].append(f"{name}: {value['reason']}")
    freeze["status"] = "complete" if not freeze["limitations"] else "partial"
    runner.write_json_private(output / "freeze.json", freeze)
    manifest = runner.evidence_manifest(output)
    runner.write_json_private(output / "manifest-summary.json", manifest)
    print(json.dumps({"status": freeze["status"], "output": str(output), "limitations": freeze["limitations"]}, indent=2))
    return 0 if freeze["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
