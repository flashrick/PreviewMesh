#!/usr/bin/env python3
"""Boundary and aggregation tests for collect-resources.py."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_resources", ROOT / "scripts/collect-resources.py"
)
COLLECTOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COLLECTOR)


def ok(document):
    return 0, json.dumps(document), ""


def failed(message):
    return 1, "", message


def sample_value(value, unit):
    return COLLECTOR.measured(value, unit)


class FakeKubectl:
    """Return JSON fixtures at the subprocess boundary and retain call history."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, args, **kwargs):
        key = tuple(args[2:])
        with self.lock:
            self.calls.append(tuple(args))
        response = self.responses.get(key)
        if response is None:
            raise AssertionError(f"unexpected kubectl call: {key!r}")
        if callable(response):
            response = response(args)
        return subprocess.CompletedProcess(args, response[0], response[1], response[2])


def metric(name, timestamp, cpu="1", memory="1Mi", window="30s", containers=None):
    item = {
        "metadata": {"name": name},
        "timestamp": timestamp,
        "window": window,
    }
    if containers is None:
        item["usage"] = {"cpu": cpu, "memory": memory}
    else:
        item["containers"] = containers
    return item


def container_metric(name, cpu="1", memory="1Mi"):
    return {"name": name, "usage": {"cpu": cpu, "memory": memory}}


def metrics_responses(namespace="pm-r12-pr3", uid="uid-1", current_uid=None,
                      node_metrics=None, pod_metrics=None, secrets=None, claims=None,
                      pods=None):
    """Build the complete read-only API response set used by Collector.sample."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    node_metrics = node_metrics or [metric("node-a", now, cpu="500m", memory="1Gi")]
    pod_metrics = pod_metrics or [metric(
        "pod-a", now, containers=[container_metric("app", "250m", "32Mi")]
    )]
    pods = pods or [{
        "metadata": {"name": "pod-a", "namespace": namespace},
        "spec": {"containers": [{"name": "app"}]},
        "status": {"phase": "Running"},
    }]
    secrets = secrets if secrets is not None else []
    claims = claims if claims is not None else []
    current_uid = current_uid or uid
    return {
        ("get", "nodes", "-o", "json"): ok({"items": [{"metadata": {"name": "node-a"}}]}),
        ("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes"): ok({"items": node_metrics}),
        ("get", "namespaces", "-l", "previewmesh.local/managed-by=previewmesh", "-o", "json"): ok({
            "items": [{"metadata": {
                "name": namespace,
                "uid": uid,
                "labels": {
                    "previewmesh.local/managed-by": "previewmesh",
                    "previewmesh.local/repository-id": "12",
                    "previewmesh.local/pr-number": "3",
                },
            }}]
        }),
        ("get", "pods", "-A", "-o", "json"): ok({"items": pods}),
        ("get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"): ok({"items": [
            dict(item, metadata=dict(item["metadata"], namespace=namespace))
            for item in pod_metrics
        ]}),
        ("get", "secrets", "-n", namespace, "-l", "owner=helm", "-o", "json"): ok({"items": secrets}),
        ("get", "persistentvolumeclaims", "-n", namespace, "-o", "json"): ok({"items": claims}),
        ("get", "namespace", namespace, "-o", "json"): ok({"metadata": {"uid": current_uid}}),
    }


def basic_sample(run_id, elapsed, namespace="pm-r12-pr3", uid="uid-1", cpu=1.0):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "started_at_utc": "2026-10-03T00:00:00.000000Z",
        "ended_at_utc": "2026-10-03T00:00:01.000000Z",
        "elapsed_seconds": elapsed,
        "errors": [],
        "managed_namespace_count": sample_value(1, "namespace"),
        "running_preview_count": sample_value(1, "namespace"),
        "scopes": {
            f"preview:{uid}": {
                "identity": {"namespace": namespace, "namespace_uid": uid},
                "measurements": {
                    "cpu_cores": sample_value(cpu, "core"),
                    "memory_bytes": sample_value(cpu * 100, "byte"),
                    "helm_release_payload_bytes": sample_value(cpu * 10, "byte"),
                    "pvc_requested_bytes": sample_value(cpu * 20, "byte"),
                    "pvc_capacity_bytes": sample_value(cpu * 15, "byte"),
                },
            }
        },
    }


class QuantityAndMetricTests(unittest.TestCase):
    def test_quantity_accepts_cpu_memory_and_exponential_forms(self):
        self.assertAlmostEqual(COLLECTOR.quantity("500m"), 0.5)
        self.assertAlmostEqual(COLLECTOR.quantity("1e3m"), 1.0)
        self.assertAlmostEqual(COLLECTOR.quantity("1.5Gi"), 1.5 * 1024 ** 3)
        self.assertAlmostEqual(COLLECTOR.quantity("2E-3"), 0.002)

    def test_quantity_rejects_invalid_negative_and_nonfinite_values(self):
        for value in ("", "NaN", "inf", "-1", "1Zi", "1e9999"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    COLLECTOR.quantity(value)

    def test_metric_usage_sums_containers_and_keeps_observation_metadata(self):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        items = [metric("node-a", "2026-10-03T00:00:00Z", containers=[
            container_metric("system", "250m", "32Mi"),
            container_metric("app", "1e3m", "1.5Gi"),
        ])]
        cpu, memory = COLLECTOR.Collector("run", max_age=60).metric_usage(
            items, ["node-a"], now
        )
        self.assertTrue(cpu["available"])
        self.assertAlmostEqual(cpu["value"], 1.25)
        self.assertAlmostEqual(memory["value"], 32 * 1024 ** 2 + 1.5 * 1024 ** 3)
        self.assertEqual(cpu["observations"][0]["window"], "30s")

    def test_metric_usage_rejects_missing_one_of_two_expected_containers(self):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        item = metric("pod-a", "2026-10-03T00:00:00Z", containers=[
            container_metric("app")
        ])
        cpu, memory = COLLECTOR.Collector("run").metric_usage(
            [item], {"pod-a": {"app", "sidecar"}}, now
        )
        self.assertFalse(cpu["available"])
        self.assertEqual(cpu["error_class"], "incomplete_metrics")
        self.assertFalse(memory["available"])

    def test_metric_usage_rejects_duplicate_or_extra_container_metrics(self):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        duplicate = metric("pod-a", "2026-10-03T00:00:00Z", containers=[
            container_metric("app"), container_metric("app")
        ])
        extra = metric("pod-b", "2026-10-03T00:00:00Z", containers=[
            container_metric("app"), container_metric("unexpected")
        ])
        collector = COLLECTOR.Collector("run")
        for item, expected in ((duplicate, {"pod-a": {"app"}}),
                               (extra, {"pod-b": {"app"}})):
            with self.subTest(item=item):
                cpu, memory = collector.metric_usage([item], expected, now)
                self.assertFalse(cpu["available"])
                self.assertEqual(cpu["error_class"], "incomplete_metrics")
                self.assertFalse(memory["available"])

    def test_metric_usage_rejects_stale_and_malformed_windows(self):
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        collector = COLLECTOR.Collector("run", max_age=30)
        stale = metric("node-a", "2026-10-02T23:00:00Z")
        invalid_window = metric("node-a", "2026-10-03T00:00:00Z", window="not-a-duration")
        for item, error_class in ((stale, "stale"), (invalid_window, "incomplete_metrics")):
            with self.subTest(error_class=error_class):
                cpu, memory = collector.metric_usage([item], ["node-a"], now)
                self.assertFalse(cpu["available"])
                self.assertEqual(cpu["error_class"], error_class)
                self.assertFalse(memory["available"])


class QueryAndStorageTests(unittest.TestCase):
    def test_forbidden_query_is_explicit_and_does_not_retain_stderr(self):
        fake = FakeKubectl({
            ("get", "nodes", "-o", "json"): failed("forbidden: secret-token-must-not-escape")
        })
        with patch.object(COLLECTOR.subprocess, "run", side_effect=fake):
            document, error = COLLECTOR.Collector("run").query("get", "nodes", "-o", "json")
        self.assertIsNone(document)
        self.assertEqual(error["error_class"], "forbidden")
        self.assertNotIn("secret-token", json.dumps(error))

    def test_malformed_resource_list_is_invalid_data(self):
        fake = FakeKubectl({
            ("get", "nodes", "-o", "json"): ok({"items": {}})
        })
        with patch.object(COLLECTOR.subprocess, "run", side_effect=fake):
            values, error = COLLECTOR.Collector("run").listing("get", "nodes", "-o", "json")
        self.assertIsNone(values)
        self.assertEqual(error["error_class"], "invalid_data")

    def test_helm_payload_bytes_exclude_secret_contents_and_metadata(self):
        release_a = b"release-payload"
        release_b = b"two"
        fake = FakeKubectl({
            ("get", "secrets", "-n", "pm-r12-pr3", "-l", "owner=helm", "-o", "json"): ok({
                "items": [
                    {"data": {"release": base64.b64encode(release_a).decode(),
                              "token": base64.b64encode(b"PRIVATE-CREDENTIAL").decode()}},
                    {"data": {"release": base64.b64encode(release_b).decode()}},
                ]
            }),
            ("get", "persistentvolumeclaims", "-n", "pm-r12-pr3", "-o", "json"): ok({"items": []}),
        })
        with patch.object(COLLECTOR.subprocess, "run", side_effect=fake):
            storage = COLLECTOR.Collector("run").storage("pm-r12-pr3")
        self.assertEqual(storage["helm_release_payload_bytes"]["value"], len(release_a) + len(release_b))
        self.assertEqual(storage["helm_history_count"]["value"], 2)
        self.assertNotIn("PRIVATE-CREDENTIAL", json.dumps(storage))
        self.assertFalse(storage["volume_used_bytes"]["available"])
        self.assertEqual(storage["volume_used_bytes"]["error_class"], "unsupported")

    def test_pvc_requested_and_bound_capacity_are_distinct(self):
        fake = FakeKubectl({
            ("get", "secrets", "-n", "pm-r12-pr3", "-l", "owner=helm", "-o", "json"): ok({"items": []}),
            ("get", "persistentvolumeclaims", "-n", "pm-r12-pr3", "-o", "json"): ok({"items": [
                {"spec": {"resources": {"requests": {"storage": "10Gi"}}},
                 "status": {"phase": "Bound", "capacity": {"storage": "8Gi"}}},
                {"spec": {"resources": {"requests": {"storage": "2Gi"}}},
                 "status": {"phase": "Pending"}},
            ]}),
        })
        with patch.object(COLLECTOR.subprocess, "run", side_effect=fake):
            storage = COLLECTOR.Collector("run").storage("pm-r12-pr3")
        self.assertEqual(storage["pvc_requested_bytes"]["value"], 12 * 1024 ** 3)
        self.assertEqual(storage["pvc_capacity_bytes"]["value"], 8 * 1024 ** 3)


class SampleAndAggregationTests(unittest.TestCase):
    def test_namespace_uid_replacement_invalidates_all_preview_measurements(self):
        fake = FakeKubectl(metrics_responses(current_uid="uid-replacement"))
        with patch.object(COLLECTOR.subprocess, "run", side_effect=fake):
            sample = COLLECTOR.Collector("run", namespaces=["pm-r12-pr3"]).sample(4.0)
        scope = sample["scopes"]["preview:uid-1"]
        self.assertEqual(scope["identity"]["namespace_uid"], "uid-1")
        for name, measurement in scope["measurements"].items():
            with self.subTest(name=name):
                self.assertFalse(measurement["available"])
                self.assertEqual(measurement["error_class"], "identity_changed")

    def test_malformed_object_becomes_explicit_invalid_sample(self):
        responses = metrics_responses()
        responses[("get", "pods", "-A", "-o", "json")] = ok({"items": [{"metadata": []}]})
        fake = FakeKubectl(responses)
        with patch.object(COLLECTOR.subprocess, "run", side_effect=fake):
            sample = COLLECTOR.Collector("run").sample(0.0)
        self.assertEqual(sample["scopes"], {})
        self.assertFalse(sample["managed_namespace_count"]["available"])
        self.assertEqual(sample["managed_namespace_count"]["error_class"], "invalid_data")
        self.assertEqual(sample["errors"][0]["error_class"], "invalid_data")

    def test_forbidden_or_stale_metrics_are_not_zero_filled(self):
        unavailable = COLLECTOR.unavailable("Kubernetes request did not succeed", "forbidden")
        samples = [basic_sample("run", 0.0, cpu=2.0), basic_sample("run", 10.0, cpu=4.0)]
        samples[1]["scopes"]["preview:uid-1"]["measurements"]["cpu_cores"] = unavailable
        summary = COLLECTOR.summarize(samples, 10.0, 20.0)
        cpu = summary["scopes"]["preview:uid-1"]["measurements"]["cpu_cores"]
        self.assertIsNone(cpu["integral"])
        self.assertEqual(cpu["available_samples"], 1)
        self.assertEqual(cpu["unavailable_samples"], 1)
        self.assertEqual(cpu["covered_seconds"], 0)
        self.assertEqual(cpu["uncovered_seconds"], 10.0)

    def test_trapezoid_integration_keeps_partial_coverage_and_gaps(self):
        samples = [
            basic_sample("run", 0.0, cpu=1.0),
            basic_sample("run", 10.0, cpu=3.0),
            basic_sample("run", 20.0, cpu=0.0),
            basic_sample("run", 40.0, cpu=7.0),
        ]
        samples[2]["scopes"]["preview:uid-1"]["measurements"]["cpu_cores"] = COLLECTOR.unavailable(
            "stale", "stale"
        )
        summary = COLLECTOR.summarize(samples, 10.0, 15.0)
        cpu = summary["scopes"]["preview:uid-1"]["measurements"]["cpu_cores"]
        self.assertAlmostEqual(cpu["integral"], 20 / 60)
        self.assertEqual(cpu["covered_seconds"], 10.0)
        self.assertEqual(cpu["uncovered_seconds"], 30.0)
        self.assertFalse(cpu["complete"])
        self.assertEqual(cpu["minimum"], 1.0)
        self.assertEqual(cpu["maximum"], 7.0)


class ConcurrencyAndCLITests(unittest.TestCase):
    def test_independent_collectors_can_sample_concurrently(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        namespaces = [
            {"metadata": {"name": "pm-r12-pr3", "uid": "uid-a", "labels": {}}},
            {"metadata": {"name": "pm-r13-pr4", "uid": "uid-b", "labels": {}}},
        ]
        pods = [
            {"metadata": {"name": "pod-a", "namespace": "pm-r12-pr3"},
             "spec": {"containers": [{"name": "app"}]}, "status": {"phase": "Running"}},
            {"metadata": {"name": "pod-b", "namespace": "pm-r13-pr4"},
             "spec": {"containers": [{"name": "app"}]}, "status": {"phase": "Running"}},
        ]
        pod_metrics = [
            dict(metric("pod-a", now, containers=[container_metric("app")]),
                 metadata={"name": "pod-a", "namespace": "pm-r12-pr3"}),
            dict(metric("pod-b", now, containers=[container_metric("app")]),
                 metadata={"name": "pod-b", "namespace": "pm-r13-pr4"}),
        ]

        def shared_run(args, **kwargs):
            key = tuple(args[2:])
            if key == ("get", "nodes", "-o", "json"):
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    "items": [{"metadata": {"name": "node-a"}}]
                }), "")
            if key == ("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes"):
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    "items": [metric("node-a", now)]
                }), "")
            if key == ("get", "namespaces", "-l", "previewmesh.local/managed-by=previewmesh", "-o", "json"):
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": namespaces}), "")
            if key == ("get", "pods", "-A", "-o", "json"):
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": pods}), "")
            if key == ("get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"):
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": pod_metrics}), "")
            if key[0:2] in (("get", "secrets"), ("get", "persistentvolumeclaims")):
                return subprocess.CompletedProcess(args, 0, '{"items": []}', "")
            if key[0:2] == ("get", "namespace"):
                uid = {"pm-r12-pr3": "uid-a", "pm-r13-pr4": "uid-b"}[key[2]]
                return subprocess.CompletedProcess(args, 0, json.dumps({"metadata": {"uid": uid}}), "")
            raise AssertionError(f"unexpected concurrent kubectl call: {key!r}")

        def run_one(run_id, namespace, uid):
            return COLLECTOR.Collector(run_id, namespaces=[namespace]).sample(1.0)

        cases = [("run-a", "pm-r12-pr3", "uid-a"), ("run-b", "pm-r13-pr4", "uid-b")]
        with patch.object(COLLECTOR.subprocess, "run", side_effect=shared_run):
            with ThreadPoolExecutor(max_workers=2) as pool:
                samples = list(pool.map(lambda case: run_one(*case), cases))
        self.assertEqual({sample["run_id"] for sample in samples}, {"run-a", "run-b"})
        self.assertEqual(
            {next(key for key in sample["scopes"] if key.startswith("preview:")) for sample in samples},
            {"preview:uid-a", "preview:uid-b"},
        )

    def run_main(self, args):
        stdout = io.StringIO()
        with patch.object(sys, "argv", [str(ROOT / "scripts/collect-resources.py"), *args]), \
             patch("sys.stdout", stdout):
            result = COLLECTOR.main()
        return result, stdout.getvalue()

    def test_cli_rejects_invalid_sampling_and_public_output(self):
        with self.assertRaises(SystemExit) as invalid_samples:
            self.run_main(["--run-id", "run", "--output", "/tmp/unused", "--samples", "1"])
        self.assertEqual(invalid_samples.exception.code, 2)
        with self.assertRaises(SystemExit) as public_output:
            self.run_main(["--run-id", "run", "--output", str(ROOT / "evidence")])
        self.assertEqual(public_output.exception.code, 2)

    def test_cli_creates_private_jsonl_and_summary_files(self):
        class FakeCollector:
            def __init__(self, run_id, namespaces, timeout, max_age):
                self.run_id = run_id

            def sample(self, elapsed):
                return basic_sample(self.run_id, elapsed)

        with tempfile.TemporaryDirectory(prefix="previewmesh-collector-test-") as directory:
            output = Path(directory) / "evidence"
            with patch.object(COLLECTOR, "Collector", FakeCollector), \
                 patch.object(COLLECTOR.time, "sleep", return_value=None):
                result, stdout = self.run_main([
                    "--run-id", "run-private", "--output", str(output),
                    "--samples", "2", "--interval", "0.01",
                ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout)["samples"], 2)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "samples.jsonl").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((output / "summary.json").stat().st_mode), 0o600)
            rows = [json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads((output / "summary.json").read_text())["run_id"], "run-private")

    def test_cli_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory(prefix="previewmesh-collector-test-") as directory:
            output = Path(directory) / "existing"
            output.mkdir()
            sentinel = output / "sentinel"
            sentinel.write_text("keep")
            with self.assertRaises(FileExistsError):
                self.run_main(["--run-id", "run", "--output", str(output), "--samples", "2"])
            self.assertEqual(sentinel.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
