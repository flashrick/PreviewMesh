"""Combine only this attempt's artifacts; missing measurements remain empty."""
import csv
import datetime as dt
import json
import os
from pathlib import Path
import urllib.request

columns = 'run_id attempt repository_id source_repository pr_number requested_sha served_sha stage started_at_utc ended_at_utc duration_seconds result error'.split()
rows = []
files = list(Path('collected').rglob('*')) if Path('collected').exists() else []
for path in files:
    if path.suffix == '.csv':
        with path.open(newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != columns:
                raise ValueError(f'Unexpected evidence schema: {path.name}')
            rows.extend(reader)
has_lifecycle_evidence = bool(rows)
results = {}
for path in files:
    if path.name in ['deploy.json', 'cleanup.json', 'build.json', 'outcome.json']:
        results[path.stem] = json.loads(path.read_text())

def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))

def measurement(stage, start, end, result):
    row = dict.fromkeys(columns, '')
    row.update(run_id=os.environ.get('GITHUB_RUN_ID', ''), attempt=os.environ.get('GITHUB_RUN_ATTEMPT', ''),
               repository_id=os.environ.get('REPOSITORY_ID', ''), source_repository=os.environ.get('SOURCE_REPOSITORY', ''),
               pr_number=os.environ.get('PR_NUMBER', ''), requested_sha=os.environ.get('REQUESTED_SHA', ''),
               stage=stage, started_at_utc=start, ended_at_utc=end, result=result)
    if start and end:
        row['duration_seconds'] = str((timestamp(end) - timestamp(start)).total_seconds())
    rows.append(row)

metadata_error = ''
try:
    base = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
    def get(url):
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer '+os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    run = get(base)
    jobs = get(base+'/jobs?per_page=100')['jobs']
    for job in jobs:
        if job.get('created_at') and job.get('started_at') and job.get('status') != 'queued':
            measurement('queue:'+job['name'], job['created_at'], job['started_at'], 'observed')
    if results.get('outcome', {}).get('outcome') == 'ready':
        verified = [r for r in rows if r['stage']=='http_verify' and r['result']=='success']
        if verified and run.get('run_started_at'):
            measurement('workflow_to_ready', run['run_started_at'], verified[-1]['ended_at_utc'], 'success')
except Exception:
    metadata_error = 'GitHub timing metadata unavailable; queue and workflow timing may be absent.'

# Every attempt needs an outcome, even when only queue measurements survived.
outcome = results.get('outcome', {}).get('outcome')
if not outcome:
    outcome = 'failure' if 'failure' in [os.environ.get('BUILD_RESULT'), os.environ.get('LOCAL_RESULT')] else 'incomplete'
measurement('attempt', '', '', outcome)
if not has_lifecycle_evidence:
    rows[-1]['error'] = 'No lifecycle evidence collected; inspect job results and control logs.'
failed = next((results[name] for name in ['cleanup', 'deploy', 'build']
               if results.get(name, {}).get('result') == 'failure'), {})
Path('evidence').mkdir(exist_ok=True)
with open('evidence/combined.csv', 'w', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
summary = {
    'build_job': os.environ.get('BUILD_RESULT', ''), 'local_job': os.environ.get('LOCAL_RESULT', ''),
    'requested_sha': os.environ.get('REQUESTED_SHA', ''),
    'served_sha': results.get('deploy', {}).get('served_sha', ''),
    'revision_verification': results.get('deploy', {}).get('revision_verification', 'not_attempted'),
    'url': results.get('deploy', {}).get('url', ''),
    'failed_stage': failed.get('failed_stage', ''),
    'error': failed.get('error', ''),
    'pending_reporting': os.environ.get('PENDING_REPORTING', ''),
    'build_failure_reporting': os.environ.get('BUILD_FAILURE_REPORTING', ''),
    'rollback': results.get('deploy', {}).get('rollback', 'not_attempted'),
    'cleanup': results.get('cleanup', {}).get('cleanup', 'not_attempted'),
    'remaining_namespace_resources': 0 if results.get('cleanup', {}).get('cleanup')=='confirmed_absent' else None,
    **results.get('outcome', {}), 'timing_note': metadata_error,
}
Path('evidence/summary.json').write_text(json.dumps(summary, indent=2)+'\n')
with open(os.environ.get('GITHUB_STEP_SUMMARY', 'evidence/summary.md'), 'a') as stream:
    stream.write('## PreviewMesh attempt\n\n```json\n'+json.dumps(summary, indent=2)+'\n```\n')
    stream.write('\nLocal URLs require hosts entries. Cleanup counts exclude images, artifacts, hosts entries and shared cluster services.\n')
