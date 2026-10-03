#!/usr/bin/env python3
"""Collect private Kubernetes resource measurements without changing workloads."""

import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time


SCHEMA_VERSION = 1
MANAGED_LABEL = 'previewmesh.local/managed-by'
UNITS = {
    'cpu_cores': 'core', 'memory_bytes': 'byte',
    'helm_release_payload_bytes': 'byte',
    'pvc_requested_bytes': 'byte', 'pvc_capacity_bytes': 'byte',
}
SUFFIXES = {
    '': Decimal(1), 'n': Decimal('1e-9'), 'u': Decimal('1e-6'),
    'm': Decimal('1e-3'), 'k': Decimal('1e3'), 'K': Decimal('1e3'),
    'M': Decimal('1e6'), 'G': Decimal('1e9'), 'T': Decimal('1e12'),
    'P': Decimal('1e15'), 'E': Decimal('1e18'),
    **{unit + 'i': Decimal(1024) ** power
       for power, unit in enumerate('KMGTPE', 1)},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def quantity(value):
    """Normalize Kubernetes quantities; reject invalid observations, never coerce to zero."""
    match = re.fullmatch(r'([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)', str(value))
    if not match or match[2] not in SUFFIXES:
        raise ValueError('invalid Kubernetes quantity')
    try:
        result = float(Decimal(match[1]) * SUFFIXES[match[2]])
    except (InvalidOperation, OverflowError) as error:
        raise ValueError('invalid Kubernetes quantity') from error
    if not math.isfinite(result) or result < 0:
        raise ValueError('non-finite or negative quantity')
    return result


def unavailable(reason, error_class='unavailable'):
    return {'available': False, 'value': None, 'reason': reason, 'error_class': error_class}


def measured(value, unit, **details):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid measured value')
    return {'available': True, 'value': value, 'unit': unit, **details}


def resource_list(document):
    if not isinstance(document, dict) or not isinstance(document.get('items'), list):
        raise ValueError('invalid resource list')
    if any(not isinstance(item, dict) for item in document['items']):
        raise ValueError('invalid resource item')
    return document['items']


def duration_seconds(value):
    parts = re.findall(r'(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)', value)
    if not parts or ''.join(number + unit for number, unit in parts) != value:
        raise ValueError('invalid metrics window')
    scale = {'ns': 1e-9, 'us': 1e-6, 'µs': 1e-6, 'ms': 1e-3, 's': 1, 'm': 60, 'h': 3600}
    seconds = sum(float(number) * scale[unit] for number, unit in parts)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('invalid metrics window')
    return seconds


class Collector:
    def __init__(self, run_id, namespaces=(), timeout=10, max_age=60):
        self.run_id = run_id
        self.namespaces = set(namespaces)
        self.timeout = timeout
        self.max_age = max_age

    def query(self, *arguments):
        # Raw responses, including Helm Secrets, stay in memory and are never archived.
        try:
            result = subprocess.run(
                ['kubectl', f'--request-timeout={self.timeout}s', *arguments],
                capture_output=True, text=True, timeout=self.timeout + 2, check=False)
        except FileNotFoundError:
            return None, unavailable('kubectl executable missing', 'missing_dependency')
        except subprocess.TimeoutExpired:
            return None, unavailable('Kubernetes request timed out', 'timeout')
        if result.returncode:
            kind = 'forbidden' if 'forbidden' in result.stderr.lower() else 'request_failed'
            # Do not save arbitrary stderr: it may include sensitive response content.
            return None, unavailable('Kubernetes request did not succeed', kind)
        try:
            return json.loads(result.stdout), None
        except ValueError:
            return None, unavailable('Kubernetes returned invalid JSON', 'invalid_data')

    def listing(self, *arguments):
        result, error = self.query(*arguments)
        if error:
            return None, error
        try:
            return resource_list(result), None
        except ValueError:
            return None, unavailable('Kubernetes returned an invalid list', 'invalid_data')

    def metric_usage(self, items, expected, now):
        """Require every expected object, a recent metrics window, and valid quantities."""
        observations, cpu, memory = [], 0.0, 0.0
        try:
            by_name = {item.get('metadata', {}).get('name'): item for item in items}
            if len(by_name) != len(items):
                raise ValueError('duplicate metrics')
            for name in expected:
                item = by_name[name]
                timestamp = datetime.fromisoformat(item['timestamp'].replace('Z', '+00:00'))
                if timestamp.tzinfo is None:
                    raise ValueError('metrics timestamp has no timezone')
                age = (now - timestamp).total_seconds()
                window = item['window']
                duration_seconds(window)
                if age < -5 or age > self.max_age:
                    return (unavailable('Metrics timestamp outside allowed age', 'stale'),) * 2
                usage = [item['usage']] if 'usage' in item else [c['usage'] for c in item['containers']]
                if isinstance(expected, dict):
                    names = [c['name'] for c in item['containers']]
                    if not expected[name] or set(expected[name]) != set(names) or len(names) != len(set(names)):
                        raise ValueError('incomplete container metrics')
                if not usage:
                    raise ValueError('empty container usage')
                cpu += sum(quantity(entry['cpu']) for entry in usage)
                memory += sum(quantity(entry['memory']) for entry in usage)
                observations.append({'name': name, 'timestamp': item['timestamp'],
                                     'window': window, 'age_seconds': age})
            return (measured(cpu, 'core', observations=observations),
                    measured(memory, 'byte', observations=observations))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return (unavailable('Missing or invalid resource metrics', 'incomplete_metrics'),) * 2

    def storage(self, namespace):
        result = {
            'volume_used_bytes': unavailable('PVC capacity does not measure filesystem use', 'unsupported'),
            'registry_image_bytes': unavailable('Registry storage is outside Kubernetes measurement scope', 'unsupported'),
        }
        secrets, error = self.listing('get', 'secrets', '-n', namespace, '-l', 'owner=helm', '-o', 'json')
        if error:
            result.update(helm_release_payload_bytes=error, helm_history_count=error)
        else:
            try:
                # Count only release payload bytes; Secret credentials never leave memory.
                sizes = [len(base64.b64decode(s['data']['release'], validate=True)) for s in secrets]
                result['helm_release_payload_bytes'] = measured(sum(sizes), 'byte')
                result['helm_history_count'] = measured(len(secrets), 'object')
            except (KeyError, TypeError, ValueError):
                error = unavailable('Invalid Helm release payload', 'invalid_data')
                result.update(helm_release_payload_bytes=error, helm_history_count=error)
        claims, error = self.listing('get', 'persistentvolumeclaims', '-n', namespace, '-o', 'json')
        if error:
            result.update(pvc_requested_bytes=error, pvc_capacity_bytes=error)
        else:
            try:
                requested = sum(quantity(p['spec']['resources']['requests']['storage']) for p in claims)
                capacity = sum(quantity(p['status']['capacity']['storage'])
                               for p in claims if p.get('status', {}).get('phase') == 'Bound')
                result['pvc_requested_bytes'] = measured(requested, 'byte')
                result['pvc_capacity_bytes'] = measured(capacity, 'byte')
            except (KeyError, TypeError, ValueError):
                error = unavailable('Invalid PVC capacity data', 'invalid_data')
                result.update(pvc_requested_bytes=error, pvc_capacity_bytes=error)
        return result

    def sample(self, elapsed_seconds):
        try:
            return self._sample(elapsed_seconds)
        except (KeyError, TypeError, ValueError, AttributeError):
            # A malformed API object is a missing observation, not a zero or a lost run.
            error = unavailable('Invalid Kubernetes object structure', 'invalid_data')
            return {'schema_version': SCHEMA_VERSION, 'run_id': self.run_id,
                    'started_at_utc': utc_now(), 'ended_at_utc': utc_now(),
                    'elapsed_seconds': elapsed_seconds, 'scopes': {}, 'errors': [error],
                    'managed_namespace_count': error, 'running_preview_count': error}

    def _sample(self, elapsed_seconds):
        sample = {'schema_version': SCHEMA_VERSION, 'run_id': self.run_id,
                  'started_at_utc': utc_now(), 'elapsed_seconds': elapsed_seconds,
                  'scopes': {}, 'errors': []}
        scopes = sample['scopes']
        nodes, nodes_error = self.listing('get', 'nodes', '-o', 'json')
        metrics, metrics_error = self.listing('get', '--raw', '/apis/metrics.k8s.io/v1beta1/nodes')
        error = nodes_error or metrics_error
        if error:
            cpu = memory = error
        elif not nodes:
            cpu = memory = unavailable('No nodes returned', 'incomplete_inventory')
        else:
            cpu, memory = self.metric_usage(metrics, [n['metadata']['name'] for n in nodes], datetime.now(timezone.utc))
        scopes['cluster'] = {'identity': {'scope': 'cluster'}, 'measurements': {
            'cpu_cores': cpu, 'memory_bytes': memory,
            'host_disk_used_bytes': unavailable('Host filesystem measurement requires a separate collector', 'unsupported'),
        }}
        namespaces, error = self.listing('get', 'namespaces', '-l', f'{MANAGED_LABEL}=previewmesh', '-o', 'json')
        if error:
            sample['errors'].append({'operation': 'preview_inventory', **error})
            sample['managed_namespace_count'] = error
            sample['running_preview_count'] = error
            sample['ended_at_utc'] = utc_now()
            return sample
        sample['managed_namespace_count'] = measured(len(namespaces), 'namespace')
        pods, pods_error = self.listing('get', 'pods', '-A', '-o', 'json')
        pod_metrics, pod_metrics_error = self.listing('get', '--raw', '/apis/metrics.k8s.io/v1beta1/pods')
        managed_names = {n['metadata']['name'] for n in namespaces}
        if pods_error:
            sample['running_preview_count'] = pods_error
        else:
            running = {p['metadata']['namespace'] for p in pods
                       if p.get('status', {}).get('phase') == 'Running'}
            sample['running_preview_count'] = measured(len(running & managed_names), 'namespace')
        for requested in sorted(self.namespaces - managed_names):
            sample['errors'].append({'namespace': requested, **unavailable(
                'Requested namespace absent or not managed by PreviewMesh', 'missing_identity')})
        for namespace in namespaces:
            meta = namespace['metadata']
            name = meta['name']
            if self.namespaces and name not in self.namespaces:
                continue
            labels = meta.get('labels', {})
            identity = {'namespace': name, 'namespace_uid': meta['uid'],
                        'repository_id': labels.get('previewmesh.local/repository-id'),
                        'pr_number': labels.get('previewmesh.local/pr-number')}
            error = pods_error or pod_metrics_error
            if error:
                cpu = memory = error
            else:
                expected = {}
                for pod in pods:
                    if pod['metadata']['namespace'] != name or pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
                        continue
                    containers = {c['name'] for c in pod['spec']['containers']}
                    containers.update(c['name'] for c in pod.get('status', {}).get('initContainerStatuses', [])
                                      if c.get('state', {}).get('running') is not None)
                    expected[pod['metadata']['name']] = containers
                own_metrics = [p for p in pod_metrics if p['metadata'].get('namespace') == name]
                cpu, memory = self.metric_usage(own_metrics, expected, datetime.now(timezone.utc))
            measurements = {'cpu_cores': cpu, 'memory_bytes': memory, **self.storage(name)}
            # Prevent resource measurements from being attributed to a replacement namespace.
            current, error = self.query('get', 'namespace', name, '-o', 'json')
            if error or current.get('metadata', {}).get('uid') != meta['uid']:
                measurements = {key: unavailable('Namespace changed during collection', 'identity_changed')
                                for key in measurements}
            scopes[f'preview:{meta["uid"]}'] = {'identity': identity, 'measurements': measurements}
        sample['ended_at_utc'] = utc_now()
        return sample


def summarize(samples, interval, max_gap):
    """Integrate adjacent valid observations only; never bridge unavailable samples."""
    elapsed = samples[-1]['elapsed_seconds'] - samples[0]['elapsed_seconds'] if len(samples) > 1 else 0
    result = {'schema_version': SCHEMA_VERSION, 'run_id': samples[0]['run_id'],
              'samples': len(samples), 'requested_interval_seconds': interval,
              'observed_duration_seconds': elapsed, 'max_gap_seconds': max_gap,
              'integration': 'trapezoidal; adjacent available samples only', 'scopes': {},
              'limitations': ['Sampling can miss transient concurrency and resource peaks.',
                              'Node totals include system workloads and collection overhead.',
                              'Helm payload sizes exclude object metadata, database and filesystem overhead.',
                              'PVC requested/provisioned capacity is not filesystem usage.',
                              'Repeated recent metrics windows are retained, not independent observations.']}
    for count in ('managed_namespace_count', 'running_preview_count'):
        available = [s[count]['value'] for s in samples if s.get(count, {}).get('available')]
        result[f'max_observed_{count}'] = max(available) if available else None
        result[f'{count}_unavailable_samples'] = len(samples) - len(available)
    for sample in samples:
        for key, scope in sample['scopes'].items():
            result['scopes'].setdefault(key, {'identity': scope['identity'], 'measurements': {}})
    for scope_key, scope in result['scopes'].items():
        for metric, unit in UNITS.items():
            values = [s['scopes'].get(scope_key, {}).get('measurements', {}).get(metric, {}) for s in samples]
            integral, covered = 0.0, 0.0
            for index in range(1, len(samples)):
                delta = samples[index]['elapsed_seconds'] - samples[index - 1]['elapsed_seconds']
                a, b = values[index - 1], values[index]
                if 0 < delta <= max_gap and a.get('available') and b.get('available'):
                    integral += (a['value'] + b['value']) / 2 * delta / 60
                    covered += delta
            valid = [value['value'] for value in values if value.get('available')]
            scope['measurements'][metric] = {
                'unit': unit, 'available_samples': len(valid), 'unavailable_samples': len(samples) - len(valid),
                'minimum': min(valid) if valid else None, 'maximum': max(valid) if valid else None,
                'integral': integral if covered else None, 'integral_unit': f'{unit}_minute',
                'covered_seconds': covered, 'uncovered_seconds': max(0, elapsed - covered),
                'complete': bool(covered) and math.isclose(covered, elapsed),
            }
    return result


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a positive finite number')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', required=True, type=Path,
                        help='New private evidence directory outside the public checkout')
    parser.add_argument('--namespace', action='append', default=[], help='Repeat to select previews; default: all managed previews')
    parser.add_argument('--samples', type=int, default=13)
    parser.add_argument('--interval', type=positive, default=10)
    parser.add_argument('--timeout', type=positive, default=10)
    parser.add_argument('--max-metric-age', type=positive, default=60)
    parser.add_argument('--max-gap', type=positive, help='Largest integrable gap; default: twice interval')
    args = parser.parse_args()
    if args.samples < 2 or not args.run_id.strip():
        parser.error('at least two samples and a nonempty run ID are required')
    output = args.output.resolve()
    public_root = Path(__file__).resolve().parents[1]
    if output == public_root or public_root in output.parents:
        parser.error('evidence output must be outside the public checkout')
    if not math.isfinite(args.interval * args.samples):
        parser.error('sampling duration is too large')
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    collector = Collector(args.run_id, args.namespace, args.timeout, args.max_metric_age)
    samples, start = [], time.monotonic()
    interrupted = False
    try:
        with (output / 'samples.jsonl').open('x') as stream:
            for index in range(args.samples):
                if index:
                    time.sleep(max(0, start + index * args.interval - time.monotonic()))
                sample = collector.sample(time.monotonic() - start)
                samples.append(sample)
                stream.write(json.dumps(sample, allow_nan=False) + '\n')
                stream.flush()
    except KeyboardInterrupt:
        interrupted = True
    if samples:
        summary = summarize(samples, args.interval, args.max_gap or args.interval * 2)
        summary['interrupted'] = interrupted
        summary['requested_samples'] = args.samples
        (output / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'run_id': args.run_id, 'samples': len(samples), 'interrupted': interrupted}))
    return 130 if interrupted else 0


if __name__ == '__main__':
    raise SystemExit(main())
