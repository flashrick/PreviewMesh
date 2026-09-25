// Execute the shipped relay with repeated event payloads and an in-memory API.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const template = readFileSync(new URL('../templates/source-notify.yml', import.meta.url), 'utf8');
const script = template.split('          script: |\n')[1];
assert.ok(script, 'notification script must exist');
const relay = new (Object.getPrototypeOf(async function () {}).constructor)(
  'github', 'context', 'core', 'process', script,
);
const requests = [];
const github = { rest: { actions: { createWorkflowDispatch: async request => {
  requests.push(structuredClone(request));
} } } };
const process = { env: {
  PREVIEWMESH_CONTROL_OWNER: 'owner', PREVIEWMESH_CONTROL_REPOSITORY: 'control',
} };
const core = { setFailed: message => assert.fail(message) };
const expected = {
  owner: 'owner', repo: 'control', workflow_id: 'preview.yml', ref: 'main',
  inputs: { repository_id: '12', source_repository: 'owner/demo', pr_number: '3' },
};

for (const action of ['opened', 'synchronize', 'reopened']) {
  const payload = {
    action, repository: { id: 12, full_name: 'owner/demo' },
    pull_request: { number: 3, head: { sha: 'a'.repeat(40), repo: { id: 12 } } },
  };
  for (let delivery = 0; delivery < 3; delivery++) {
    const before = requests.length;
    await relay(github, { payload: structuredClone(payload) }, core, process);
    assert.equal(requests.length, before + 1);
    assert.deepEqual(requests.at(-1), expected);
  }
}
// Event revisions are deliberately omitted: the control plane reads current state.
await relay(github, { payload: {
  repository: { id: 12, full_name: 'owner/demo' },
  pull_request: { number: 3, head: { sha: 'b'.repeat(40) } },
} }, core, process);
assert.deepEqual(requests.at(-1), expected);
console.log('PASS: repeated opened/synchronize/reopened payloads relay the same PR identity (API double only).');
