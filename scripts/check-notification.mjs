// Execute the shipped relay with repeated event payloads and an in-memory API.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const template = readFileSync(new URL('../templates/source-notify.yml', import.meta.url), 'utf8');

function scriptsFromTemplate(text) {
  const marker = '          script: |\n';
  const scripts = [];
  let offset = 0;
  while (true) {
    const start = text.indexOf(marker, offset);
    if (start < 0) break;
    const lines = text.slice(start + marker.length).split('\n');
    const body = [];
    for (const line of lines) {
      if (line === '') {
        body.push('');
        continue;
      }
      if (!line.startsWith('            ')) break;
      body.push(line.slice(12));
    }
    scripts.push(body.join('\n'));
    offset = start + marker.length;
  }
  return scripts;
}

const [pendingScript, dispatchScript] = scriptsFromTemplate(template);
assert.ok(pendingScript && dispatchScript, 'notification scripts must exist');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runScript = script => new AsyncFunction('github', 'context', 'core', 'process', script);
const dispatches = [];
const statuses = [];
const comments = [];
const updates = [];
const github = { rest: {
  repos: { createCommitStatus: async request => {
    statuses.push(structuredClone(request));
  } },
  issues: {
    listComments: async () => ({data: comments.map(comment => structuredClone(comment))}),
    createComment: async request => {
      const comment = {id: comments.length + 1, user: {login: 'github-actions[bot]', type: 'Bot'}, ...structuredClone(request)};
      comments.push(comment);
      return {data: comment};
    },
    updateComment: async request => {
      updates.push(structuredClone(request));
      const comment = comments.find(item => item.id === request.comment_id);
      comment.body = request.body;
      return {data: structuredClone(comment)};
    },
  },
  actions: { createWorkflowDispatch: async request => {
    dispatches.push(structuredClone(request));
  } },
} };
const process = { env: {
  PREVIEWMESH_CONTROL_OWNER: 'owner', PREVIEWMESH_CONTROL_REPOSITORY: 'control',
  GITHUB_SERVER_URL: 'https://github.com', GITHUB_REPOSITORY: 'owner/demo', GITHUB_RUN_ID: '1',
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
    const context = { payload: structuredClone(payload), repo: {owner: 'owner', repo: 'demo'} };
    await runScript(pendingScript)(github, context, core, process);
    await runScript(dispatchScript)(github, context, core, process);
    assert.equal(dispatches.length, (['opened', 'synchronize', 'reopened'].indexOf(action) * 3) + delivery + 1);
    assert.deepEqual(dispatches.at(-1), expected);
  }
}
// Event revisions are deliberately omitted: the control plane reads current state.
const revisionless = { payload: {
  repository: { id: 12, full_name: 'owner/demo' },
  pull_request: { number: 3, head: { sha: 'b'.repeat(40) } },
} };
await runScript(pendingScript)(github, { ...revisionless, repo: {owner: 'owner', repo: 'demo'} }, core, process);
await runScript(dispatchScript)(github, { ...revisionless, repo: {owner: 'owner', repo: 'demo'} }, core, process);
assert.deepEqual(dispatches.at(-1), expected);

// Closing is visible before dispatch and repeated deliveries update one comment.
const closedContext = {
  payload: {
    action: 'closed',
    repository: { id: 12, full_name: 'owner/demo' },
    pull_request: { number: 3, head: { sha: 'a'.repeat(40) }, merged: false },
  },
  repo: {owner: 'owner', repo: 'demo'},
};
await runScript(pendingScript)(github, closedContext, core, process);
await runScript(dispatchScript)(github, closedContext, core, process);
await runScript(pendingScript)(github, closedContext, core, process);
await runScript(dispatchScript)(github, closedContext, core, process);
assert.equal(comments.length, 1, 'repeated closed deliveries must not duplicate the progress comment');
assert.equal(updates.length, 1, 'repeated closed deliveries should update the progress comment');
assert.match(comments[0].body, /Preview cleanup is in progress/);
assert.match(comments[0].body, /preview URL may remain available briefly/);
assert.match(comments[0].body, /A final result will appear here after cleanup is confirmed/);
assert.match(comments[0].body, /actions\/runs\/1/);
assert.equal(statuses.at(-1).description, 'Cleanup request sent; waiting for PreviewMesh to start');
console.log('PASS: close events publish idempotent cleanup progress before dispatch.');
