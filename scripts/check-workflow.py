"""Run orchestration regression checks with tool doubles, without GitHub or Kubernetes."""
import csv
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
sha = 'a' * 40

# Exercise the workflow's actual shell expression: each control owns its source image.
workflow = (root / '.github/workflows/preview.yml').read_text()
image_expression = re.search(r'--image "([^"\n]+)"', workflow).group(1)
for control_id in ['34', '56']:
    env = dict(os.environ, GITHUB_REPOSITORY_OWNER='Example-Owner',
               GITHUB_REPOSITORY_ID=control_id, REPOSITORY_ID='12')
    image = subprocess.check_output(['bash', '-c', 'printf "%s" "' + image_expression + '"'], env=env, text=True)
    assert image == f'ghcr.io/example-owner/previewmesh-c{control_id}-r12', image

# Keep the source-side relay wired to PR head updates. Without synchronize,
# an already-open PR would never request a deployment for its next commit.
notification = (root / 'templates/source-notify.yml').read_text()
assert 'pull_request_target:' in notification
assert 'types: [opened, synchronize, reopened, closed]' in notification

control = '''#!/usr/bin/env python3
import json, os, pathlib, sys
command = sys.argv[1]
scenario = os.environ['SCENARIO']
if command == 'resolve':
    print('{}')
elif command == 'inspect':
    counter = pathlib.Path('counter')
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    if scenario == 'inspect_failure': sys.exit(1)
    state = 'closed' if scenario in ['pre_closed', 'merged_closed', 'report_failure'] or (scenario == 'post_closed' and count) else 'open'
    sha = 'b' * 40 if scenario == 'pre_superseded' or (scenario == 'post_superseded' and count) else 'a' * 40
    print(json.dumps({'state':state, 'merged':scenario == 'merged_closed', 'sha':sha, 'port':8080}))
elif command == 'status':
    pathlib.Path('reported.json').write_text(json.dumps(sys.argv[2:]))
    if scenario == 'report_failure': sys.exit(1)
'''
preview = '''#!/usr/bin/env python3
import json, os, pathlib, sys
command = sys.argv[1]
with open('mutations', 'a') as f: f.write(command+'\\n')
failed = os.environ['SCENARIO'] == 'deploy_failure' and command == 'deploy'
wrong_revision = os.environ['SCENARIO'] == 'wrong_revision' and command == 'deploy'
result = {'result':'failure' if failed else 'success', 'requested_sha':'a'*40,
          'served_sha':'b'*40 if wrong_revision else 'a'*40,
          'http_verification':'success',
          'revision_verification':'failure' if wrong_revision else 'success',
          'cleanup':'confirmed_absent' if command == 'cleanup' else 'not_attempted'}
pathlib.Path(sys.argv[sys.argv.index('--result-file')+1]).write_text(json.dumps(result))
sys.exit(1 if failed else 0)
'''
with tempfile.TemporaryDirectory(prefix='previewmesh-workflow-test-') as temp:
    temp = Path(temp)
    for scenario, expected, mutations, exitcode in [
        ('pre_closed', 'removed', ['cleanup'], 0),
        ('merged_closed', 'removed', ['cleanup'], 0),
        ('pre_superseded', 'superseded', [], 0),
        ('post_closed', 'removed', ['deploy', 'cleanup'], 0),
        ('post_superseded', 'superseded', ['deploy'], 0),
        ('ready', 'ready', ['deploy'], 0),
        ('wrong_revision', 'failure', ['deploy'], 1),
        ('deploy_failure', 'failure', ['deploy'], 1),
        ('inspect_failure', 'failure', [], 1),
        ('report_failure', 'removed', ['cleanup'], 0),
    ]:
        case = temp / scenario
        case.mkdir()
        tools = case / 'tools'
        tools.mkdir()
        for name, content in [('control', control), ('previewmesh', preview)]:
            path = tools / name
            path.write_text(content)
            path.chmod(0o700)
        go = tools / 'go'
        go.write_text('#!/bin/sh\ncp "$MOCK_TOOLS/$(basename "$3")" "$3"\n')
        go.chmod(0o700)
        env = dict(os.environ, PATH=str(tools)+os.pathsep+os.environ['PATH'], MOCK_TOOLS=str(tools),
                   SCENARIO=scenario, REPOSITORY_ID='12', SOURCE_REPOSITORY='owner/demo', PR_NUMBER='3',
                   BUILT_SHA=sha, BUILT_IMAGE='unused', GITHUB_SERVER_URL='https://github.com',
                   GITHUB_REPOSITORY='owner/control', GITHUB_RUN_ID='1')
        completed = subprocess.run(['bash', str(root/'scripts/local-attempt.sh')], cwd=case, env=env, capture_output=True, text=True)
        assert completed.returncode == exitcode, (scenario, completed.stderr)
        outcome = json.loads((case/'evidence/outcome.json').read_text())
        assert outcome['outcome'] == expected, (scenario, outcome)
        actual = (case/'mutations').read_text().splitlines() if (case/'mutations').exists() else []
        assert actual == mutations, (scenario, actual)
        reported = json.loads((case/'reported.json').read_text())
        assert '--comment' in reported, (scenario, reported)
        assert reported[reported.index('--build-state')+1] == 'success'
        if mutations:
            assert reported[reported.index('--result-file')+1] == ('evidence/cleanup.json' if 'cleanup' in mutations else 'evidence/deploy.json')
        else:
            assert '--result-file' not in reported
        assert reported[reported.index('--run-url')+1] == 'https://github.com/owner/control/actions/runs/1'
        target = reported[reported.index('--url')+1]
        assert target == ('http://pm-r12-pr3.preview.test:18080' if scenario == 'ready' else 'https://github.com/owner/control/actions/runs/1'), (scenario, target)
        assert reported[reported.index('--state')+1] == ('success' if expected in ['ready', 'removed'] else 'failure')
        if scenario == 'report_failure':
            assert outcome['reporting_result'] == 'failure'

    # Timing metadata is available, but it must not hide missing lifecycle evidence.
    report = temp/'report'
    (report/'collected/local').mkdir(parents=True)
    env = dict(os.environ, GITHUB_REPOSITORY='owner/control', GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='1',
               GH_TOKEN='test', BUILD_RESULT='success', LOCAL_RESULT='failure', GITHUB_STEP_SUMMARY=str(report/'summary.md'))
    fake_api = '''import io,json,runpy,sys,urllib.request
run={'run_started_at':'2026-01-01T00:00:00Z'}
jobs={'jobs':[{'name':'build','created_at':'2026-01-01T00:00:00Z','started_at':'2026-01-01T00:00:01Z','status':'completed'}]}
urllib.request.urlopen=lambda req,timeout: io.StringIO(json.dumps(jobs if '/jobs?' in req.full_url else run))
runpy.run_path(sys.argv[1],run_name='__main__')
'''
    subprocess.run(['python3','-c',fake_api,str(root/'scripts/report.py')], cwd=report, env=env, check=True)
    with (report/'evidence/combined.csv').open() as f:
        rows = list(csv.DictReader(f))
    assert any(r['stage']=='queue:build' for r in rows)
    assert any(r['stage']=='attempt' and r['result']=='failure' and r['error'] for r in rows)
    timing = lambda stage, second: {'stage':stage, 'started_at_utc':f'2026-01-01T00:00:0{second}Z',
                                   'ended_at_utc':f'2026-01-01T00:00:0{second+1}Z',
                                   'duration_seconds':1, 'result':'success'}
    (report/'collected/local/deploy.json').write_text(json.dumps({'result':'success', 'stage_timings':[
        timing('deploy', 1), timing('readiness', 2), timing('http_verify', 3), timing('resource_observation', 4)]}))
    (report/'collected/local/cleanup.json').write_text(json.dumps({'result':'failure','failed_stage':'cleanup','error':'API unavailable','cleanup':'not_attempted',
                                                                  'stage_timings':[timing('cleanup', 5), timing('resource_verify', 6)]}))
    subprocess.run(['python3','-c',fake_api,str(root/'scripts/report.py')], cwd=report, env=env, check=True)
    summary = json.loads((report/'evidence/summary.json').read_text())
    assert summary['failed_stage']=='cleanup' and summary['error']=='API unavailable'
    assert summary['remaining_namespace_resources'] is None
    stages = {timing['stage'] for timing in summary['stage_timings']}
    assert {'deploy', 'readiness', 'http_verify', 'cleanup', 'resource_observation', 'resource_verify'} <= stages
print('PASS: synchronize notification and 10 current-state/reporting regressions (tool doubles only).')
