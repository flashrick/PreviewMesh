#!/usr/bin/env python3
"""Repeat the current deployment of an owned preview against a live cluster.

Requires Go, Helm 3, kubectl, an authenticated KUBECONFIG, and a reachable
preview URL. Run from the project root. Evidence contains private runtime
identities; choose an output directory outside the public repository.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--attempts', type=int, default=3)
    args = parser.parse_args()
    require(args.attempts >= 2, 'At least two attempts are required')
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
        return {'identities': identities, 'annotations': namespace['metadata']['annotations']}

    baseline = snapshot()
    source = baseline['annotations']['previewmesh.local/source-repository']
    entries = json.loads(args.registry.read_text())
    registration = next(entry for entry in entries if entry['source_repository'] == source)
    prefix = f"pm-r{registration['repository_id']}-pr"
    require(ns.startswith(prefix) and ns[len(prefix):].isdigit(), 'Registry identity mismatch')
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
    common = ['--repository-id', registration['repository_id'], '--source-repository', source,
              '--pr', ns[len(prefix):], '--sha', sha, '--port', str(registration['port'])]
    records = []
    with tempfile.TemporaryDirectory(prefix='previewmesh-repeat-') as temporary:
        binary = str(Path(temporary) / 'previewmesh')
        subprocess.run(['go', 'build', '-o', binary, './cmd/previewmesh'], check=True)
        for attempt in range(args.attempts + 1):
            # Verify the existing endpoint before making any deployment request.
            command = 'verify' if attempt == 0 else 'deploy'
            result_path = args.output / f'{attempt}-{command}.json'
            command_args = [binary, command, *common, '--result-file', str(result_path),
                            '--evidence', str(args.output / 'stages.csv')]
            if command == 'deploy':
                command_args += ['--image', values['image'], '--pull-secret', values['imagePullSecret']]
            subprocess.run(command_args, check=True, stdout=subprocess.DEVNULL)
            result = json.loads(result_path.read_text())
            for key, expected in {'result': 'success', 'requested_sha': sha, 'served_sha': sha,
                                  'http_verification': 'success',
                                  'revision_verification': 'success'}.items():
                require(result[key] == expected, f'{command}: unexpected {key}')
            observed = snapshot()
            require(observed['identities'] == baseline['identities'], 'Resource identity changed')
            require(observed['annotations']['previewmesh.local/state'] == 'ready', 'Preview not ready')
            require(observed['annotations']['previewmesh.local/verified-sha'] == sha, 'Verified SHA changed')
            current_values = read_json('helm', 'get', 'values', ns, '-n', ns, '-o', 'json')
            require(current_values == values, 'Helm values changed')
            records.append({'attempt': attempt, 'command': command, **observed})
            (args.output / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')
            print(f'{command} {attempt}: verified SHA, stable resource UIDs and values; '
                  f"Helm revision {observed['annotations']['previewmesh.local/verified-revision']}", flush=True)


if __name__ == '__main__':
    # Keep newly written evidence private even when the caller has a broad umask.
    os.umask(0o077)
    main()
