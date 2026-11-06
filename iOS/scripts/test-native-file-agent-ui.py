#!/usr/bin/env python3
"""Behavioral staged iOS UI proof; backend/ledger/filesystem responses are UI-only fixtures.

Run: python iOS/scripts/test-native-file-agent-ui.py [--case NAME] [--output DIR]
Uses tests/requirements.txt's existing Playwright (or ../.local/api-test-venv)
and an installed Chromium/Chrome; never installs dependencies or browsers.

The complete stage-native-ui.py output boots over a process-owned localhost
asset server. Only webkit.codexNative's transport endpoint is emulated; nativeCall,
api, subscription, permission, history, syncSession and renderers are unchanged.
No model, Swift backend, Git, filesystem mutation, Android build or device runs.
"Restore" means restoring session snapshots, NOT undoing native file patches
(the native ledger has no historyRestore action). Permission previews must render
only prepared file-modification diffs; input/before/after never fabricate changes.
"""

import argparse
import asyncio
import hashlib
import importlib
import importlib.util
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT.parent / '.local'
SOURCE = ROOT / 'app/src/main/assets/codex.html'
RESOURCES = ROOT / 'iOS/Sources/AppHarness-Resources'
STAGER = Path(__file__).with_name('stage-native-ui.py')
DIRECTORY = '/private/var/mobile/Containers/Data/Application/UI-FIXTURE/Documents/File Agent'
SID = 'ses_native_ui_fixture'
FILE = DIRECTORY + '/review <safe>.txt'
USER, ASSISTANT = 'msg_001_user', 'msg_002_assistant'
BEFORE = 'context\nOLD <script>window.diffInjected=true</script>\n'
AFTER = 'context\nNEW <img src=x onerror="window.diffInjected=true">\n'
PATCH = ('--- ' + FILE + '\n+++ ' + FILE + '\n@@ -1,2 +1,2 @@\n context\n'
         '-OLD <script>window.diffInjected=true</script>\n'
         '+NEW <img src=x onerror="window.diffInjected=true">\n')
TOKEN = 'native:11111111-1111-4111-8111-111111111111'
KEY = '22222222-2222-4222-8222-222222222222'
NEXT_KEY = '33333333-3333-4333-8333-333333333333'
NEXT_PATCH = '--- ' + FILE + '\n+++ ' + FILE + '\n@@ -2 +2 @@\n-second_old\n+second_new\n'
NESTED = DIRECTORY + '/nested <safe>'
DEEP = NESTED + '/deep'
EMPTY = DIRECTORY + '/empty'
LATER = DIRECTORY + '/later'
NESTED_FILE = NESTED + '/child.txt'
DEEP_FILE = DEEP + '/deep.txt'
LATER_FILE = LATER + '/later.txt'
STALE_DIRECTORY = DIRECTORY.replace('UI-FIXTURE', 'OLD-UI-FIXTURE')
SESSION = {'id': SID, 'directory': DIRECTORY, 'title': 'Native file UI fixture',
           'permission': [], 'time': {'created': 1000, 'updated': 2000}}
# NativeAgentBackend.documentation route shapes; /event and /question are absent.
ROUTES = {'/doc': ['get'], '/global/health': ['get'], '/global/config': ['get', 'patch'],
          '/config': ['get'], '/config/providers': ['get'], '/provider': ['get'],
          '/auth/{providerID}': ['put', 'delete'], '/path': ['get'], '/project': ['get'],
          '/session': ['get', 'post'], '/experimental/session': ['get'],
          '/session/status': ['get'], '/session/{sessionID}': ['get', 'patch', 'delete'],
          '/session/{sessionID}/message': ['get'], '/session/{sessionID}/prompt_async': ['post'],
          '/session/{sessionID}/abort': ['post'], '/session/{sessionID}/diff': ['get'],
          '/permission': ['get'], '/permission/{requestID}/reply': ['post']}
DOC = {'openapi': '3.1.0', 'paths': {p: {m: {} for m in methods} for p, methods in ROUTES.items()}}


def require(value, message):
    if not value:
        raise AssertionError(message)


def metadata():
    # Exact native prepared-write fields, including lowercase filepath and before/after.
    return {'filepath': FILE, 'resolvedPath': FILE, 'diff': PATCH, 'before': BEFORE,
            'after': AFTER, 'additions': 1, 'deletions': 1, 'historyKind': 'native-patch'}


def message(mid, role, parts, completed=False):
    info = {'id': mid, 'sessionID': SID, 'role': role, 'time': {'created': 1000}}
    if role == 'assistant':
        info['parentID'] = USER
    if completed:
        info['time']['completed'] = 2000
    return {'info': info, 'parts': [dict(p, messageID=mid, sessionID=SID) for p in parts]}


def tool(pid, name, status='completed', **state):
    return {'id': pid, 'type': 'tool', 'tool': name, 'callID': 'call_' + pid,
            'state': {'status': status, 'input': {'filePath': FILE},
                      'time': {'start': 1100, 'end': 1500}, **state}}


def snapshots(completed=True):
    parts = [{'id': 'prt_001_reason', 'type': 'reasoning', 'text': 'Inspect first.',
              'time': {'start': 1000, 'end': 1100}},
             tool('prt_002_read', 'read', output='READ_SNAPSHOT'),
             {'id': 'prt_003_reason', 'type': 'reasoning', 'text': 'Compare next.',
              'time': {'start': 1200, 'end': 1300}},
             tool('prt_004_edit', 'edit', status='completed' if completed else 'running',
                  input={'filePath': FILE, 'oldString': BEFORE, 'newString': AFTER},
                  metadata=dict(metadata(), **({'historyKey': KEY, 'historyCommit': TOKEN} if completed else {})),
                  **({'output': 'EDIT_SNAPSHOT'} if completed else {}))]
    if completed:
        parts += [{'id': 'prt_005_reason', 'type': 'reasoning', 'text': 'Verify last.',
                   'time': {'start': 1600, 'end': 1700}},
                  {'id': 'prt_006_answer', 'type': 'text', 'text': 'FINAL_SNAPSHOT'}]
    return [message(USER, 'user', [{'id': 'prt_prompt', 'type': 'text', 'text': 'Review this file.'}]),
            message(ASSISTANT, 'assistant', parts, completed)]


class NativeFixture:
    """Strict response fixture, not a replacement backend or ledger implementation."""

    def __init__(self):
        self.messages = snapshots()
        self.status = 'idle'
        self.permissions = []
        self.trace, self.unexpected = [], []
        self.reply_gate = None
        # Exact JSBridge fsList/fsRead/fsWrite payloads; no filesystem or UI functions mocked.
        self.fs_dirs = {DIRECTORY: ['nested <safe>', 'empty', 'later'],
                        NESTED: ['deep'], DEEP: [], EMPTY: [], LATER: []}
        self.fs_contents = {FILE: AFTER, NESTED_FILE: 'CHILD_CONTENT\n',
                            DEEP_FILE: 'DEEP_CONTENT\n', LATER_FILE: 'LATER_CONTENT\n'}
        self.fs_errors, self.fs_gates, self.fs_completed = {}, {}, []
        # The session can retain an obsolete container while native's current project exists.
        self.session_directory = DIRECTORY
        # Ledger IDs deliberately differ from messageID:callID; no fake Git hashes.
        self.files = [
            {'key': KEY, 'file': FILE, 'source': 'local', 'connectionID': '',
             'status': 'modified', 'additions': 1, 'deletions': 1, 'patch': PATCH},
            {'key': NEXT_KEY, 'file': FILE, 'source': 'local', 'connectionID': '',
             'status': 'modified', 'additions': 1, 'deletions': 1, 'patch': NEXT_PATCH},
            {'key': 'other-file-record', 'file': DIRECTORY + '/unrelated.txt', 'source': 'local',
             'connectionID': '', 'status': 'added', 'additions': 1, 'deletions': 0,
             'patch': '--- /dev/null\n+++ unrelated.txt\n@@ -0,0 +1 @@\n+UNRELATED_PATCH\n'}]

    def pending(self, pid='per_native_fixture'):
        self.status = 'busy'
        self.messages = snapshots(False)
        p = {'id': pid, 'sessionID': SID, 'permission': 'edit', 'patterns': [FILE],
             'metadata': metadata(), 'tool': {'messageID': ASSISTANT, 'callID': 'call_prt_004_edit'}}
        self.permissions = [p]
        return deepcopy(p)

    async def request(self, source, q):
        self.trace.append(deepcopy(q))
        try:
            value, events = await self.respond(q['action'], q.get('data', {}))
            return {'value': deepcopy(value), 'events': deepcopy(events)}
        except (AssertionError, KeyError, TimeoutError) as error:
            self.unexpected.append(str(error))
            return {'value': {'error': str(error)}, 'events': []}

    async def respond(self, action, data):
        if action == 'backendSubscribe':
            return {'subscribed': True}, []
        if action == 'projects':
            return {'projects': [DIRECTORY]}, []
        if action == 'connections':
            return {'connections': []}, []
        if action == 'remoteHistory':
            return {'events': []}, []
        if action == 'cancelRemote':
            require(data == {'sessionID': SID}, 'cancelRemote targeted wrong session')
            return {'cancelled': 0}, []
        if action == 'sttGet':
            return {'mode': 'native', 'hasKey': False}, []
        if action in ('appearance', 'turnDone'):
            return {}, []
        if action == 'toast':
            require(set(data) == {'msg'}, 'Unexpected toast payload')
            return {}, []
        if action in ('fsList', 'fsRead', 'fsWrite'):
            require(set(data) <= ({'path', 'content'} if action == 'fsWrite' else {'path'}),
                    'Unexpected filesystem payload: ' + repr(data))
            path = data.get('path', DIRECTORY)
            require(path in self.fs_dirs or path in self.fs_contents or path == STALE_DIRECTORY,
                    'Unexpected filesystem path: ' + repr(path))
            key = (action, path)
            if key in self.fs_gates:
                await asyncio.wait_for(self.fs_gates[key].wait(), 10)
            self.fs_completed.append(key)
            if key in self.fs_errors:
                return {'error': self.fs_errors[key]}, []
            if action == 'fsList':
                require(path in self.fs_dirs, 'fsList expected fixture directory')
                files = [{'name': p.rsplit('/', 1)[1], 'size': len(content.encode('utf-8'))}
                         for p, content in self.fs_contents.items() if p.rsplit('/', 1)[0] == path]
                return {'path': path, 'dirs': self.fs_dirs[path], 'files': files}, []
            require(path in self.fs_contents, 'fsRead/fsWrite expected fixture file')
            if action == 'fsWrite':
                require(isinstance(data.get('content'), str), 'fsWrite content must be text')
                self.fs_contents[path] = data['content']
                return {'saved': True, 'size': len(data['content'].encode('utf-8'))}, []
            content = self.fs_contents[path]
            return {'content': content, 'size': len(content.encode('utf-8'))}, []
        if action.startswith('history'):
            require(data.get('directory') == DIRECTORY, 'history directory mismatch')
            require(data.get('sessionID') == SID, 'history session mismatch')
            if action in ('historyRecord', 'historyHint'):
                # Swift ignores UI hints/manifests: never make them create fixture records.
                return {'saved': False, 'ignored': True, 'historyKind': 'native-patch'}, []
            if action == 'historyList':
                files = [{k: v for k, v in f.items() if k != 'patch'} for f in self.files]
                return {'historyKind': 'native-patch', 'turns': [
                    {'id': USER, 'title': 'Native edit evidence', 'time': 1000,
                     'commit': TOKEN, 'files': files}]}, []
            if action == 'historyShow':
                require(data.get('commit') == TOKEN, 'historyShow must pass native:<id>, not Git')
                return {'historyKind': 'native-patch', 'files': self.files}, []
        require(action == 'backendRequest', 'Unexpected native action: ' + action)
        method, path = data.get('method'), data.get('path')
        require(data.get('directory') == self.session_directory, 'backendRequest directory mismatch: ' + str(data))
        session = dict(SESSION, directory=self.session_directory)
        if method == 'GET':
            values = {'/doc': DOC, '/global/health': {'healthy': True, 'version': 'ui-only-fixture'},
                      '/config/providers': {'providers': [], 'default': {}}, '/config': {},
                      '/path': {'directory': DIRECTORY}, '/project': [{'worktree': DIRECTORY}],
                      '/session': [session], '/experimental/session': [session], '/session/' + SID: session,
                      '/session/' + SID + '/message': self.messages,
                      '/session/status': {SID: {'type': self.status}}, '/permission': self.permissions,
                      # The canonical sync probes this unsupported native route via allSettled.
                      '/question': {'error': 'Unsupported native backend route: GET question.'}}
            require(path in values, 'Unexpected native GET: ' + str(path))
            return values[path], []
        if method == 'POST' and path.startswith('/permission/') and path.endswith('/reply'):
            require(len(self.permissions) == 1, 'Duplicate/no pending permission reply')
            p = self.permissions[0]
            require(path == '/permission/' + p['id'] + '/reply', 'Wrong permission reply path')
            require(data.get('body') in ({'reply': 'once'}, {'reply': 'reject'}), 'Wrong reply body')
            if self.reply_gate:
                await asyncio.wait_for(self.reply_gate.wait(), 10)
            self.permissions = []
            return True, [{'type': 'permission.replied', 'properties': {
                'sessionID': SID, 'requestID': p['id'], 'reply': data['body']['reply']}}]
        if method == 'POST' and path == '/session/' + SID + '/abort':
            require('body' not in data, 'Stop unexpectedly sent a write/prompt body')
            self.status, self.permissions = 'idle', []
            return True, []  # Stop must reconcile via the actual syncSession, not a fake event.
        raise AssertionError('Unexpected native request: ' + str(data))

    def calls(self, action, path=None):
        return [q for q in self.trace if q['action'] == action and
                (path is None or q.get('data', {}).get('path') == path)]


@contextmanager
def asset_server(staged):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            requests.append({'method': 'GET', 'path': path})
            if path == '/_ui/index.html':
                file = staged
            elif path.startswith('/_ui/'):
                name = path[len('/_ui/'):]
                file = RESOURCES / name
                if Path(name).name != name or not file.is_file():
                    self.send_error(404)
                    return
            elif path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
                return
            else:
                self.send_error(404, 'Only static UI assets are served; no HTTP backend')
                return
            body = file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if file == staged else
                             mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        require(not thread.is_alive(), 'Static server did not terminate')


BRIDGE = """directory => {
  window.KumoNativeBackend = true;
  localStorage.clear();
  localStorage.setItem('oc_dir', JSON.stringify(directory));
  localStorage.setItem('oc_lp_hint', 'true');
  window.webkit = {messageHandlers: {codexNative: {postMessage(q) {
    window.__nativeFixture(q).then(r => {
      for (const event of r.events) window.dispatchEvent(new CustomEvent('kumoBackendEvent', {detail:event}));
      if (q.id) window.codexNativeResult(q.id, r.value);
    });
  }}}};
}"""


async def frames(page):
    await page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


async def publish(page, event):
    await page.evaluate("event => window.dispatchEvent(new CustomEvent('kumoBackendEvent', {detail:event}))", event)
    await frames(page)


async def wait_calls(page, fixture, action, path=None, count=1):
    for _ in range(80):
        if len(fixture.calls(action, path)) >= count:
            return
        await page.wait_for_timeout(25)
    raise AssertionError('Missing transport call: ' + action + ' ' + str(path))


async def open_thread(page):
    await page.locator('#menu').click()
    await page.locator('#drawer [data-sid="' + SID + '"]').click()
    await page.wait_for_function('S.sid === ' + json.dumps(SID) + ' && S.msgs.size === 2 && syncing === null')
    await frames(page)


async def asked(page, fixture, pid):
    p = fixture.pending(pid)
    await publish(page, {'type': 'permission.asked', 'properties': p})
    await page.locator('[data-pid="' + pid + '"]').wait_for(state='visible')
    return page.locator('[data-pid="' + pid + '"]')


async def permission_diff(page, fixture, expect):
    card = await asked(page, fixture, 'per_diff')
    await expect(card).to_contain_text(FILE)
    # Proposed changes are not successful-edit evidence and must stay actionable in the feed.
    await expect(page.locator('#workLogBody [data-pid]')).to_have_count(0)
    await expect(page.locator('#main [data-r="once"]')).to_be_enabled()
    rows = await card.locator('.diff-line').evaluate_all(
        "els => els.map(e => ({kind:e.classList.contains('a')?'add':e.classList.contains('d')?'delete':'context', text:e.querySelector('.diff-text')?.textContent}))")
    require(any(r['kind'] == 'delete' and r['text'] == BEFORE.splitlines()[1] for r in rows) and
            any(r['kind'] == 'add' and r['text'] == AFTER.splitlines()[1] for r in rows),
            'permission.asked metadata.diff/before/after is not rendered in the approval card; observed diff rows: ' + repr(rows))
    preview = card.locator('.permission-change')
    await expect(preview).to_have_attribute('open', '')
    await expect(preview.locator('summary')).to_have_text('Proposed changes')
    await expect(preview.locator('.tool-command')).to_have_text(FILE)
    await expect(preview.locator('.diff-line.d .diff-text')).to_be_visible()
    await expect(preview.locator('.diff-line.a .diff-text')).to_be_visible()
    await expect(card.locator('script,img,safe')).to_have_count(0)
    require(rows == [{'kind': 'context', 'text': 'context'},
                     {'kind': 'delete', 'text': BEFORE.splitlines()[1]},
                     {'kind': 'add', 'text': AFTER.splitlines()[1]},
                     {'kind': 'context', 'text': ' '}], 'Preview altered prepared diff rows')
    require(not await page.evaluate('Boolean(window.diffInjected)'), 'Diff HTML executed instead of escaping')
    await preview.locator('summary').click()
    await expect(preview).not_to_have_attribute('open', '')
    await expect(preview.locator('.diff-line.a .diff-text')).to_be_hidden()
    await preview.locator('summary').click()
    await expect(preview.locator('.diff-line.a .diff-text')).to_be_visible()
    require(not fixture.calls('backendRequest', '/permission/per_diff/reply'), 'Preview answered permission')


async def publish_permission(page, fixture, p):
    fixture.permissions = [deepcopy(p)]
    await publish(page, {'type': 'permission.asked', 'properties': p})
    return page.locator('[data-pid="' + p['id'] + '"]')


async def permission_fallback(page, fixture, expect):
    p = fixture.pending('per_fallback')
    # Both snapshots and the bound tool inputs exist. None is permission-diff evidence.
    for diff in (None, '', ' \n\t', {'old': BEFORE, 'new': AFTER}, [PATCH]):
        p['metadata'] = {'filepath': FILE, 'before': BEFORE, 'after': AFTER}
        if diff is not None:
            p['metadata']['diff'] = diff
        card = await publish_permission(page, fixture, p)
        await expect(card).to_contain_text(FILE)
        await expect(card.locator('.permission-change,.diff-line')).to_have_count(0)
        await expect(card.locator('[data-r="once"]')).to_be_enabled()
        await expect(card.locator('[data-r="reject"]')).to_be_enabled()
    await expect(card.locator('script,img')).to_have_count(0)
    require(not fixture.calls('backendRequest', '/permission/per_fallback/reply'), 'Fallback replied without a click')


async def permission_types(page, fixture, expect):
    p = fixture.pending('per_types')
    for permission in ('write', 'edit', 'patch', 'apply_patch', 'multiedit', 'read', 'bash', 'external_directory'):
        p['permission'] = permission
        card = await publish_permission(page, fixture, p)
        await expect(card.locator('.permission-change')).to_have_count(
            1 if permission in ('write', 'edit', 'patch', 'apply_patch', 'multiedit') else 0)
    require(not any(q['data'].get('method') == 'POST' for q in fixture.calls('backendRequest')),
            'Permission type preview triggered a backend mutation')


async def permission_prepared(page, fixture, expect):
    fixture.messages = snapshots(False)
    fixture.messages[1]['parts'][-1]['state']['metadata'] = {}
    await page.evaluate('syncSession()')
    await page.locator('#feed .work-summary').click()
    await expect(page.locator('#workLogDialog')).to_be_visible()
    edit = page.locator('#workLogBody [data-tool-part="prt_004_edit"]')
    await expect(edit).to_have_count(1)
    await expect(edit.locator('.diff-line')).to_have_count(0)
    p = {'id': 'per_prepared', 'sessionID': SID, 'permission': 'edit', 'patterns': [FILE],
         'metadata': {'filepath': FILE, 'before': BEFORE, 'after': AFTER},
         'tool': {'messageID': ASSISTANT, 'callID': 'call_prt_004_edit'}}
    card = await publish_permission(page, fixture, p)
    await expect(card.locator('.permission-change')).to_have_count(0)
    await expect(page.locator('#workLogDialog')).not_to_be_visible()
    # The real subscription receives the same bound request once preparation supplies a patch.
    p['metadata']['diff'] = PATCH
    card = await publish_permission(page, fixture, p)
    await expect(card.locator('.permission-change')).to_have_count(1)
    await expect(card.locator('.diff-line.d .diff-text')).to_have_text(BEFORE.splitlines()[1])
    await expect(card.locator('.diff-line.a .diff-text')).to_have_text(AFTER.splitlines()[1])
    await expect(page.locator('#feed .file-chip')).to_have_count(0)
    require(not any(q['data'].get('method') == 'POST' for q in fixture.calls('backendRequest')),
            'Prepared preview wrote/submitted anything')


async def permission_bounded(page, fixture, expect):
    await page.set_viewport_size({'width': 390, 'height': 844})
    p = fixture.pending('per_bounded')
    p['permission'] = 'write'
    lines = ['added_' + str(i) for i in range(320)] + ['LAST_REAL_DIFF_LINE']
    p['metadata']['diff'] = '--- /dev/null\n+++ ' + FILE + '\n@@ -0,0 +1,321 @@\n' + '\n'.join('+' + line for line in lines)
    card = await publish_permission(page, fixture, p)
    preview = card.locator('.permission-change .change-preview')
    await expect(preview.locator('.diff-line.a')).to_have_count(len(lines))
    require(await preview.locator('.diff-text').all_text_contents() == lines,
            'Bounded preview truncated/altered actual patch content')
    size = await preview.evaluate('e => ({height:e.clientHeight,full:e.scrollHeight,max:parseFloat(getComputedStyle(e).maxHeight),overflow:getComputedStyle(e).overflowY})')
    require(0 < size['height'] <= size['max'] + 1 and size['full'] > size['height'] and size['overflow'] == 'auto',
            'Permission diff is not bounded/scrollable: ' + repr(size))
    await expect(card.locator('[data-r="once"]')).to_be_visible()
    await expect(card.locator('[data-r="reject"]')).to_be_visible()
    await preview.evaluate('e => e.scrollTop = e.scrollHeight')
    await expect(preview.locator('.diff-line.a').last).to_be_visible()
    require(not any(q['data'].get('method') == 'POST' for q in fixture.calls('backendRequest')),
            'Scrolling a proposed diff triggered a mutation')


async def permission_reply(page, fixture, expect, reply):
    pid = 'per_' + reply
    card = await asked(page, fixture, pid)
    await expect(card).to_contain_text(FILE)
    fixture.reply_gate = asyncio.Event()
    button = card.locator('[data-r="' + reply + '"]')
    await button.click()
    path = '/permission/' + pid + '/reply'
    await wait_calls(page, fixture, 'backendRequest', path)
    await expect(button).to_be_disabled()
    bounds = await button.bounding_box()
    await page.mouse.click(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
    await frames(page)
    require(len(fixture.calls('backendRequest', path)) == 1, 'Repeated click duplicated reply')
    q = fixture.calls('backendRequest', path)[0]
    require(q['data'] == {'path': path, 'method': 'POST', 'directory': DIRECTORY, 'body': {'reply': reply}},
            'Incorrect permission native backendRequest: ' + repr(q))
    fixture.reply_gate.set()
    await expect(card).to_have_count(0)
    await page.wait_for_function('S.perms.size === 0')
    await frames(page)
    require(len(fixture.calls('backendRequest', path)) == 1, 'Reply sent more than once')


async def allow(page, fixture, expect):
    await permission_reply(page, fixture, expect, 'once')


async def reject(page, fixture, expect):
    await permission_reply(page, fixture, expect, 'reject')


async def stop(page, fixture, expect):
    await asked(page, fixture, 'per_stop')
    await publish(page, {'type': 'session.status', 'properties': {'sessionID': SID, 'status': {'type': 'busy'}}})
    # Composer contains unsent text: Stop must not submit it or perform any actual write.
    await page.locator('#inp').fill('UNSENT_TEXT_MUST_NOT_WRITE')
    before = len(fixture.calls('backendRequest', '/session/' + SID + '/message'))
    await page.locator('#send').click()
    await page.wait_for_function('!S.running && S.perms.size === 0 && syncing === null')
    calls = fixture.calls('backendRequest', '/session/' + SID + '/abort')
    require(len(calls) == 1 and calls[0]['data'] == {
        'path': '/session/' + SID + '/abort', 'method': 'POST', 'directory': DIRECTORY},
        'Stop did not send exactly one current-session/directory abort')
    require(len(fixture.calls('backendRequest', '/session/' + SID + '/message')) > before,
            'Stop did not reconcile its native snapshot via syncSession')
    require(not fixture.calls('backendRequest', '/permission/per_stop/reply'), 'Stop accidentally approved/denied')
    require(not any(q['data'].get('path', '').endswith('/prompt_async') for q in fixture.calls('backendRequest')),
            'Stop submitted a prompt')
    await expect(page.locator('#inp')).to_have_value('UNSENT_TEXT_MUST_NOT_WRITE')


async def history(page, fixture, expect):
    await page.locator('#diffPanel').click()
    await expect(page.locator('#sheet .change-turn')).to_have_count(1)
    await expect(page.locator('#sheet h4')).to_contain_text('Native edit evidence')
    mapping = await page.evaluate('Array.from(S.historyFiles, ([key,file]) => ({key,commit:file.commit,file:file.file}))')
    require({r['key'] for r in mapping} == {f['key'] for f in fixture.files},
            'historyList did not map the native ledger keys unchanged')
    require(all(r['commit'] == TOKEN for r in mapping), 'Native patch tokens replaced with fake Git commits')
    card = page.locator('#sheet details[data-file="0"]')
    await expect(card.locator('summary')).to_contain_text(FILE)
    await expect(card.locator('.counts')).to_have_text('+2 −2')
    await expect(card.locator('.diff-line')).to_have_count(0)
    await card.locator('summary').click()
    await expect(card).to_have_attribute('data-loaded', 'true')
    texts = await card.locator('.diff-text').all_text_contents()
    require(texts == ['context', BEFORE.splitlines()[1], AFTER.splitlines()[1], ' ',
                      'second_old', 'second_new', ' '], 'historyShow lost/reordered per-operation diff rows: ' + repr(texts))
    require('UNRELATED_PATCH' not in texts, 'History preview mixed another file into this diff')
    require(len(fixture.calls('historyShow')) == 1, 'History expansion made duplicate native requests')
    await card.locator('summary').click()
    await card.locator('summary').click()
    await frames(page)
    require(len(fixture.calls('historyShow')) == 1, 'History preview ignored its loaded cache')
    require(not await page.evaluate('Boolean(window.diffInjected)'), 'History patch executed HTML')
    await expect(card.locator('script,img')).to_have_count(0)


async def chronology(page, fixture, expect):
    # Replace existing part IDs with authoritative completed snapshots. No assignments to S.
    fixture.messages = snapshots(False)
    await page.evaluate('syncSession()')
    require(not await page.locator('#feed .msg.ai').count(), 'Premature final snapshot appeared')
    fixture.messages = snapshots()
    await page.evaluate('syncSession()')
    await expect(page.locator('#feed .msg.ai')).to_have_text('FINAL_SNAPSHOT')
    order = await page.locator('#feed > .msg, #feed > .work-summary').evaluate_all(
        "els => els.map(e => e.classList.contains('work-summary')?'work':e.classList.contains('user')?'user':'answer')")
    require(order == ['user', 'work', 'answer'], 'Restore reordered feed chronology: ' + repr(order))
    await page.locator('#feed .work-summary').click()
    await expect(page.locator('#workLogDialog')).to_be_visible()
    kinds = await page.locator('#workLogBody > .work-log-entry').evaluate_all('els => els.map(e => e.dataset.kind)')
    require(kinds == ['reasoning', 'read', 'reasoning', 'edit', 'reasoning'],
            'Restore merged/reordered reasoning/tools: ' + repr(kinds))
    require(not await page.locator('.stage-note .tool-event, .stage-note .change-preview').count(),
            'Tools/diffs nested inside restored reasoning')
    edit = page.locator('#workLogBody [data-tool-part="prt_004_edit"]')
    await expect(edit).to_have_attribute('data-state', 'completed')
    await edit.locator('summary').click()
    await expect(edit.locator('.diff-line.d .diff-text')).to_have_text(BEFORE.splitlines()[1])
    await expect(edit.locator('.diff-line.a .diff-text')).to_have_text(AFTER.splitlines()[1])
    await expect(edit).to_contain_text('EDIT_SNAPSHOT')
    require(not await page.evaluate('Boolean(window.diffInjected)'), 'Restored tool diff executed HTML')
    await page.evaluate('syncSession()')
    require(await page.locator('#workLogBody > .work-log-entry').evaluate_all('els => els.map(e => e.dataset.kind)') == kinds,
            'Second sync duplicated/reordered restored entries')
    await expect(page.locator('#feed .msg.ai')).to_have_count(1)


async def foreign_session(page, fixture, expect):
    p = fixture.pending('per_foreign')
    p['sessionID'] = 'ses_other'
    fixture.permissions = []
    await publish(page, {'type': 'permission.asked', 'properties': p})
    await publish(page, {'type': 'session.status', 'properties': {'sessionID': 'ses_other', 'status': {'type': 'busy'}}})
    await expect(page.locator('[data-pid]')).to_have_count(0)
    require(not await page.evaluate('S.running'), 'Foreign session changed current Stop state')
    require(not fixture.calls('historyHint'), 'Foreign permission leaked a native history hint')


async def open_files(page, fixture, entry='drawer'):
    require(await page.evaluate('S.sid') == SID, 'Files must open from the selected session')
    if entry == 'drawer':
        await page.locator('#menu').click()
        await page.locator('#drawer [data-a="files"]').click()
    else:
        await page.locator('#plus').click()
        await page.locator('#attachFiles2').click()
    await page.locator('#fmScreen').wait_for(state='visible')
    await wait_calls(page, fixture, 'fsList')


def fm_dir(page, name):
    return page.locator('#fmList [data-fm-dir=' + json.dumps(name) + ']')


def fm_file(page, path):
    return page.locator('#fmList [data-fm-file=' + json.dumps(path.rsplit('/', 1)[1]) + ']')


def fm_crumb(page, path):
    return page.locator('#fmCrumb [data-crumb=' + json.dumps(path) + ']')


async def files_browser(page, fixture, expect):
    await open_files(page, fixture)
    require(fixture.calls('fsList')[0]['data'] == {'path': DIRECTORY},
            'Drawer Files did not use the selected session directory')
    await expect(fm_crumb(page, DIRECTORY)).to_have_text('File Agent')
    await expect(page.locator('#fmCrumb')).not_to_contain_text('sdcard')
    await expect(fm_file(page, FILE)).to_be_visible()
    await expect(fm_dir(page, 'nested <safe>')).to_be_visible()
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value(AFTER)
    await expect(page.locator('#fmViewName')).to_have_text(FILE.rsplit('/', 1)[1])
    require(fixture.calls('fsRead')[-1]['data'] == {'path': FILE}, 'File click read wrong path')
    require(not await page.evaluate('window.diffInjected'), 'File content was interpreted as HTML')
    await page.locator('#fmBack').click()
    await expect(page.locator('#fmView')).to_be_hidden()
    await fm_dir(page, 'nested <safe>').click()
    await expect(fm_file(page, NESTED_FILE)).to_be_visible()
    await fm_dir(page, 'deep').click()
    await expect(fm_file(page, DEEP_FILE)).to_be_visible()
    await expect(fm_crumb(page, NESTED)).to_be_visible()
    await fm_crumb(page, NESTED).click()
    await expect(fm_file(page, NESTED_FILE)).to_be_visible()
    await fm_file(page, NESTED_FILE).click()
    await expect(page.locator('#fmText')).to_have_value('CHILD_CONTENT\n')
    await page.locator('#fmBack').click()
    await fm_crumb(page, DIRECTORY).click()
    await expect(fm_file(page, FILE)).to_be_visible()
    await fm_dir(page, 'empty').click()
    await expect(page.locator('#fmList')).to_have_text('Empty')
    await fm_crumb(page, DIRECTORY).click()
    await expect(fm_file(page, FILE)).to_be_visible()
    await page.locator('#fmBack').click()
    await expect(page.locator('#fmScreen')).to_be_hidden()
    await open_files(page, fixture, 'plus')
    await expect(fm_file(page, FILE)).to_be_visible()
    require(fixture.calls('fsList')[-1]['data'] == {'path': DIRECTORY},
            'Plus Files did not reopen the selected project')


async def files_loading(page, fixture, expect):
    fixture.fs_gates[('fsList', DIRECTORY)] = asyncio.Event()
    await open_files(page, fixture, 'plus')
    await expect(page.locator('#fmList [role="status"]')).to_have_text('Loading…')
    await expect(fm_crumb(page, DIRECTORY)).to_be_visible()
    fixture.fs_gates[('fsList', DIRECTORY)].set()
    await expect(fm_file(page, FILE)).to_be_visible()
    fixture.fs_gates[('fsRead', FILE)] = asyncio.Event()
    await fm_file(page, FILE).click()
    await wait_calls(page, fixture, 'fsRead')
    await expect(page.locator('#fmView [role="status"]')).to_have_text('Loading…')
    await expect(page.locator('#fmText')).to_be_hidden()
    await expect(page.locator('#fmEdit')).to_be_disabled()
    await expect(page.locator('#fmDel')).to_be_disabled()
    fixture.fs_gates[('fsRead', FILE)].set()
    await expect(page.locator('#fmText')).to_be_visible()
    await expect(page.locator('#fmText')).to_have_value(AFTER)
    await expect(page.locator('#fmEdit')).to_be_enabled()


async def files_failure(page, fixture, expect):
    failure = 'Папка недоступна: ' + DIRECTORY + ' <denied>'
    fixture.fs_errors[('fsList', DIRECTORY)] = {'message': failure}
    await open_files(page, fixture)
    await expect(page.locator('#fmList [role="alert"]')).to_have_text('Files: ' + failure)
    await expect(fm_crumb(page, DIRECTORY)).to_be_visible()
    await expect(page.locator('#fmList')).not_to_contain_text('Empty')
    await expect(page.locator('#fmView')).to_be_hidden()
    del fixture.fs_errors[('fsList', DIRECTORY)]
    await page.locator('#fmList').get_by_role('button', name='Retry', exact=True).click()
    await expect(fm_file(page, FILE)).to_be_visible()
    require(len(fixture.calls('fsList', DIRECTORY)) == 2, 'Retry changed the failed directory')
    await fm_dir(page, 'nested <safe>').click()
    await expect(fm_file(page, NESTED_FILE)).to_be_visible()
    fixture.fs_errors[('fsList', DIRECTORY)] = failure
    await fm_crumb(page, DIRECTORY).click()
    await expect(page.locator('#fmList [role="alert"]')).to_have_text('Files: ' + failure)
    await expect(fm_file(page, NESTED_FILE)).to_have_count(0)
    del fixture.fs_errors[('fsList', DIRECTORY)]
    await page.locator('#fmList').get_by_role('button', name='Retry', exact=True).click()
    await expect(fm_file(page, FILE)).to_be_visible()


async def files_read_failure(page, fixture, expect):
    await open_files(page, fixture)
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value(AFTER)
    await page.locator('#fmBack').click()
    await fm_dir(page, 'nested <safe>').click()
    await expect(fm_file(page, NESTED_FILE)).to_be_visible()
    fixture.fs_errors[('fsRead', NESTED_FILE)] = 'Файл не найден <read denied>'
    await fm_file(page, NESTED_FILE).click()
    await expect(page.locator('#fmView [role="alert"]')).to_have_text('Read: Файл не найден <read denied>')
    await expect(page.locator('#fmViewName')).to_have_text('child.txt')
    await expect(page.locator('#fmText')).to_be_hidden()
    await expect(page.locator('#fmEdit')).to_be_disabled()
    await expect(page.locator('#fmDel')).to_be_disabled()
    del fixture.fs_errors[('fsRead', NESTED_FILE)]
    await page.locator('#fmView').get_by_role('button', name='Retry', exact=True).click()
    await expect(page.locator('#fmText')).to_have_value('CHILD_CONTENT\n')
    await expect(page.locator('#fmText')).to_be_visible()
    require(len(fixture.calls('fsRead', NESTED_FILE)) == 2, 'Read Retry used another file')
    await page.locator('#fmBack').click()
    await expect(fm_file(page, NESTED_FILE)).to_be_visible()


async def files_stale_navigation(page, fixture, expect):
    await open_files(page, fixture)
    await expect(fm_file(page, FILE)).to_be_visible()
    for stale_error in (False, True):
        gate = fixture.fs_gates[('fsList', NESTED)] = asyncio.Event()
        if stale_error:
            fixture.fs_errors[('fsList', NESTED)] = 'Late denied response'
        await fm_dir(page, 'nested <safe>').click()
        await wait_calls(page, fixture, 'fsList', NESTED, count=2 if stale_error else 1)
        # Real breadcrumb click while the first directory request is pending.
        await fm_crumb(page, DIRECTORY).click()
        await expect(fm_file(page, FILE)).to_be_visible()
        await fm_dir(page, 'later').click()
        await expect(fm_file(page, LATER_FILE)).to_be_visible()
        gate.set()
        for _ in range(80):
            if fixture.fs_completed.count(('fsList', NESTED)) >= (2 if stale_error else 1):
                break
            await page.wait_for_timeout(25)
        require(fixture.fs_completed.count(('fsList', NESTED)) >= (2 if stale_error else 1),
                'Delayed filesystem response was not delivered')
        await frames(page)
        await expect(fm_file(page, LATER_FILE)).to_be_visible()
        await expect(fm_crumb(page, LATER)).to_have_class('on')
        await expect(fm_file(page, NESTED_FILE)).to_have_count(0)
        await expect(page.locator('#fmList [role="alert"]')).to_have_count(0)
        await fm_file(page, LATER_FILE).click()
        await expect(page.locator('#fmText')).to_have_value('LATER_CONTENT\n')
        require(fixture.calls('fsRead')[-1]['data'] == {'path': LATER_FILE}, 'Stale response changed click path')
        await page.locator('#fmBack').click()
        await fm_crumb(page, DIRECTORY).click()
        await expect(fm_file(page, FILE)).to_be_visible()


async def files_saved_refresh(page, fixture, expect):
    await open_files(page, fixture, 'plus')
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value(AFTER)
    await page.locator('#fmEdit').click()
    saved = 'SAVED_FROM_REAL_EDITOR <safe>\n'
    await page.locator('#fmText').fill(saved)
    await page.locator('#fmEdit').click()
    await wait_calls(page, fixture, 'fsWrite', FILE)
    await expect(page.locator('#fmText')).to_have_attribute('readonly', '')
    require(fixture.calls('fsWrite')[-1]['data'] == {'path': FILE, 'content': saved}, 'Save sent wrong contents/path')
    await page.locator('#fmBack').click()
    await page.locator('#fmBack').click()
    await open_files(page, fixture)
    await expect(fm_file(page, FILE).locator('.s')).to_have_text(str(len(saved.encode('utf-8'))) + ' Б')
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value(saved)
    require(len(fixture.calls('fsRead', FILE)) == 2, 'Reopened file was not reread')
    await page.locator('#fmBack').click()
    await page.locator('#fmBack').click()
    # An agent/native write between openings must also be observed, not UI-cached.
    fixture.fs_contents[FILE] = 'UPDATED_BY_NATIVE_AGENT\n'
    added = DIRECTORY + '/test.txt'
    fixture.fs_contents[added] = 'AGENT_CREATED_TEST\n'
    await open_files(page, fixture, 'plus')
    await expect(fm_file(page, added)).to_be_visible()
    await fm_file(page, added).click()
    await expect(page.locator('#fmText')).to_have_value('AGENT_CREATED_TEST\n')
    await page.locator('#fmBack').click()
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value('UPDATED_BY_NATIVE_AGENT\n')


async def files_stale_session(page, fixture, expect):
    fixture.fs_errors[('fsList', STALE_DIRECTORY)] = 'Папка недоступна: ' + STALE_DIRECTORY
    await open_files(page, fixture)
    await expect(page.locator('#fmList [role="alert"]')).to_have_text('Files: Папка недоступна: ' + STALE_DIRECTORY)
    await expect(fm_crumb(page, STALE_DIRECTORY)).to_be_visible()
    await page.locator('#fmList').get_by_role('button', name='Retry', exact=True).click()
    await wait_calls(page, fixture, 'fsList', STALE_DIRECTORY, count=2)
    require(all(q['data'] == {'path': STALE_DIRECTORY} for q in fixture.calls('fsList')),
            'Stale session silently recovered into another workspace')
    require(not fixture.calls('fsRead'), 'Stale workspace read an unrelated file')


async def files_native_back(page, fixture, expect):
    await open_files(page, fixture)
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value(AFTER)
    # Actual app entry point used by native back/gestures, not a replaced UI function.
    require(await page.evaluate('window.__appBack()'), 'Native Back did not handle the Files reader')
    await expect(page.locator('#fmView')).to_be_hidden()
    await expect(fm_file(page, FILE)).to_be_visible()
    fixture.fs_gates[('fsRead', FILE)] = asyncio.Event()
    await fm_file(page, FILE).click()
    await wait_calls(page, fixture, 'fsRead', FILE, count=2)
    require(await page.evaluate('window.__appBack()'), 'Native Back did not cancel the pending reader')
    await expect(fm_file(page, FILE)).to_be_visible()
    fixture.fs_gates[('fsRead', FILE)].set()
    for _ in range(80):
        if fixture.fs_completed.count(('fsRead', FILE)) == 2:
            break
        await page.wait_for_timeout(25)
    require(fixture.fs_completed.count(('fsRead', FILE)) == 2, 'Delayed read was not delivered')
    await frames(page)
    await expect(page.locator('#fmView')).to_be_hidden()
    await expect(fm_file(page, FILE)).to_be_visible()
    require(await page.evaluate('window.__appBack()'), 'Native Back did not close Files')
    await expect(page.locator('#fmScreen')).to_be_hidden()


async def files_scope(page, fixture, expect):
    async def check_scope():
        leaked = await page.evaluate("['fileReset','crumbRender','readState','statusHTML','currentRequest','toastNative'].filter(name => name in window)")
        require(not leaked, 'Files helpers leaked onto window: ' + repr(leaked))
        require(await page.evaluate('typeof window.__kumoFilesScope') == 'function', 'Files scope probe missing')
        snapshot = await page.evaluate('window.__kumoFilesScope()')
        require(snapshot == {'textType': 'object', 'textIsNull': False, 'textMatches': True,
                             'textareaTag': 'TEXTAREA', 'editorConnected': True},
                'Files closure lost its editor: ' + repr(snapshot))
    await check_scope()
    await open_files(page, fixture)
    await fm_file(page, FILE).click()
    await expect(page.locator('#fmText')).to_have_value(AFTER)
    await check_scope()
    await page.locator('#fmBack').click()
    await expect(fm_file(page, FILE)).to_be_visible()
    await check_scope()


async def files_native_default(page, fixture, expect):
    # Empty selection is a UI boundary condition; let native fsList choose its own default.
    await page.evaluate("S.dir = ''")
    await open_files(page, fixture, 'plus')
    require(fixture.calls('fsList')[0]['data'] == {}, 'Empty directory must not send an Android fallback')
    await expect(fm_file(page, FILE)).to_be_visible()
    await expect(fm_crumb(page, DIRECTORY)).to_have_text('File Agent')
    require(not await page.evaluate('S.dir'), 'Files browser changed the session workspace')


CASES = {'files_browser': files_browser, 'files_loading': files_loading,
         'files_failure': files_failure, 'files_read_failure': files_read_failure,
         'files_stale_navigation': files_stale_navigation, 'files_saved_refresh': files_saved_refresh,
         'files_stale_session': files_stale_session, 'files_native_default': files_native_default,
         'files_native_back': files_native_back, 'files_scope': files_scope,
         'permission_diff': permission_diff, 'permission_fallback': permission_fallback,
         'permission_types': permission_types, 'permission_prepared': permission_prepared,
         'permission_bounded': permission_bounded, 'allow': allow, 'reject': reject, 'stop': stop,
         'history': history, 'restore_chronology': chronology, 'foreign_session': foreign_session}


async def run_case(browser, origin, name, output, expect):
    fixture, errors, external = NativeFixture(), [], []
    if name == 'files_stale_session':
        fixture.session_directory = STALE_DIRECTORY
    if name.startswith('permission_') or name in ('allow', 'reject', 'stop'):
        fixture.messages, fixture.status = snapshots(False), 'busy'
    context = await browser.new_context(viewport={'width': 820, 'height': 1180},
                                        service_workers='block', reduced_motion='reduce')
    result = {'name': name}
    try:
        page = await context.new_page()
        page.set_default_timeout(3000)
        page.set_default_navigation_timeout(10000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.expose_binding('__nativeFixture', fixture.request)
        await page.add_init_script('(' + BRIDGE + ')(' + json.dumps(fixture.session_directory) + ')')

        async def local_only(route):
            if route.request.url.startswith(origin + '/'):
                await route.continue_()
            else:
                external.append(route.request.url)
                await route.abort()

        await context.route('**/*', local_only)

        async def exercise():
            await page.goto(origin + '/_ui/index.html')
            await page.evaluate('S.bootP')
            await page.wait_for_function('S.live && S.sessions.length === 1')
            require(len(fixture.calls('backendSubscribe')) == 1, 'Native subscription did not boot once')
            await open_thread(page)
            await CASES[name](page, fixture, expect)
            await frames(page)
            require(not fixture.unexpected, 'Unexpected fixture requests: ' + repr(fixture.unexpected))
            require(not errors, 'Browser page errors: ' + repr(errors))
            require(not external, 'UI attempted external network: ' + repr(external))
            ui_errors = await page.evaluate('S.errors')
            require(not ui_errors, 'UI error log: ' + repr(ui_errors))

        try:
            await asyncio.wait_for(exercise(), 25)
            result['status'] = 'PASS'
        except Exception as error:
            # A failing assertion/browser timeout must not prevent independent cases.
            result.update(status='FAIL', error=str(error) or type(error).__name__)
        finally:
            if fixture.reply_gate:
                fixture.reply_gate.set()
            for gate in fixture.fs_gates.values():
                gate.set()
            result.update(requests=fixture.trace, unexpected=fixture.unexpected, pageErrors=errors, external=external)
            await page.screenshot(path=str(output / (name + '.png')), timeout=3000)
            if result['status'] == 'FAIL':
                await asyncio.to_thread((output / (name + '.html')).write_text,
                                        await page.content(), encoding='utf-8')
    finally:
        await context.close()
    print(result['status'] + ' ' + name + (': ' + result['error'] if 'error' in result else ''), flush=True)
    return result


async def run_browser(args, output, staged):
    # Loaded only after main() selects the existing Playwright interpreter.
    playwright = importlib.import_module('playwright.async_api')
    expect = playwright.expect
    expect.set_options(timeout=3000)
    report = {'scope': 'UI-only backend/ledger/filesystem fixture; no real model, Swift, Git, writes or WKWebView',
              'restore': 'syncSession snapshot restore, not file undo', 'cases': [],
              'stagedSHA256': hashlib.sha256(staged.read_bytes()).hexdigest()}
    with asset_server(staged) as (origin, requests):
        try:
            async with playwright.async_playwright() as pw:
                executable = args.browser or os.environ.get('PLAYWRIGHT_BROWSER_EXECUTABLE') or os.environ.get('CHROME_EXECUTABLE')
                if not executable and not Path(pw.chromium.executable_path).is_file():
                    chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
                    require(chrome.is_file(), 'No installed Chromium/Chrome; pass --browser (no downloads performed)')
                    executable = str(chrome)
                browser = await pw.chromium.launch(**({'executable_path': executable} if executable else {}))
                report['browser'] = browser.version
                try:
                    for name in args.case or CASES:
                        report['cases'].append(await run_case(browser, origin, name, output, expect))
                finally:
                    await browser.close()
            require(all(r['method'] == 'GET' and (r['path'].startswith('/_ui/') or r['path'] == '/favicon.ico')
                        for r in requests), 'Native backend incorrectly fell through to HTTP')
        finally:
            report['httpAssets'] = requests
            await asyncio.to_thread((output / 'report.json').write_text,
                                    json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return all(c['status'] == 'PASS' for c in report['cases'])


def main():
    # Reuse the dependency location documented by existing tests, never pip/npm install.
    if importlib.util.find_spec('playwright') is None:
        venv = LOCAL / 'api-test-venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        require(venv.is_file() and venv.resolve() != Path(sys.executable).resolve(),
                'Playwright unavailable; use the existing tests/requirements.txt environment')
        return subprocess.run([str(venv), '-B', str(Path(__file__).resolve()), *sys.argv[1:]],
                              check=False, timeout=330).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, action='append', help='Run specific cases (default: all)')
    parser.add_argument('--output', type=Path, help='Evidence directory; must be inside workspace .local')
    parser.add_argument('--browser', help='Existing Chromium/Chrome executable; never downloaded')
    args = parser.parse_args()
    LOCAL.mkdir(exist_ok=True)
    if args.output:
        output = args.output.resolve()
        require(output.is_relative_to(LOCAL.resolve()) and output != LOCAL.resolve(), '--output must be a child of .local')
        output.mkdir(parents=True, exist_ok=True)
    else:
        output = Path(tempfile.mkdtemp(prefix='native-file-agent-ui-', dir=LOCAL))
    print('Evidence: ' + str(output), flush=True)
    protected = [SOURCE, STAGER, Path(__file__).with_name('test-stage-native-ui.py'),
                 *RESOURCES.glob('*')]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected if p.is_file()}
    spec = importlib.util.spec_from_file_location('stage_native_file_ui', STAGER)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot import the canonical stage-native-ui.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    canonical = SOURCE.read_bytes().decode('utf-8').replace('\r\n', '\n')
    for label, old in (('permission', module.PERMISSION_OLD), ('file manager', module.FILE_MANAGER_OLD)):
        require(canonical.count(old) == 1, label + ' patch is not an exact canonical block')
        for changed in (canonical.replace(old, old + '\n' + old),
                        canonical.replace(old, old.replace('function ', 'function changed_', 1))):
            try:
                module.patch_html(changed)
            except module.PatchError:
                pass
            else:
                raise AssertionError(label + ' patch did not fail closed on changed/duplicate canonical block')
    surround = module.FILE_MANAGER_BEFORE + module.FILE_MANAGER_OLD + module.FILE_MANAGER_AFTER
    require(canonical.count(surround) == 1, 'Files surrounding canonical boundaries changed')
    for changed in (canonical.replace(module.FILE_MANAGER_BEFORE + module.FILE_MANAGER_OLD,
                                      module.FILE_MANAGER_BEFORE + '/* shifted start */\n' + module.FILE_MANAGER_OLD),
                    canonical.replace(module.FILE_MANAGER_OLD + module.FILE_MANAGER_AFTER,
                                      module.FILE_MANAGER_OLD + '\n/* shifted end */' + module.FILE_MANAGER_AFTER),
                    canonical + '\n' + surround):
        try:
            module.patch_html(changed)
        except module.PatchError:
            pass
        else:
            raise AssertionError('Files patch accepted changed/duplicate surrounding boundaries')
    require(module.FILE_MANAGER_NEW.startswith("/* ---------- file manager ---------- */\n;(function(){\n 'use strict';\n"),
            'Files must start a function-local strict scope')
    require(module.FILE_MANAGER_NEW.endswith('\n})();'), 'Files IIFE must end before the following app scope')
    staged_html = module.patch_html(canonical)
    require(staged_html.count(module.FILE_MANAGER_BEFORE + module.FILE_MANAGER_NEW + module.FILE_MANAGER_AFTER) == 1,
            'Files IIFE changed its surrounding boundaries')

    require((RESOURCES / 'codex.html').read_bytes() == staged_html.encode('utf-8'),
            'Generated iOS HTML is stale; run stage-native-ui.py first')
    print('PASS exact permission/file manager patches and scope boundaries fail closed; generated iOS HTML matches staging', flush=True)
    assets = output / 'assets'
    assets.mkdir(exist_ok=True)
    staged, _ = module.stage(SOURCE, assets)
    try:
        passed = asyncio.run(run_browser(args, output, staged))
    finally:
        require(all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == digest
                    for p, digest in before.items()), 'A parent-owned source/resource changed during the UI proof')
    print('PASS parent-owned sources/resources unchanged; static server and browser closed', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
