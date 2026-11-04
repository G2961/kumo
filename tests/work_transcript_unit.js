/* Offline unit tests against the actual LIVE asset functions. Run:
 * node tests/work_transcript_unit.js
 * No DOM, bridge, server, model calls, build, or installation.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../app/src/main/assets/codex.html'), 'utf8');
const start = '/* ---------- work transcript model (no rendering) ---------- */';
const end = '/* ---------- end work transcript model ---------- */';
assert.equal(html.split(start).length, 2);
assert.equal(html.split(end).length, 2);
const helpers = html.slice(html.indexOf(start), html.indexOf(end) + end.length);
function actualFunction(name) {
  const begin = html.indexOf('function ' + name + '(');
  assert.notEqual(begin, -1, name);
  const lineEnd = html.indexOf('\n', begin);
  const finish = html.slice(begin, lineEnd).endsWith('}') ? lineEnd : html.indexOf('\n}', begin) + 2;
  return html.slice(begin, finish);
}
// Parse the complete inline script as well; do not execute the app or its handlers.
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const context = vm.createContext({S: {}});
const redactSource = html.match(/^const redact=.*$/m);
assert.ok(redactSource, 'Actual redaction helper');
vm.runInContext(redactSource[0], context);
vm.runInContext(['splitThoughts', 'assistantSegments', 'toolFeedText', 'toolFeedFilename',
  'toolFeedState', 'changeKey', 'toolChanges', 'toolFeedChanges', 'workCommandName',
  'workCommandIntent', 'workToolVoice', 'workActiveLabel', 'workRenderUnits', 'workQuestionFacts'].map(actualFunction).join('\n') + '\n' + helpers, context);
const {buildWorkTranscript: build, summarizeWorkBlock: summarize} = context;
const plain = value => JSON.parse(JSON.stringify(value));
const types = result => Array.from(result.items, item => item.type);
const work = result => Array.from(result.blocks.values());
const text = (id, value) => ({id, type: 'text', text: value});
const reason = (id, value = 'PRIVATE_REASONING', ended = true) => ({id, type: 'reasoning', text: value, time: ended ? {start: 1, end: 2} : {start: 1}});
const tool = (id, name = 'read', input = {filePath: 'server.js'}, status = 'completed', extra = {}) => ({id, callID: 'call_' + id, type: 'tool', tool: name, state: {status, input, ...extra}});
function message(id, parts = [], options = {}) {
  const info = {id, sessionID: 'ses_test', role: 'assistant', ...options};
  return {info, parts: new Map(parts.map(part => [part.id, part]))};
}
const user = id => message(id, [text('prompt', 'Please inspect')], {role: 'user'});
const batch = (...parts) => message('m1', parts, {parentID: 'u1'});
function reset() {
  Object.assign(context.S, {sid: 'ses_test', dir: '/project', running: false, lastErr: null,
    status: {type: 'idle'}, msgs: new Map(), perms: new Map(), questions: new Map(),
    historyFiles: new Map(), changeHints: new Map(), remoteRuns: new Map(), renderedRemoteRuns: new Set()});
}
let passed = 0;
function test(name, run) {
  reset();
  try {run(); passed++; console.log('PASS ' + name)}
  catch (error) {console.error('FAIL ' + name); throw error}
}

test('contiguous reasoning/tools across messages with one parent', () => {
  const m1 = message('m1', [reason('r1'), tool('read')], {parentID: 'u1'});
  const m2 = message('m2', [reason('r2'), tool('cmd', 'bash', {command: 'npm test'})], {parentID: 'u1'});
  const result = build([user('u1'), m1, m2]);
  assert.deepEqual(types(result), ['user', 'work']);
  const block = work(result)[0];
  assert.equal(block.id, 'work_m1_r1');
  assert.equal(result.blocks.get(block.id), block);
  assert.equal(result.items[1].block, block);
  assert.equal(block.parentID, 'u1');
  assert.deepEqual(Array.from(block.events, e => e.key), ['m1_r1', 'm1_read', 'm2_r2', 'm2_cmd']);
  assert.equal(block.events[0].message, m1);
  assert.equal(block.events[1].part, m1.parts.get('read'));
  assert.equal(block.label, 'Read 1 file · Ran 1 command');
  assert.equal(block.steps, 4);
  assert.equal(block.reasoning, 2);
});

test('undefined parent fixtures group only until user or known/new parent', () => {
  const result = build([batch(reason('a')), message('m2', [reason('b')]), message('m3', [tool('c')]),
    user('u2'), message('m4', [tool('d')]), message('m5', [tool('e')], {parentID: 'u2'}),
    message('m6', [tool('f')], {parentID: 'u3'})]);
  assert.deepEqual(types(result), ['work', 'work', 'user', 'work', 'work', 'work']);
  assert.equal(work(result)[1].events.length, 2);
  assert.equal(work(result)[1].parentID, null);
});

test('short, long, synthetic commentary and final text are all boundaries', () => {
  for (const value of ['OK', 'Checking files now.', 'Final answer. '.repeat(1000)]) {
    const visible = {...text('t1', value), synthetic: true};
    const m = batch(reason('r1'), visible, tool('t2'), text('t3', 'Done.'));
    const result = build([m]);
    assert.deepEqual(types(result), ['work', 'text', 'work', 'text']);
    assert.equal(result.items[1].message, m);
    assert.equal(result.items[1].segment.text, value.trim());
    assert.equal(work(result)[1].id, 'work_m1_t2');
  }
});

test('native/tagged reasoning order comes from assistantSegments', () => {
  const m = batch(text('mixed', 'Before<think>first</think>Between<thought>second</thought>After'), reason('native'), tool('tool'));
  const result = build([m]);
  assert.deepEqual(types(result), ['text', 'work', 'text', 'work', 'text', 'work']);
  const segments = context.assistantSegments(m);
  const events = work(result).flatMap(b => Array.from(b.events)).filter(e => e.type === 'reasoning');
  assert.deepEqual(events.map(e => plain(e.segment)), Array.from(segments).filter(s => s.type === 'reasoning').map(plain));
  assert.equal(events[0].key, 'm1_mixed');
  assert.match(events[1].key, /^m1_mixed_at_\d+$/);
  assert.deepEqual(Array.from(result.items).filter(i => i.type === 'text').map(i => i.segment.text), ['Before', 'Between', 'After']);
});

test('literal tags in code and unfinished non-leading tags stay visible', () => {
  const values = ['`<think>example</think>`', '```xml\n<think>example</think>\n```', 'Visible <think>unfinished'];
  for (const value of values) {
    const result = build([batch(text('t', value))]);
    assert.deepEqual(types(result), ['text']);
    assert.equal(result.items[0].segment.text, value);
  }
});

test('files split even adjacent native tool/reasoning segments', () => {
  const file = {id: 'f', type: 'file', filename: 'report.txt', url: 'data:text/plain,report'};
  for (const event of [reason('r1'), tool('t1')]) {
    const next = {...event, id: 'next'};
    const m = batch(event, file, next);
    const result = build([m]);
    assert.deepEqual(types(result), ['work', 'file', 'work']);
    assert.equal(result.items[1].part, file);
    assert.equal(result.items[1].message, m);
    assert.equal(work(result)[1].events[0].key, 'm1_next');
  }
  assert.deepEqual(types(build([batch(file, file)])), ['file']);
});

test('only latest incomplete trailing reason in latest message runs', () => {
  context.S.running = true;
  const older = message('m0', [reason('old', 'old', false)], {parentID: 'u1'});
  const newest = batch(reason('early', 'early', false), text('c', 'Commentary'), reason('live', 'live', false));
  let result = build([older, newest]);
  assert.deepEqual(work(result).flatMap(b => Array.from(b.events, e => e.running)), [false, false, true]);
  assert.equal(work(result)[1].status, 'running');
  assert.equal(work(result)[1].label, 'Reasoning…');
  for (const following of [text('t', 'answer'), tool('t'), {id: 'f', type: 'file'}]) {
    result = build([batch(reason('r', 'reason', false), following)]);
    assert.equal(work(result)[0].events[0].running, false);
  }
  assert.equal(work(build([newest, user('u2')]))[1].events[0].running, false);
  for (const options of [{time: {completed: 5}}, {error: {name: 'MessageAbortedError'}}]) {
    assert.equal(work(build([message('m1', [reason('r', 'reason', false)], options)]))[0].events[0].running, false);
  }
  context.S.lastErr = 'disconnected';
  assert.equal(work(build([batch(reason('r', 'reason', false))]))[0].events[0].running, false);
});

test('empty thoughts are omitted, not turned into invented work', () => {
  for (const running of [false, true]) {
    context.S.running = running;
    assert.equal(build([batch(reason('empty', ' \n ', false), text('emptytag', '<think>  '))]).blocks.size, 0);
  }
  const block = work(build([batch(reason('r'))]))[0];
  assert.equal(block.label, 'Reasoning · 1s');
  assert.equal(block.completed, 0);
  assert.equal(block.status, 'completed'); // Timeline ended, not a claim of success.
  assert.doesNotMatch(block.label + block.detail, /PRIVATE_REASONING|success|passed/i);
  context.S.running = false;
  const stopped = work(build([batch(reason('r', 'partial', false))]))[0];
  assert.equal(stopped.status, 'stopped');
});

test('stable first-event key through streaming, completion, and reload', () => {
  const m = batch(text('r', '<think>Partial'));
  context.S.running = true;
  const first = work(build([m]))[0];
  m.parts.get('r').text += ' reasoning</think>';
  m.parts.set('t', tool('t', 'bash', {command: 'echo hello'}, 'running'));
  let block = work(build([m]))[0];
  assert.equal(block.id, first.id);
  assert.equal(block.status, 'running');
  m.parts.get('t').state.status = 'completed';
  m.info.time = {completed: 50};
  context.S.running = false;
  const saved = plain({info: m.info, parts: Array.from(m.parts.values())});
  block = work(build([{info: saved.info, parts: new Map(saved.parts.map(p => [p.id, p]))}]))[0];
  assert.equal(block.id, first.id);
  assert.equal(block.status, 'completed');
});

test('message errors/abort occur after prior events, not before or skipped', () => {
  for (const name of ['APIError', 'MessageAbortedError']) {
    const m = message('m1', [reason('r', 'partial', false), tool('t', 'bash', {}, 'running')], {error: {name}});
    const result = build([m, message('m2', [reason('after')])]);
    assert.deepEqual(types(result), ['work', 'error', 'work']);
    assert.equal(work(result)[0].events.length, 2);
    assert.equal(work(result)[0].status, name === 'APIError' ? 'error' : 'stopped');
    assert.equal(result.items[1].message, m);
  }
  const m = message('m1', [tool('t'), text('answer', 'Partial answer')], {error: {name: 'APIError'}});
  assert.deepEqual(types(build([m])), ['work', 'text', 'error']);
});

test('failed tools stay in the contiguous block until a real message boundary', () => {
  const result = build([batch(reason('r'), tool('bad', 'bash', {}, 'completed', {metadata: {exit: 7}}), tool('after'))]);
  assert.deepEqual(types(result), ['work']);
  assert.equal(work(result)[0].events.length, 3);
  assert.equal(work(result)[0].status, 'error');
  assert.equal(work(result)[0].commands, 0);
  assert.equal(work(result)[0].errors, 1);
  assert.equal(work(result)[0].label, 'Read 1 file');
  assert.match(work(result)[0].detail, /^1 failed/);
  const simultaneous = message('m1', [reason('r'), tool('bad', 'bash', {}, 'error', {error: 'failed'}), reason('after')], {error: {name: 'APIError'}});
  assert.deepEqual(types(build([simultaneous])), ['work', 'error']);
  assert.equal(work(build([simultaneous]))[0].events.length, 3);
});

test('honest_read_counts retains all six events including failed read', () => {
  const result = build([batch(
    reason('r'),
    tool('a', 'read', {path: 'a.js'}),
    tool('duplicate', 'read', {path: '/project/a.js'}),
    tool('bad', 'read', {path: 'missing.js'}, 'error', {error: 'not found'}),
    tool('b', 'read', {path: 'b.js'}),
    tool('cmd', 'bash', {command: 'echo checked'})
  )]);
  assert.deepEqual(types(result), ['work']);
  const block = work(result)[0];
  assert.equal(block.events.length, 6);
  assert.equal(block.steps, 6);
  assert.equal(block.reads, 2);
  assert.equal(block.commands, 1);
  assert.equal(block.errors, 1);
  assert.equal(block.completed, 4);
  assert.equal(block.status, 'error');
  assert.equal(block.label, 'Read 2 files · Ran 1 command');
  assert.match(block.detail, /^1 failed/);
});

test('read dedupe uses environment and minimally normalized project path', () => {
  const block = work(build([batch(
    tool('r1', 'read', {filePath: '/project/src/server.js'}),
    tool('r2', 'read', {path: './src/server.js'}),
    tool('r3', 'read', {path: 'src\\server.js'}),
    tool('r4', 'read', {path: 'client.js'}),
    tool('c', 'bash', {command: 'npm test'}, 'completed', {metadata: {exitCode: 0}})
  )]))[0];
  assert.equal(block.reads, 2);
  assert.equal(block.label, 'Read 2 files · Ran 1 command');
  assert.doesNotMatch(block.label + block.detail, /tests? passed|success/i);
  const remote = id => tool(id, 'mobile_ssh_ssh_read_file', {connection_id: id, path: '/project/src/server.js'});
  assert.equal(work(build([batch(tool('local'), remote('c1'), remote('c2'), remote('c1-again'))]))[0].reads, 4);
  const same = tool('same', 'mobile_ssh_ssh_read_file', {connection_id: 'c1', path: '/project/src/server.js'});
  assert.equal(work(build([batch(remote('c1'), same)]))[0].reads, 1);
  context.S.dir = '/';
  assert.equal(work(build([batch(tool('a', 'read', {path: 'a'}), tool('b', 'read', {path: '/a'}))]))[0].reads, 1);
  context.S.dir = '/other';
  const m = message('m1', [tool('a', 'read', {path: './a'}), tool('b', 'read', {path: '/project/a'})], {path: {cwd: '/project'}});
  assert.equal(work(build([m]))[0].reads, 1);
});

test('failed or unfinished reads are never successful counts', () => {
  for (const state of [
    {status: 'error', error: 'permission denied'}, {status: 'completed', metadata: {exitCode: 1}},
    {status: 'completed', metadata: {error: 'failed'}}, {status: 'pending'}, {status: 'running'}
  ]) {
    const block = work(build([batch(tool('r', 'read', {path: 'server.js'}, state.status, state))]))[0];
    assert.equal(block.reads, 0);
    assert.equal(block.completed, 0);
  }
  const noPath = work(build([batch(tool('r', 'read', {}))]))[0];
  assert.equal(noPath.reads, 0);
  assert.equal(noPath.label, 'Read a file'); // read with no path is still a tool step, not a thought
});

test('edits require completed per-call metadata/diff evidence, not inputs', () => {
  const patch = '--- a/a.js\n+++ b/a.js\n@@ -1 +1 @@\n-old\n+new\n';
  const block = work(build([batch(
    tool('e1', 'edit', {filePath: 'a.js'}, 'completed', {metadata: {diff: patch}}),
    tool('e2', 'edit', {filePath: '/project/a.js'}, 'completed', {metadata: {diff: patch}}),
    tool('e3', 'apply_patch', {}, 'completed', {metadata: {files: [{filePath: 'b.js', additions: 1}]}}),
    tool('unsupported', 'edit', {filePath: 'no-evidence.js'}),
    tool('shell', 'bash', {command: 'touch shell.js'}, 'completed', {output: 'changed shell.js'})
  )]))[0];
  assert.equal(block.edits, 2);
  assert.equal(block.commands, 1);
  assert.equal(block.label, 'Edited 2 files · Ran 1 command');
  for (const status of ['error', 'running', 'pending']) {
    const failed = work(build([batch(tool('e', 'edit', {filePath: 'a.js'}, status, {metadata: {diff: patch}}))]))[0];
    assert.equal(failed.edits, 0);
  }
  const failedShell = work(build([batch(tool('sh', 'bash', {command: 'touch a.js'}, 'completed', {metadata: {exitCode: 1, files: [{filePath: 'a.js', patch}]}}))]))[0];
  assert.equal(failedShell.edits, 0);
  assert.equal(failedShell.commands, 0);
  const newFile = work(build([batch(tool('write', 'write', {filePath: 'new.js', content: 'line'}, 'completed', {metadata: {exists: false}}))]))[0];
  assert.equal(newFile.edits, 1);
});

test('history is per call only, global or other-call diffs never assigned', () => {
  context.S.historyFiles.set('other:call_e', {file: 'unrelated.js', commit: 'abc'});
  context.S.changeHints.set('other:call_e', {file: 'unrelated.js', patch: '+unrelated'});
  const m = batch(tool('e', 'edit', {filePath: 'a.js'}));
  assert.equal(work(build([m]))[0].edits, 0);
  context.S.historyFiles.set('m1:call_e', {file: 'a.js', commit: 'abc'});
  assert.equal(work(build([m]))[0].edits, 1);
});

test('waiting, skipped questions, running tools and errors retain honest status', () => {
  const m = batch(tool('p', 'read', {filePath: 'server.js'}, 'running'));
  context.S.perms.set('permission', {tool: {messageID: 'm1', callID: 'call_p'}});
  let block = work(build([m]))[0];
  assert.equal(block.status, 'waiting');
  assert.equal(block.events[0].running, false);
  assert.equal(block.reads, 0);
  context.S.perms.clear();
  assert.equal(work(build([m]))[0].status, 'stopped');
  context.S.running = true;
  block = work(build([m]))[0];
  assert.equal(block.status, 'running');
  assert.equal(block.label, 'Reading server.js');
  assert.equal(block.events[0].running, true);
  assert.equal(work(build([batch(tool('r', 'read', {}, 'running'))]))[0].label, 'Reading a file');
  assert.equal(work(build([batch(tool('p', 'read', {}, 'pending'))]))[0].status, 'waiting');
  context.S.questions.set('q', {tool: {messageID: 'm1', callID: 'call_q'}});
  assert.equal(work(build([batch(tool('q', 'question', {}, 'running'))]))[0].status, 'waiting');
  block = work(build([batch(tool('q', 'question', {}, 'error', {error: 'The user dismissed this question'}))]))[0];
  assert.equal(block.status, 'stopped');
  assert.equal(block.errors, 0);
  assert.equal(block.skipped, 1);
});

test('read success label survives live work, pending requests and stale running parts', () => {
  const make = (state = 'running', options = {}) => message('m1', [
    tool('a', 'read', {path: 'a.js'}), tool('b', 'read', {path: 'b.js'}),
    tool('active', 'read', {path: 'server.js'}, state)
  ], options);
  context.S.running = true;
  let block = work(build([make()]))[0];
  assert.equal(block.label, 'Read 2 files');
  assert.equal(block.status, 'running');
  assert.match(block.detail, /Reading server\.js/);
  assert.equal(block.events[2].running, true);
  block = work(build([make('pending')]))[0];
  assert.equal(block.label, 'Read 2 files');
  assert.equal(block.status, 'waiting');
  assert.match(block.detail, /1 waiting/);
  for (const running of [false, true]) {
    context.S.running = running;
    block = work(build([make('running', {time: {completed: 10}})]))[0];
    assert.equal(block.label, 'Read 2 files');
    assert.equal(block.status, 'stopped');
    assert.equal(block.running, 0);
    assert.equal(block.events[2].running, false);
    assert.doesNotMatch(block.detail, /Reading|running/);
  }
  context.S.running = false;
  for (const state of ['running', 'pending', 'in_progress']) {
    block = work(build([make(state)]))[0];
    assert.equal(block.status, 'stopped');
    assert.equal(block.label, 'Read 2 files');
    assert.equal(block.reads, 2);
    assert.equal(block.waiting, 0);
    assert.equal(block.events[2].running, false);
  }
  context.S.perms.set('permission', {tool: {messageID: 'm1', callID: 'call_active'}});
  block = work(build([make()]))[0];
  assert.equal(block.status, 'waiting');
  assert.equal(block.label, 'Read 2 files');
  assert.equal(block.events[2].running, false);
  context.S.perms.clear();
  context.S.questions.set('q', {tool: {messageID: 'm1', callID: 'call_active'}});
  const question = make();
  question.parts.get('active').tool = 'question';
  block = work(build([question]))[0];
  assert.equal(block.status, 'waiting');
  assert.match(block.label, /^Read 2 files/);
  assert.equal(block.events[2].running, false);
  context.S.questions.clear();
  assert.equal(work(build([question]))[0].status, 'stopped');
});

test('unknown tools are other steps, never inferred reads or tests', () => {
  const block = work(build([batch(reason('r', 'All tests passed. Changed 99 files.'),
    tool('mcp', 'fixture_mcp_inspect', {path: 'a.js'}, 'completed', {output: 'All tests passed; 99 files changed'})
  )]))[0];
  assert.equal(block.other, 1);
  assert.equal(block.reads, 0);
  assert.equal(block.edits, 0);
  assert.equal(block.commands, 0);
  assert.equal(block.label, '1 other action');
  assert.doesNotMatch(block.label + block.detail, /99|tests|PRIVATE_REASONING/);
});

test('SSH result objects/JSON and exact native metadata determine effective state', () => {
  for (const serialize of [v => v, JSON.stringify]) {
    const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'test -f app.js'}, 'completed',
      {output: serialize({exitCode: 7, stdout: 'not successful'})});
    const block = work(build([batch(p)]))[0];
    assert.equal(block.status, 'error');
    assert.equal(block.commands, 0);
  }
  const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running', {metadata: {runID: 'native1'}});
  const m = batch(p);
  context.S.remoteRuns.set('native1', {id: 'native1', sessionID: 'ses_test', connectionID: 'c1', status: 'completed', exitCode: 0});
  let block = work(build([m]))[0];
  assert.equal(block.status, 'completed');
  assert.equal(block.commands, 1);
  assert.equal(block.events[0].running, false);
  context.S.remoteRuns.get('native1').exitCode = 4;
  block = work(build([m]))[0];
  assert.equal(block.status, 'error');
  assert.equal(block.commands, 0);
  context.S.running = true;
  context.S.remoteRuns.get('native1').sessionID = 'unrelated';
  assert.equal(work(build([m]))[0].status, 'running');
  context.S.remoteRuns.get('native1').sessionID = 'ses_test';
  context.S.remoteRuns.get('native1').connectionID = 'other';
  assert.equal(work(build([m]))[0].status, 'running');
  p.state.metadata = {};
  context.S.remoteRuns.get('native1').connectionID = 'c1';
  context.S.remoteRuns.get('native1').command = 'echo ready';
  context.S.remoteRuns.get('native1').kind = 'exec';
  context.S.remoteRuns.get('native1').status = 'running';
  delete context.S.remoteRuns.get('native1').exitCode;
  const fallback = build([m]);
  assert.equal(work(fallback)[0].status, 'running');
  assert.equal(work(fallback)[0].events.length, 1);
  assert.equal(fallback.runBlocks.get('native1'), work(fallback)[0].id);
});

test('SSH changed file evidence is not invented on failed calls', () => {
  const input = {connection_id: 'c1', path: '/srv/a.js', content: 'new'};
  for (const output of [{status: 'completed', additions: 1}, {status: 'completed', patch: '+new'}]) {
    const block = work(build([batch(tool('w', 'mobile_ssh_ssh_write_file', input, 'completed', {output}))]))[0];
    assert.equal(block.edits, 1);
  }
  for (const output of [{status: 'completed'}, {status: 'error', error: 'denied', patch: '+new'}, {exitCode: 1, additions: 1}]) {
    assert.equal(work(build([batch(tool('w', 'mobile_ssh_ssh_write_file', input, 'completed', {output}))]))[0].edits, 0);
  }
  const p = tool('w', 'mobile_ssh_ssh_write_file', input, 'running', {metadata: {runID: 'write1'}});
  context.S.remoteRuns.set('write1', {id: 'write1', connectionID: 'c1', sessionID: 'ses_test', status: 'completed', path: '/srv/a.js', patch: '+new'});
  assert.equal(work(build([batch(p)]))[0].edits, 1);
});

test('source objects, global maps, outputs and rendered-run claims are untouched', () => {
  const p = tool('t', 'mobile_ssh_ssh_exec', {connection_id: 'c1'}, 'completed', {output: {runID: 'native', exitCode: 0}});
  const m = batch(reason('r'), p, text('answer', 'Raw answer'));
  context.S.msgs = new Map([[m.info.id, m]]);
  context.S.remoteRuns.set('native', {id: 'native', sessionID: 'ses_test', connectionID: 'c1', status: 'completed', exitCode: 0, stdout: 'RAW'});
  context.S.renderedRemoteRuns.add('existing');
  function freeze(value) {
    if (!value || typeof value !== 'object' || Object.isFrozen(value)) return;
    Object.freeze(value);
    for (const child of value instanceof Map ? value.values() : Object.values(value)) freeze(child);
  }
  const snapshot = () => JSON.stringify(context.S, (_, value) => value instanceof Map || value instanceof Set ? [...value] : value);
  const before = snapshot();
  freeze(m);
  const result = build([m]);
  const summary = summarize(work(result)[0]);
  assert.equal(snapshot(), before);
  assert.equal(p.messageID, undefined);
  assert.equal(p.sessionID, undefined);
  assert.equal(result.items[0].block.events[1].part, p);
  assert.deepEqual(plain(summarize(work(result)[0])), plain(summary));
  assert.equal(result.blocks.get('work_m1_r').label, summary.label);
});

const nativeRun = (id, extra = {}) => ({id, sessionID: 'ses_test', connectionID: 'c1', kind: 'exec', command: 'echo ready', stdout: 'NATIVE_OUTPUT', stderr: '', status: 'running', ...extra});
const allEvents = result => work(result).flatMap(block => Array.from(block.events));

test('native orphan is visible before any tool part, with stable UI-only identity', () => {
  context.S.running = true;
  context.S.remoteRuns.set('run1', nativeRun('run1'));
  const result = build([]), block = work(result)[0], event = block.events[0];
  assert.deepEqual(types(result), ['work']);
  assert.equal(block.id, 'work_remote_run1');
  assert.equal(event.key, 'remote_run1');
  assert.equal(event.provisional, true);
  assert.equal(event.part.provisional, true);
  assert.equal(event.part.runID, 'run1');
  assert.equal(event.part.state.output.stdout, 'NATIVE_OUTPUT');
  assert.equal(event.part.state.input.connection_id, 'c1');
  assert.equal(event.running, true);
  assert.equal(block.status, 'running');
  assert.equal(block.commands, 0);
  assert.equal(result.runBlocks.get('run1'), block.id);
  context.S.remoteRuns.get('run1').stdout += '\nSTREAM_DELTA';
  const next = build([]);
  assert.equal(work(next)[0].id, block.id);
  assert.match(allEvents(next)[0].part.state.output.stdout, /STREAM_DELTA/);
});

test('orphan joins only current unfinished technical tail, never crosses public boundaries', () => {
  context.S.running = true;
  context.S.remoteRuns.set('run1', nativeRun('run1'));
  const m = batch(reason('r', 'partial', false));
  let result = build([m]);
  assert.deepEqual(types(result), ['work']);
  assert.equal(work(result)[0].events.length, 2);
  assert.equal(work(result)[0].events[0].running, false);
  assert.equal(result.runBlocks.get('run1'), 'work_m1_r');
  for (const boundary of [text('t', 'Public commentary'), {id: 'f', type: 'file'}]) {
    result = build([batch(reason('r'), boundary)]);
    assert.deepEqual(types(result), ['work', boundary.type, 'work']);
    assert.equal(work(result)[0].events.length, 1);
    assert.equal(result.runBlocks.get('run1'), 'work_remote_run1');
  }
  result = build([m, user('u2')]);
  assert.deepEqual(types(result), ['work', 'user', 'work']);
  assert.equal(work(result)[1].parentID, 'u2');
  for (const options of [{time: {completed: 10}}, {error: {name: 'APIError'}}]) {
    result = build([message('m1', [reason('r')], options)]);
    assert.equal(work(result).length, 2);
    assert.equal(work(result)[0].events.length, 1);
    assert.equal(result.runBlocks.get('run1'), 'work_remote_run1');
  }
  result = build([m, message('m2', [], {parentID: 'u2'})]);
  assert.equal(work(result).length, 2);
  assert.equal(work(result)[1].parentID, 'u2');
});

test('orphan handoff exact result, metadata, runID and callID never duplicates', () => {
  for (const binding of ['object', 'json', 'metadata', 'runID', 'callID']) {
    reset();context.S.running = true;
    context.S.remoteRuns.set('run1', nativeRun('run1'));
    const before = build([user('u1')]);
    const selectedRunID = allEvents(before)[0].part.runID;
    const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running');
    if (binding === 'object') p.state.output = {runID: 'run1'};
    if (binding === 'json') p.state.output = JSON.stringify({runID: 'run1'});
    if (binding === 'metadata') p.state.metadata = {runID: 'run1'};
    if (binding === 'runID') p.runID = 'run1';
    if (binding === 'callID') p.callID = 'run1';
    const result = build([user('u1'), batch(p)]);
    assert.equal(allEvents(result).length, 1);
    assert.equal(allEvents(result)[0].part, p);
    assert.equal(allEvents(result)[0].provisional, undefined);
    assert.equal(result.runBlocks.get(selectedRunID), 'work_m1_ssh');
    assert.equal(allEvents(result)[0].runID, 'run1');
    context.S.remoteRuns.get('run1').status = 'completed';
    context.S.remoteRuns.get('run1').exitCode = 0;
    const completed = build([user('u1'), batch(p)]);
    assert.equal(allEvents(completed).length, 1);
    assert.equal(work(completed)[0].commands, 1);
    assert.equal(completed.runBlocks.get(selectedRunID), 'work_m1_ssh');
    if (binding !== 'callID') {
      context.S.remoteRuns.clear(); // Explicit aliases survive native-buffer eviction/reload.
      assert.equal(build([batch(p)]).runBlocks.get(selectedRunID), 'work_m1_ssh');
    }
  }
});

test('exact reservations win over earlier identical commands and paths', () => {
  context.S.running = true;
  for (const kind of ['exec', 'write']) {
    context.S.remoteRuns.clear();
    const extra = kind === 'write' ? {kind, path: '/srv/app.js'} : {kind};
    context.S.remoteRuns.set('known', nativeRun('known', extra));
    context.S.remoteRuns.set('fallback', nativeRun('fallback', extra));
    const input = kind === 'write' ? {connection_id: 'c1', path: './app.js'} : {connection_id: 'c1', command: 'echo ready'};
    const name = kind === 'write' ? 'mobile_ssh_ssh_write_file' : 'mobile_ssh_ssh_exec';
    const early = tool('early', name, input, 'running');
    const exact = tool('exact', name, input, 'running', {metadata: {runID: 'known'}});
    let result = build([batch(early, exact)]);
    assert.equal(allEvents(result).length, 2);
    assert.deepEqual(allEvents(result).map(e => e.runID), ['fallback', 'known']);
    assert.equal(result.runBlocks.get('known'), 'work_m1_early');
    assert.equal(result.runBlocks.get('fallback'), 'work_m1_early');
    assert.equal(early.runID, undefined);
    context.S.remoteRuns.delete('fallback');
    result = build([batch(early, text('text', 'Boundary'), exact)]);
    assert.deepEqual(types(result), ['work', 'text', 'work']);
    assert.equal(allEvents(result)[0].runID, undefined);
    assert.equal(result.runBlocks.get('known'), 'work_m1_exact');
  }
});

test('fallback cannot steal known IDs, ambiguous runs, old calls or another connection', () => {
  context.S.running = true;
  const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running');
  context.S.remoteRuns.set('run1', nativeRun('run1'));
  context.S.remoteRuns.set('run2', nativeRun('run2'));
  let result = build([batch(p)]);
  assert.equal(allEvents(result).filter(e => e.provisional).length, 2);
  assert.equal(allEvents(result)[0].runID, undefined);
  context.S.remoteRuns.delete('run2');
  result = build([batch(p, {...p, id: 'same', callID: 'call_same'})]);
  assert.equal(allEvents(result).filter(e => e.provisional).length, 1);
  assert.equal(allEvents(result)[0].runID, undefined);
  p.state.metadata = {runID: 'known-but-not-delivered'};
  result = build([batch(p)]);
  assert.equal(result.runBlocks.get('known-but-not-delivered'), 'work_m1_ssh');
  assert.equal(allEvents(result).filter(e => e.provisional).length, 1);
  delete p.state.metadata;
  p.state.input.connection_id = 'other';
  assert.equal(allEvents(build([batch(p)])).filter(e => e.provisional).length, 1);
  delete p.state.input.connection_id;
  assert.equal(allEvents(build([batch(p)])).filter(e => e.provisional).length, 1);
  p.state.input.connection_id = 'c1';p.state.status = 'completed';
  result = build([batch(p)]);
  assert.equal(allEvents(result).filter(e => e.provisional).length, 1);
  p.state.status = 'running';
  result = build([batch(p, text('text', 'Final')), user('u2')]);
  assert.equal(allEvents(result).filter(e => e.provisional).length, 1);
});

test('public text after a matching part does not recreate an orphan or move output', () => {
  context.S.running = true;
  context.S.remoteRuns.set('run1', nativeRun('run1'));
  const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running');
  for (const list of [
    [batch(p, text('comment', 'Still working'))],
    [batch(p), message('m2', [text('comment', 'Still working')], {parentID: 'u1'})]
  ]) {
    const result = build(list);
    assert.deepEqual(types(result), ['work', 'text']);
    assert.equal(allEvents(result).length, 1);
    assert.equal(result.runBlocks.get('run1'), 'work_m1_ssh');
    assert.equal(allEvents(result)[0].part, p);
  }
  const changedParent = build([batch(p), message('m2', [text('comment', 'New turn')], {parentID: 'u2'})]);
  assert.deepEqual(types(changedParent), ['work', 'text', 'work']);
  assert.equal(allEvents(changedParent).filter(e => e.provisional).length, 1);
});

test('completed fallback represented once, unmatched completed/error runs never become ghosts', () => {
  context.S.running = true;
  context.S.remoteRuns.set('done', nativeRun('done', {status: 'completed', exitCode: 0}));
  context.S.remoteRuns.set('error', nativeRun('error', {status: 'error', command: 'different', stderr: 'failed'}));
  context.S.remoteRuns.set('other-session', nativeRun('other-session', {sessionID: 'other'}));
  assert.equal(build([]).blocks.size, 0);
  const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'completed');
  const result = build([batch(p)]);
  assert.equal(allEvents(result).length, 1);
  assert.equal(work(result)[0].commands, 1);
  assert.equal(result.runBlocks.get('done'), 'work_m1_ssh');
  assert.equal(result.runBlocks.has('error'), false);
  assert.equal(result.runBlocks.has('other-session'), false);
});

test('redacted native command fallback uses production redaction', () => {
  context.S.running = true;
  context.S.remoteRuns.set('run1', nativeRun('run1', {command: 'echo [redacted]'}));
  const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo sk-fixture'}, 'running');
  const result = build([batch(p)]);
  assert.equal(allEvents(result).length, 1);
  assert.equal(result.runBlocks.get('run1'), 'work_m1_ssh');
});

test('provisional stale running status is stopped and source state/connection untouched', () => {
  context.S.connectionID = 'selected-local-policy';
  context.S.remoteRuns.set('run1', nativeRun('run1'));
  context.S.renderedRemoteRuns.add('claimed-elsewhere');
  const snapshot = () => JSON.stringify(context.S, (_, value) => value instanceof Map || value instanceof Set ? [...value] : value);
  const before = snapshot();
  const result = build([]);
  assert.equal(work(result)[0].status, 'stopped');
  assert.equal(allEvents(result)[0].running, false);
  assert.equal(work(result)[0].commands, 0);
  assert.equal(snapshot(), before);
  assert.equal(context.S.msgs.size, 0);
  context.S.running = true;context.S.lastErr = 'Stopped session';
  assert.equal(work(build([]))[0].status, 'stopped');
  context.S.sid = null;
  assert.equal(build([]).blocks.size, 0);
});

test('SSH write summary uses one authoritative result path, not its relative input alias', () => {
  const patch = '--- a/app.js\n+++ b/app.js\n@@ -1 +1 @@\n-old\n+new\n';
  for (const serialize of [v => v, JSON.stringify]) {
    const p = tool('w', 'mobile_ssh_ssh_write_file', {connection_id: 'c1', path: 'app.js'}, 'completed',
      {output: serialize({path: '/srv/app.js', patch, additions: 1, deletions: 1})});
    const m = batch(p), before = JSON.stringify(p);
    let block = work(build([m]))[0];
    assert.equal(block.events.length, 1);
    assert.equal(block.edits, 1);
    assert.equal(block.label, 'Edited 1 file');
    assert.deepEqual(Array.from(context.workChangedFiles(context.workToolFacts(block.events[0]), m)), ['ssh:c1\n/srv/app.js']);
    const same = tool('again', 'mobile_ssh_ssh_write_file', {connection_id: 'c1', path: '/srv/app.js'}, 'completed',
      {output: serialize({path: '/srv/app.js', patch})});
    block = work(build([batch(p, same)]))[0];
    assert.equal(block.edits, 1);
    assert.equal(JSON.stringify(p), before);
    p.state.status = 'error';p.state.error = 'Write failed';
    assert.equal(work(build([batch(p)]))[0].edits, 0);
  }
  const p = tool('native', 'mobile_ssh_ssh_write_file', {connection_id: 'c1', path: 'app.js'}, 'running',
    {metadata: {runID: 'write-run'}});
  context.S.remoteRuns.set('write-run', nativeRun('write-run', {kind: 'write', status: 'completed', path: '/srv/app.js', patch}));
  let result = build([batch(p)]), event = allEvents(result)[0];
  assert.equal(work(result)[0].edits, 1);
  assert.deepEqual(Array.from(context.workChangedFiles(context.workToolFacts(event), event.message)), ['ssh:c1\n/srv/app.js']);
  p.state.status = 'completed';p.state.output = {path: '/canonical/app.js', patch};
  result = build([batch(p)]);event = allEvents(result)[0];
  assert.deepEqual(Array.from(context.workChangedFiles(context.workToolFacts(event), event.message)), ['ssh:c1\n/canonical/app.js']);
  p.state.output = {path: '/srv/app.js', additions: 0, deletions: 0};
  context.S.remoteRuns.clear();
  assert.equal(work(build([batch(p)]))[0].edits, 0);
});

// Exercise real card HTML as well as the model; only decorative icon constants are supplied.
const escSource = html.match(/^const esc=.*$/m);
assert.ok(escSource, 'Actual escaping helper');
vm.runInContext(escSource[0], context);
context.I = {file: '', term: '', edit: ''};
vm.runInContext(['feedTextBody', 'toolFeedCommand', 'toolFeedOutput', 'toolFeedEvent',
  'toolFeedReservedRuns', 'remoteToolCard'].map(actualFunction).join('\n'), context);
const workCard = (event, claimed = new Set()) => context.remoteToolCard({
  ...event.part, messageID: event.part.messageID || event.message.info.id,
  runID: event.runID, workBinding: true
}, claimed, new Set(), new Set(), event.key + '_activity');

test('work card does not steal or duplicate native output from a different provisional run', () => {
  context.S.running = true;
  const p = tool('recorded', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running',
    {output: {runID: 'expected-run'}});
  const m = batch(p);context.S.msgs.set(m.info.id, m);
  context.S.remoteRuns.set('different-run', nativeRun('different-run'));
  const events = allEvents(build([m])), claimed = new Set();
  assert.equal(events.length, 2);
  const source = JSON.stringify(p);
  const real = workCard(events[0], claimed);
  assert.doesNotMatch(real, /NATIVE_OUTPUT|data-run-id="different-run"/);
  assert.equal(claimed.size, 0);
  const provisional = workCard(events[1], claimed);
  assert.match(provisional, /NATIVE_OUTPUT/);
  assert.match(provisional, /data-run-id="different-run"/);
  assert.equal((real + provisional).split('NATIVE_OUTPUT').length - 1, 1);
  assert.deepEqual([...claimed], ['different-run']);
  assert.equal(JSON.stringify(p), source);
});

test('work card without chosen runID rejects every legacy binding and command fallback', () => {
  context.S.remoteRuns.set('known', nativeRun('known'));
  for (const extra of [{output: {runID: 'known'}}, {metadata: {runID: 'known'}}, {}]) {
    const p = {...tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running', extra),
      messageID: 'm1', callID: 'known', runID: undefined, workBinding: true};
    const claimed = new Set(), card = context.remoteToolCard(p, claimed);
    assert.doesNotMatch(card, /NATIVE_OUTPUT|data-run-id=/);
    assert.equal(claimed.size, 0);
  }
  assert.equal(context.S.renderedRemoteRuns.size, 0);
});

test('work card uses chosen UI runID over output/metadata IDs and preserves recorded output', () => {
  context.S.remoteRuns.set('data-run', nativeRun('data-run', {stdout: 'WRONG_NATIVE_OUTPUT'}));
  context.S.remoteRuns.set('chosen-run', nativeRun('chosen-run', {stdout: 'CHOSEN_NATIVE_OUTPUT'}));
  const p = {...tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1'}, 'running',
    {output: {runID: 'data-run'}, metadata: {runID: 'data-run'}}), messageID: 'm1',
    callID: 'data-run', runID: 'chosen-run', workBinding: true};
  const before = JSON.stringify(p), claimed = new Set();
  let card = context.remoteToolCard(p, claimed);
  assert.match(card, /CHOSEN_NATIVE_OUTPUT/);
  assert.match(card, /data-run-id="chosen-run"/);
  assert.doesNotMatch(card, /WRONG_NATIVE_OUTPUT|data-run-id="data-run"/);
  assert.deepEqual([...claimed], ['chosen-run']);
  assert.equal(JSON.stringify(p), before);
  p.state.output = {stdout: 'RECORDED_FINAL_OUTPUT', runID: 'data-run'};
  card = context.remoteToolCard(p, new Set());
  assert.match(card, /RECORDED_FINAL_OUTPUT/);
  assert.doesNotMatch(card, /CHOSEN_NATIVE_OUTPUT|WRONG_NATIVE_OUTPUT/);
});

test('work card exact binding respects session, connection and prior claims', () => {
  const run = nativeRun('chosen');context.S.remoteRuns.set(run.id, run);
  const p = {...tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running'),
    messageID: 'm1', sessionID: 'ses_test', runID: 'chosen', workBinding: true};
  for (const change of [{sessionID: 'other-session'}, {connectionID: 'other-connection'}]) {
    Object.assign(run, nativeRun('chosen'), change);
    const claimed = new Set(), card = context.remoteToolCard(p, claimed);
    assert.doesNotMatch(card, /NATIVE_OUTPUT/);
    assert.equal(claimed.size, 0);
  }
  Object.assign(run, nativeRun('chosen'));
  context.S.remoteRuns.set('fallback', nativeRun('fallback', {stdout: 'FORBIDDEN_FALLBACK'}));
  const claimed = new Set(['chosen']);
  assert.doesNotMatch(context.remoteToolCard(p, claimed), /NATIVE_OUTPUT|FORBIDDEN_FALLBACK/);
  assert.deepEqual([...claimed], ['chosen']);
  context.S.remoteRuns.delete('chosen');
  assert.doesNotMatch(context.remoteToolCard(p, new Set()), /FORBIDDEN_FALLBACK/);
});

test('non-work cards retain exact precedence and command/path fallback', () => {
  context.S.remoteRuns.set('native', nativeRun('native'));
  const p = tool('ssh', 'mobile_ssh_ssh_exec', {connection_id: 'c1', command: 'echo ready'}, 'running');
  assert.match(context.remoteToolCard(p), /NATIVE_OUTPUT/);
  assert.match(context.remoteToolCard({...p, workBinding: false}), /NATIVE_OUTPUT/);
  context.S.remoteRuns.set('chosen', nativeRun('chosen', {stdout: 'SECOND_NATIVE_OUTPUT'}));
  p.runID = 'chosen';p.state.output = {runID: 'native'};
  const card = context.remoteToolCard(p);
  assert.match(card, /NATIVE_OUTPUT/);
  assert.match(card, /data-run-id="native"/);
  assert.doesNotMatch(card, /SECOND_NATIVE_OUTPUT/);
  context.S.remoteRuns.clear();
  context.S.remoteRuns.set('write', nativeRun('write', {kind: 'write', path: '/srv/app.js'}));
  const write = tool('write', 'mobile_ssh_ssh_write_file', {connection_id: 'c1', path: 'app.js'}, 'running');
  assert.match(context.remoteToolCard(write), /data-run-id="write"/);
  assert.doesNotMatch(context.remoteToolCard({...write, workBinding: true}), /data-run-id=/);
});

test('command intent verbalizes only literal leading tokens', () => {
  const intent = command => context.workCommandIntent(command);
  assert.equal(intent('bun install 2>&1 | tail -20').title, 'Install dependencies');
  assert.equal(intent('bun install 2>&1 | tail -20').active, 'Installing dependencies');
  assert.equal(intent('npm test').title, 'Run tests');
  assert.equal(intent('cat package.json 2>/dev/null; ls').title, 'Inspect package.json');
  assert.equal(intent('curl -sL https://registry.npmjs.org/express | head -3').title, 'Check the network');
  assert.equal(intent('bun -e "const r = await fetch()"').title, 'Run an inline script');
  assert.equal(intent('git status').title, 'Check git status');
  assert.equal(intent('./gradlew assembleRelease').title, 'Build the project');
  assert.equal(intent('FOO=1 npm i').title, 'Install dependencies');
  assert.equal(intent('   '), null);
  assert.equal(intent('wat-the-unknown --flag'), null);
});

test('work tool voice: model description wins, command stays technical sub', () => {
  const described = context.workToolVoice('command', 'bash', {command: 'npm install', description: 'Set up the toolchain'}, {}, '', false);
  assert.deepEqual(plain(described), {title: 'Set up the toolchain', sub: 'npm install', active: 'Installing dependencies', generic: false});
  const mapped = context.workToolVoice('command', 'bash', {command: 'npm test'}, {}, '', false);
  assert.deepEqual(plain(mapped), {title: 'Run tests', sub: 'npm test', active: 'Running tests', generic: false});
  const fallback = context.workToolVoice('command', 'bash', {command: 'wat --unknown'}, {}, '', false);
  assert.deepEqual(plain(fallback), {title: 'Run command', sub: 'wat --unknown', active: 'Running command', generic: true});
  const stateTitle = context.workToolVoice('command', 'bash', {}, {}, 'Inspect fixture', false);
  assert.equal(stateTitle.title, 'Inspect fixture');
  // st.title that just echoes the command must not become the headline.
  const echoed = context.workToolVoice('command', 'bash', {command: 'ls -la'}, {}, 'ls -la', false);
  assert.equal(echoed.title, 'List files');
  const read = context.workToolVoice('read', 'read', {filePath: '/project/src/server.js'}, {}, '', false);
  assert.deepEqual(plain(read), {title: 'Read server.js', sub: '/project/src/server.js', active: 'Reading server.js'});
  const edit = context.workToolVoice('edit', 'edit', {filePath: 'app.js'}, {}, '', false);
  assert.equal(edit.title, 'Edit app.js');
  const doneEdit = context.workToolVoice('edit', 'edit', {filePath: 'app.js'}, {}, '', true);
  assert.equal(doneEdit.title, 'Edited app.js');
  const written = context.workToolVoice('edit', 'mobile_ssh_ssh_write_file', {path: 'app.js'}, {}, '', true);
  assert.equal(written.title, 'Wrote app.js');
  const unknown = context.workToolVoice('other', 'fixture-two', {}, {}, '', false);
  assert.equal(unknown.title, 'fixture-two');
  assert.equal(context.workActiveLabel({kind: 'read', part: {tool: 'read', state: {input: {path: 'server.js'}}}}), 'Reading server.js');
  assert.equal(context.workActiveLabel({kind: 'command', part: {tool: 'bash', state: {input: {command: 'bun install'}}}}), 'Installing dependencies');
});

test('one operation with attempts: label, final state, honest attempt count', () => {
  const m = batch(reason('r1'), tool('bad', 'bash', {command: 'npm install'}, 'error', {error: 'boom'}),
    reason('r2'), tool('good', 'bash', {command: 'bun install'}, 'completed', {metadata: {exitCode: 0}}));
  const block = work(build([m]))[0];
  assert.equal(block.opKey, 'Install dependencies');
  assert.equal(block.label, 'Install dependencies');
  assert.equal(block.status, 'completed');
  assert.equal(block.attempts, 2);
  assert.equal(block.errors, 1);
  assert.match(block.detail, /1 failed · 2 attempts/);
});

test('adjacent same-intent blocks split by commentary collapse into one row', () => {
  const m1 = message('m1', [reason('r1'), tool('c1', 'bash', {command: 'npm install'}, 'error', {error: 'boom'})], {parentID: 'u1'});
  const commentary = message('m2', [text('t', 'npm is unavailable, trying via bun:')], {parentID: 'u1'});
  const m3 = message('m3', [tool('c2', 'bash', {command: 'bun install'}, 'completed', {metadata: {exitCode: 0}})], {parentID: 'u1'});
  const result = build([user('u1'), m1, commentary, m3]);
  assert.deepEqual(types(result), ['user', 'text', 'work']);
  const block = work(result)[0];
  assert.equal(block.events.length, 3);
  assert.equal(block.label, 'Install dependencies');
  assert.equal(block.status, 'completed');
  assert.equal(result.blocks.size, 1);
  // The surviving block keeps the earlier stable id, so an open sheet never closes mid-run.
  assert.equal(block.id, 'work_m1_r1');
});

test('different intents and generic commands never merge', () => {
  const m1 = message('m1', [tool('c1', 'bash', {command: 'npm install'}, 'error', {error: 'x'})], {parentID: 'u1'});
  const between = message('m2', [text('t', 'Trying another way.')], {parentID: 'u1'});
  const m3 = message('m3', [tool('c2', 'bash', {command: 'wat --unknown'}, 'completed')], {parentID: 'u1'});
  const result = build([user('u1'), m1, between, m3]);
  assert.deepEqual(types(result), ['user', 'work', 'text', 'work']);
  assert.equal(work(result)[0].label, 'Install dependencies');
  assert.equal(work(result)[1].label, 'Ran 1 command');
  assert.equal(work(result)[0].status, 'error');
});

test('render units group same-intent attempts and absorb interleaved reasoning', () => {
  const m = batch(tool('c1', 'bash', {command: 'npm install'}, 'error', {error: 'boom'}),
    reason('r2'), tool('c2', 'bash', {command: 'bun install'}, 'completed', {metadata: {exitCode: 0}}),
    reason('r3'), tool('r9', 'read', {filePath: 'x.js'}));
  const block = work(build([m]))[0];
  const units = context.workRenderUnits(block);
  assert.deepEqual(plain(units.map(u => u.type)), ['group', 'note', 'single']);
  assert.equal(units[0].events.length, 2); // interleaved reasoning absorbed
  assert.equal(units[1].event.segment.key, 'm1_r3'); // standalone note survives
  assert.equal(units[2].event.part.id, 'r9');
});

test('structured questions parse safely; malformed input never throws', () => {
  const input = {questions: [{question: 'Where is the project?', header: 'Project not found',
    options: [{label: 'Create new in /root/wptube', description: 'Fresh folder'}, {label: 'Another SSH host'}, {label: 'Clarify the task'}]}]};
  const facts = context.workQuestionFacts({state: {input}});
  assert.equal(facts.questions.length, 1);
  assert.equal(facts.questions[0].header, 'Project not found');
  assert.equal(facts.questions[0].options.length, 3);
  assert.equal(facts.questions[0].options[0].description, 'Fresh folder');
  assert.equal(facts.answers, null);
  assert.equal(facts.dismissed, false);
  const done = context.workQuestionFacts({state: {input, output: {answers: [['Create new in /root/wptube']]}}});
  assert.deepEqual(plain(done.answers), [['Create new in /root/wptube']]);
  const asString = context.workQuestionFacts({state: {input: JSON.stringify(input), output: JSON.stringify({answers: ['Clarify the task']})}});
  assert.deepEqual(plain(asString.answers), [['Clarify the task']]);
  assert.equal(asString.questions[0].header, 'Project not found');
  const dismissed = context.workQuestionFacts({state: {input, error: 'The user dismissed this question'}});
  assert.equal(dismissed.dismissed, true);
  const missing = context.workQuestionFacts({state: {input: {questions: [{question: 'Q'}]}}});
  assert.deepEqual(plain(missing.questions), [{question: 'Q', header: '', multiple: false, options: []}]);
  const bad = context.workQuestionFacts({state: {input: '{oops'}});
  assert.deepEqual(plain(bad), {questions: [], answers: null, dismissed: false});
  const alien = context.workQuestionFacts({state: {input: {foo: '{not a question}'}}});
  assert.deepEqual(plain(alien), {questions: [], answers: null, dismissed: false});
  const garbage = context.workQuestionFacts({state: {output: 'not json at all'}});
  assert.deepEqual(plain(garbage), {questions: [], answers: null, dismissed: false});
});

console.log('\n' + passed + ' work transcript unit checks passed; full inline script syntax valid.');
