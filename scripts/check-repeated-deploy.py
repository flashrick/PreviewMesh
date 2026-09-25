#!/usr/bin/env python3
"""Repeat deployment or test missing-resource repair on an owned live preview.

Requires Go, Helm 3, kubectl, an authenticated KUBECONFIG, and a reachable
preview URL. Run from the project root. Evidence contains private runtime
identities; choose an output directory outside the public repository.
Repair mode interrupts the preview by deleting each resource in turn. Run only
when no other deployment or cleanup is modifying the selected preview.
Duplicate checks require cluster-wide list access for Namespaces, Deployments,
Services, and Ingresses and a quiet cluster with no unrelated resource churn.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def run(*args):
    return subprocess.check_output(args, text=True)


def read_json(*args):
    return json.loads(run(*args))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_duplicates(items, ns, repo_id, pr):
    """Check identity aliases across the cluster, including objects outside ns."""
    matches = {kind: [] for kind in ('Namespace', 'Deployment', 'Service', 'Ingress')}
    inventory = []
    for item in items:
        kind, metadata = item['kind'], item['metadata']
        if kind not in matches:
            continue
        name = metadata['name']
        namespace = metadata.get('namespace', '')
        labels = metadata.get('labels', {})
        annotations = metadata.get('annotations', {})
        inventory.append([kind, namespace, name])
        if kind == 'Namespace':
            belongs = name == ns or (
                labels.get('previewmesh.local/repository-id') == str(repo_id)
                and labels.get('previewmesh.local/pr-number') == str(pr))
        else:
            # Count every object in the preview, even if its labels are missing.
            belongs = (namespace == ns or name == ns
                       or labels.get('app.kubernetes.io/instance') == ns
                       or annotations.get('meta.helm.sh/release-name') == ns)
        if belongs:
            matches[kind].append([namespace, name])
    for kind, objects in matches.items():
        expected = [['' if kind == 'Namespace' else ns, ns]]
        require(objects == expected, f'Duplicate or missing {kind} for selected preview')
    return sorted(inventory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--attempts', type=int, default=3)
    parser.add_argument('--check-duplicates', action='store_true',
                        help='Check cluster-wide identities and reject added or removed objects')
    parser.add_argument('--repair-missing', action='store_true',
                        help='Delete Service, Deployment, and Ingress in turn and verify repair')
    args = parser.parse_args()
    require(args.repair_missing or args.attempts >= 2, 'At least two attempts are required')
    # Refuse to overwrite evidence from another run.
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    ns = args.namespace

    def snapshot():
        namespace = read_json('kubectl', 'get', 'namespace', ns, '-o', 'json')
        resources = read_json('kubectl', 'get', 'deployment,service,ingress',
                              '-n', ns, '-o', 'json')['items']
        identities = {}
        for kind in ('Deployment', 'Service', 'Ingress'):
            objects = [item for item in resources if item['kind'] == kind]
            require(len(objects) == 1 and objects[0]['metadata']['name'] == ns,
                    f'Expected exactly one {kind} named {ns}')
            identities[kind] = objects[0]['metadata']['uid']
        identities['Namespace'] = namespace['metadata']['uid']
        return {'identities': identities, 'annotations': namespace['metadata']['annotations'],
                'labels': namespace['metadata'].get('labels', {})}

    baseline = snapshot()
    source = baseline['annotations']['previewmesh.local/source-repository']
    entries = json.loads(args.registry.read_text())
    registration = next(entry for entry in entries if entry['source_repository'] == source)
    prefix = f"pm-r{registration['repository_id']}-pr"
    require(ns.startswith(prefix) and ns[len(prefix):].isdigit(), 'Registry identity mismatch')
    for key, expected in {'managed-by': 'previewmesh',
                          'repository-id': str(registration['repository_id']),
                          'pr-number': ns[len(prefix):]}.items():
        require(baseline['labels'].get('previewmesh.local/' + key) == expected,
                f'Namespace ownership mismatch: {key}')
    values = read_json('helm', 'get', 'values', ns, '-n', ns, '-o', 'json')
    # The deploy CLI only preserves these values; reject custom overrides before mutation.
    require(set(values) == {'commitSHA', 'hostname', 'image', 'imagePullSecret', 'port'},
            'Release has unsupported Helm overrides; refusing to change its configuration')
    require(values['hostname'] == ns + '.preview.test', 'Unexpected preview hostname')
    sha = values['commitSHA']
    require(baseline['annotations']['previewmesh.local/state'] == 'ready', 'Preview is not ready')
    require(baseline['annotations']['previewmesh.local/verified-sha'] == sha,
            'Current release is not the verified revision')
    require(values['port'] == registration['port'], 'Registered port mismatch')
    inventory_number = 0

    def inventory():
        nonlocal inventory_number
        objects = read_json('kubectl', 'get', 'namespace,deployment,service,ingress',
                            '--all-namespaces', '-o', 'json')
        # Retain failed observations too, so a duplicate can be investigated.
        (args.output / f'inventory-{inventory_number}.json').write_text(
            json.dumps(objects, indent=2) + '\n')
        inventory_number += 1
        return check_duplicates(objects['items'], ns, registration['repository_id'], ns[len(prefix):])

    baseline_inventory = inventory() if args.check_duplicates else None
    common = ['--repository-id', registration['repository_id'], '--source-repository', source,
              '--pr', ns[len(prefix):], '--sha', sha, '--port', str(registration['port'])]
    records = []
    scenarios = ['Service', 'Deployment', 'Ingress'] if args.repair_missing else [None] * args.attempts
    # Check deletion permissions before interrupting a working preview.
    if args.repair_missing:
        for kind in scenarios:
            require(run('kubectl', 'auth', 'can-i', 'delete', kind.lower(), '-n', ns).strip() == 'yes',
                    f'Cannot delete {kind}')
    (args.output / 'desired.json').write_text(json.dumps(values, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='previewmesh-repeat-') as temporary:
        binary = str(Path(temporary) / 'previewmesh')
        subprocess.run(['go', 'build', '-o', binary, './cmd/previewmesh'], check=True)
        previous = baseline
        for attempt, missing_kind in enumerate([None, *scenarios]):
            # Verify the existing endpoint before making any deployment request.
            command = 'verify' if attempt == 0 else 'deploy'
            result_path = args.output / f'{attempt}-{command}.json'
            command_args = [binary, command, *common, '--result-file', str(result_path),
                            '--evidence', str(args.output / 'stages.csv')]
            if command == 'deploy':
                command_args += ['--image', values['image'], '--pull-secret', values['imagePullSecret']]
            if missing_kind:
                # Save the healthy state before introducing a single missing resource.
                (args.output / f'{attempt}-before.json').write_text(json.dumps(previous, indent=2) + '\n')
                try:
                    subprocess.run(['kubectl', 'delete', missing_kind.lower(), ns, '-n', ns,
                                    '--wait=true', '--timeout=60s'], check=True)
                    absent = run('kubectl', 'get', missing_kind.lower(), ns, '-n', ns,
                                 '--ignore-not-found', '-o', 'json')
                    require(not absent.strip(), f'{missing_kind} still exists after deletion')
                    resources = read_json('kubectl', 'get', 'deployment,service,ingress',
                                          '-n', ns, '-o', 'json')
                    (args.output / f'{attempt}-missing.json').write_text(json.dumps(resources, indent=2) + '\n')
                finally:
                    # Even a failed absence check must attempt to restore the preview.
                    subprocess.run(command_args, check=True, stdout=subprocess.DEVNULL)
            else:
                subprocess.run(command_args, check=True, stdout=subprocess.DEVNULL)
            result = json.loads(result_path.read_text())
            for key, expected in {'result': 'success', 'requested_sha': sha, 'served_sha': sha,
                                  'http_verification': 'success',
                                  'revision_verification': 'success'}.items():
                require(result[key] == expected, f'{command}: unexpected {key}')
            observed = snapshot()
            if args.check_duplicates:
                # An unlabelled new object anywhere must also fail the check.
                require(inventory() == baseline_inventory, 'Cluster resource names changed')
            for kind, uid in previous['identities'].items():
                if kind == missing_kind:
                    require(observed['identities'][kind] != uid, f'{kind} was not recreated')
                else:
                    require(observed['identities'][kind] == uid, f'Unexpected {kind} identity change')
            require(observed['annotations']['previewmesh.local/state'] == 'ready', 'Preview not ready')
            require(observed['annotations']['previewmesh.local/verified-sha'] == sha, 'Verified SHA changed')
            current_values = read_json('helm', 'get', 'values', ns, '-n', ns, '-o', 'json')
            require(current_values == values, 'Helm values changed')
            records.append({'attempt': attempt, 'command': command,
                            'missing_resource': missing_kind,
                            'cluster_inventory_unchanged': True if args.check_duplicates else None,
                            **observed})
            (args.output / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')
            previous = observed
            action = f'restored {missing_kind}' if missing_kind else 'stable resource UIDs'
            print(f'{command} {attempt}: verified SHA, {action} and values; '
                  f"Helm revision {observed['annotations']['previewmesh.local/verified-revision']}", flush=True)


if __name__ == '__main__':
    # Keep newly written evidence private even when the caller has a broad umask.
    os.umask(0o077)
    main()
