"""Exercise credential replacement offline, without sudo or a real cluster."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='previewmesh-setup-') as directory:
    work = Path(directory)
    (work / 'scripts').mkdir()
    shutil.copy(root / 'scripts/configure-runner.py', work / 'scripts')
    subprocess.run(['git', 'init', '-q', str(work)], check=True)
    (work / '.gitignore').write_text('/config/runner.yaml\n/config/.runner-*\n')
    (work / 'config').mkdir()
    config = work / 'config/runner.yaml'
    mock = work / 'tools'
    mock.mkdir()
    (mock / 'sudo').write_text('''#!/usr/bin/env python3
import json, os, sys
if 'view' in sys.argv:
    print(json.dumps({'clusters': [{'cluster': {'server': 'https://localhost:6443',
          'certificate-authority-data': 'test-ca'}}], 'users': [{'user': {'token': 'admin-secret'}}]}))
else:
    if os.environ.get('SCENARIO') == 'token_failure': sys.exit(1)
    print('runner-secret')
''')
    (mock / 'kubectl').write_text('''#!/usr/bin/env python3
import os, sys
if os.environ.get('SCENARIO') == 'connection_failure': sys.exit(1)
if sys.argv[-1] == 'clusterroles' and os.environ.get('SCENARIO') != 'overprivileged':
    print('no')
    sys.exit(1)
print('yes')
''')
    for tool in mock.iterdir():
        tool.chmod(0o700)
    env = dict(os.environ, PATH=str(mock) + os.pathsep + os.environ['PATH'],
               PREVIEWMESH_RUNNER_CONFIG=str(config))
    for scenario in ['success', 'token_failure', 'connection_failure', 'overprivileged']:
        config.write_text('previous config')
        result = subprocess.run(['python3', str(work / 'scripts/configure-runner.py')],
                                env=dict(env, SCENARIO=scenario), capture_output=True, text=True)
        assert 'admin-secret' not in result.stdout + result.stderr
        assert 'runner-secret' not in result.stdout + result.stderr
        assert not list(config.parent.glob('.runner-*'))
        if scenario == 'success':
            assert result.returncode == 0, result.stderr
            value = json.loads(config.read_text())
            assert value['users'] == [{'name': 'previewmesh-runner', 'user': {'token': 'runner-secret'}}]
            assert 'admin-secret' not in config.read_text()
            assert config.stat().st_mode & 0o777 == 0o600
        else:
            assert result.returncode != 0, scenario
            assert config.read_text() == 'previous config', scenario
    # Refuse credential paths that Git could publish, including already tracked files.
    for tracked in [False, True]:
        if tracked:
            subprocess.run(['git', '-C', str(work), 'add', '-f', 'config/runner.yaml'], check=True)
        else:
            (work / '.gitignore').write_text('')
        result = subprocess.run(['python3', str(work / 'scripts/configure-runner.py')],
                                env=env, capture_output=True, text=True)
        assert result.returncode != 0
        assert config.read_text() == 'previous config'
        (work / '.gitignore').write_text('/config/runner.yaml\n/config/.runner-*\n')
print('Setup script checks passed.')

# Check version rejection and exit propagation without inspecting a real cluster.
with tempfile.TemporaryDirectory(prefix='previewmesh-environment-') as directory:
    mock = Path(directory)
    for name, body in {
        'go': 'echo "go version go${TEST_GO:-1.25.0} linux/amd64"',
        'helm': 'echo "v${TEST_HELM:-3.0.0}"',
        'kubectl': 'exit "${TEST_KUBECTL_EXIT:-0}"',
    }.items():
        tool = mock / name
        tool.write_text('#!/usr/bin/env bash\n' + body + '\n')
        tool.chmod(0o700)
    env = dict(os.environ, PATH=str(mock) + os.pathsep + os.environ['PATH'])
    for variables, expected in [({}, 0), ({'TEST_GO': '1.24.9'}, 1),
                                ({'TEST_HELM': '4.0.0'}, 1), ({'TEST_KUBECTL_EXIT': '7'}, 7)]:
        result = subprocess.run(['bash', str(root / 'scripts/check-environment.sh'), 'runner'],
                                env=dict(env, **variables), capture_output=True, text=True)
        assert result.returncode == expected, (variables, result.stdout, result.stderr)
print('Environment script checks passed.')
