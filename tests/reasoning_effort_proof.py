"""Proof of reasoning effort handling and reasoning-only Thought with real OpenCode."""
import argparse
import asyncio
import base64
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.async_api import async_playwright, expect

from reasoning_activity_proof import expanded, launch_browser, thought
from work_log_proof import BODY, SUMMARY, assert_entries, assert_groups, open_work, work_id

ROOT = Path(__file__).resolve().parents[1]


def isolated_server_env(work_dir, config):
    # Config/auth mutations must never reach the developer's real OpenCode profile.
    env = {key: value for key, value in os.environ.items() if not key.startswith('OPENCODE_')}
    for key, folder in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                        ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache'),
                        ('OPENCODE_TEST_HOME', 'home'), ('OPENCODE_TEST_MANAGED_CONFIG_DIR', 'managed')]:
        path = work_dir / folder
        path.mkdir()
        env[key] = str(path)
    for name in ['AUTOUPDATE', 'DEFAULT_PLUGINS', 'MODELS_FETCH', 'LSP_DOWNLOAD',
                 'CLAUDE_CODE', 'EXTERNAL_SKILLS', 'PROJECT_CONFIG']:
        env['OPENCODE_DISABLE_' + name] = 'true'
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    return env


def assert_isolated_paths(paths, work_dir):
    for name in ['config', 'state', 'home']:
        assert Path(paths[name]).resolve().is_relative_to(work_dir.resolve()), f'Non-isolated {name}: {paths[name]}'


class FakeProviderServer:
    def __init__(self):
        self.received_calls = []
        self.sequence_calls = 0
        self.gates = {f'{i}_{stage}': threading.Event() for i in range(3) for stage in ['start', 'delta']}
        self.reached = set()
        self.fixture_path: str | None = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                fixture.received_calls.append({
                    'path': self.path,
                    'headers': dict(self.headers),
                    'body': body
                })
                is_title = 'title generator' in str(body)
                prompt_text = str(body)

                if '/chat/completions' in self.path:
                    def chunk(delta, reason=None):
                        return 'data: ' + json.dumps({
                            'id': 'chatcmpl_test',
                            'object': 'chat.completion.chunk',
                            'created': 0,
                            'model': body.get('model', 'test'),
                            'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]
                        }) + '\n\n'

                    if not is_title and 'contiguous_sse_fixture' in prompt_text:
                        # Flush genuine provider SSE frames, with deterministic browser checkpoints.
                        index = fixture.sequence_calls
                        fixture.sequence_calls += 1
                        if index >= 3:
                            self.send_error(500, 'Unexpected extra model round in sequence fixture')
                            return
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.close_connection = True

                        def write(frame):
                            self.wfile.write(frame.encode())
                            self.wfile.flush()

                        def checkpoint(stage):
                            key = f'{index}_{stage}'
                            fixture.reached.add(key)
                            if not fixture.gates[key].wait(30):
                                raise TimeoutError('Browser did not release provider checkpoint ' + key)

                        write(chunk({'reasoning_content': f'SSE reasoning round {index}.'}))
                        checkpoint('start')
                        write(chunk({'reasoning_content': f'\n\nCurrent round {index} delta.'}))
                        checkpoint('delta')
                        if index < 2:
                            write(chunk({'tool_calls': [{'index': 0, 'id': f'call_sequence_{index}', 'type': 'function',
                                'function': {'name': 'read', 'arguments': json.dumps({'filePath': fixture.fixture_path})}}]}))
                            write(chunk({}, 'tool_calls'))
                        else:
                            write(chunk({'content': '<think>Embedded SSE reasoning.</think>Sequence SSE answer.'}, 'stop'))
                        write('data: [DONE]\n\n')
                        return
                    if is_title:
                        payload = (chunk({'content': 'Title OK'}, 'stop') + 'data: [DONE]\n\n').encode()
                    elif 'think_tags' in prompt_text:
                        # Model output with <think>...</think> tags inside content
                        payload = (chunk({'content': '<think>\nAnalyzing the prompt carefully.\nGenerating optimal code.\n</think>\nHere is the answer with think tags.'}, 'stop') +
                                   'data: [DONE]\n\n').encode()
                    elif 'no_thinking' in prompt_text:
                        # Model output WITHOUT any thinking
                        payload = (chunk({'content': 'Direct answer without any thinking.'}, 'stop') +
                                   'data: [DONE]\n\n').encode()
                    else:
                        # Native reasoning chunk
                        payload = (chunk({'reasoning_content': 'Thinking about reasoning effort in chat completion...'}) +
                                   chunk({'content': 'Chat completion answer received.'}, 'stop') +
                                   'data: [DONE]\n\n').encode()
                else:
                    # Responses API
                    if is_title:
                        events = [
                            {'type': 'response.created', 'response': {'id': 'resp_0', 'created_at': int(time.time()), 'model': 'test'}},
                            {'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': 'title_msg', 'type': 'message'}},
                            {'type': 'response.output_text.delta', 'item_id': 'title_msg', 'output_index': 0, 'content_index': 0, 'delta': 'Title OK'},
                            {'type': 'response.output_text.done', 'item_id': 'title_msg', 'output_index': 0, 'content_index': 0, 'text': 'Title OK'},
                            {'type': 'response.output_item.done', 'output_index': 0, 'item': {'id': 'title_msg', 'type': 'message'}},
                            {'type': 'response.completed', 'response': {'id': 'resp_0', 'model': 'test', 'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}}
                        ]
                    elif 'no_thinking' in prompt_text:
                        # Responses without any reasoning text (empty reasoning part placeholder)
                        events = [
                            {'type': 'response.created', 'response': {'id': 'resp_1', 'created_at': int(time.time()), 'model': 'test'}},
                            {'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': 'rs_empty', 'type': 'reasoning'}},
                            {'type': 'response.output_item.done', 'output_index': 0, 'item': {'id': 'rs_empty', 'type': 'reasoning'}},
                            {'type': 'response.output_item.added', 'output_index': 1, 'item': {'id': 'msg_empty', 'type': 'message'}},
                            {'type': 'response.output_text.delta', 'item_id': 'msg_empty', 'output_index': 1, 'content_index': 0, 'delta': 'Direct responses answer.'},
                            {'type': 'response.output_text.done', 'item_id': 'msg_empty', 'output_index': 1, 'content_index': 0, 'text': 'Direct responses answer.'},
                            {'type': 'response.output_item.done', 'output_index': 1, 'item': {'id': 'msg_empty', 'type': 'message'}},
                            {'type': 'response.completed', 'response': {'id': 'resp_1', 'model': 'test', 'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}}
                        ]
                    else:
                        text = 'Responses answer received.'
                        events = [
                            {'type': 'response.created', 'response': {'id': 'resp_1', 'created_at': int(time.time()), 'model': 'test'}},
                            {'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': 'rs_1', 'type': 'reasoning'}},
                            {'type': 'response.reasoning_summary_part.added', 'output_index': 0, 'summary_index': 0},
                            {'type': 'response.reasoning_summary_text.delta', 'output_index': 0, 'summary_index': 0, 'delta': 'Thinking about reasoning effort in responses...'},
                            {'type': 'response.reasoning_summary_text.done', 'output_index': 0, 'summary_index': 0, 'text': 'Thinking about reasoning effort in responses...'},
                            {'type': 'response.output_item.done', 'output_index': 0, 'item': {'id': 'rs_1', 'type': 'reasoning'}},
                            {'type': 'response.output_item.added', 'output_index': 1, 'item': {'id': 'msg_1', 'type': 'message'}},
                            {'type': 'response.output_text.delta', 'item_id': 'msg_1', 'output_index': 1, 'content_index': 0, 'delta': text},
                            {'type': 'response.output_text.done', 'item_id': 'msg_1', 'output_index': 1, 'content_index': 0, 'text': text},
                            {'type': 'response.output_item.done', 'output_index': 1, 'item': {'id': 'msg_1', 'type': 'message'}},
                            {'type': 'response.completed', 'response': {'id': 'resp_1', 'model': 'test', 'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}}
                        ]
                    payload = (''.join('data: ' + json.dumps(ev) + '\n\n' for ev in events) + 'data: [DONE]\n\n').encode()

                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/v1'

    def close(self):
        for gate in self.gates.values():
            gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


async def real_sequence_sse(page, provider, evidence_dir):
    # Observe Chromium's real EventSource frames, not a mocked onEvent/EventSource.
    frames, snapshots, rendered = [], [], {}

    async def record_sequence(target, phase):
        rendered[phase] = {
            'main': await target.locator('#main > .msg.ai, ' + SUMMARY).evaluate_all(
                'els=>els.map(e=>e.dataset.workOpen||"text:"+e.dataset.raw)'),
            'details': await target.locator(BODY + ' .think, ' + BODY + ' .activity').evaluate_all('''els=>els.map(e=>({
              key:e.dataset.part,head:e.querySelector('.think-head')?.textContent,
              expanded:e.querySelector('.think-head')?.getAttribute('aria-expanded'),spinning:e.matches('.think[data-running]')}))'''),
        }

    cdp = await page.context.new_cdp_session(page)
    await cdp.send('Network.enable')
    cdp.on('Network.eventSourceMessageReceived', lambda event: frames.append(json.loads(event['data'])))

    async def capture_snapshot(response):
        if '/message?' in response.url and response.request.method == 'GET':
            snapshots.append({'at': time.time(), 'url': response.url, 'messages': await response.json()})

    page.on('response', capture_snapshot)
    session = await page.evaluate("async()=>api('/session',{method:'POST',body:{title:'Contiguous SSE proof'},directory:S.dir})")
    await page.evaluate('sid=>openSession(sid)', session['id'])
    await page.wait_for_function('S.live && !S.running')
    await page.fill('#inp', 'contiguous_sse_fixture: read the fixture twice, then answer.')
    async with page.expect_response(lambda response: '/prompt_async' in response.url) as sent:
        await page.click('#send')
    assert (await sent.value).status == 204
    keys, previous, wid = [], None, None
    try:
        for index in range(3):
            await page.wait_for_function('n=>[...S.msgs.values()].filter(m=>m.info.role==="assistant" && [...m.parts.values()].some(p=>p.type==="reasoning" && p.text)).length>=n', arg=index + 1, timeout=30000)
            messages = await page.evaluate('[...S.msgs.values()].filter(m=>m.info.role==="assistant").map(m=>({info:m.info,parts:[...m.parts.values()]}))')
            current_message = messages[-1]
            current_part = next(p for p in current_message['parts'] if p['type'] == 'reasoning' and p.get('text'))
            key = f'{current_message["info"]["id"]}_{current_part["id"]}_think'
            keys.append(key)
            if wid is None:
                wid = work_id(current_message['info']['id'], current_part['id'])
            await assert_groups(page, {wid: index * 2 + 1})
            if index == 0:
                await open_work(page, wid)
            selector = BODY + ' ' + thought(current_message['info']['id'], current_part['id'])
            await expect(page.locator(BODY + ' .think')).to_have_count(index + 1)
            await expanded(page, selector, True)
            await expect(page.locator(selector + '[data-running]')).to_have_count(1)
            await expect(page.locator(BODY + ' .think[data-running]')).to_have_count(1)
            assert await page.evaluate('S.running')
            if index == 0:
                # The real polling path must not replace an already received SSE delta
                # with the server's unfinished, still-empty persisted reasoning part.
                live_text = await page.locator(selector + ' .rtext').all_text_contents()
                await page.evaluate('syncSession()')
                await expect(page.locator(selector + ' .rtext')).to_have_text(live_text)
                await expanded(page, selector, True)
                await expect(page.locator(selector + '[data-running]')).to_have_count(1)
            if previous:
                await expanded(page, previous, True)  # Explicit open survives the next model call.
                await expect(page.locator(previous + '[data-running]')).to_have_count(0)
            old_texts = await page.locator(BODY + ' .think').evaluate_all('els=>els.slice(0,-1).map(e=>e.textContent)')
            assert f'{index}_start' in provider.reached
            provider.gates[f'{index}_start'].set()
            await expect(page.locator(selector + ' .rtext')).to_have_text(
                f'SSE reasoning round {index}.\n\nCurrent round {index} delta.')
            assert await page.locator(BODY + ' .think').evaluate_all('els=>els.slice(0,-1).map(e=>e.textContent)') == old_texts
            # Explicitly mark this live block open so completion does not collapse it.
            await page.locator(selector + ' > .think-head').click()
            await page.locator(selector + ' > .think-head').click()
            previous = selector
            provider.gates[f'{index}_delta'].set()
        await page.wait_for_function('!S.running && [...document.querySelectorAll(".msg.ai")].some(e=>e.textContent.includes("Sequence SSE answer."))', timeout=30000)
        messages = await page.evaluate('[...S.msgs.values()].filter(m=>m.info.role==="assistant").map(m=>({info:m.info,parts:[...m.parts.values()]}))')
        assert len(messages) == 3, messages
        parents = {m['info']['parentID'] for m in messages}
        assert len(parents) == 1, f'Fixture must exercise multiple messages with the same parent: {parents}'
        assert provider.sequence_calls == 3
        expected, native = [], []
        for index, m in enumerate(messages):
            part = next(p for p in m['parts'] if p['type'] == 'reasoning' and p.get('text'))
            selector = BODY + ' ' + thought(m['info']['id'], part['id'])
            native.append(selector)
            expected.append(keys[index])
            assert part['time'].get('end') is not None, f'Real server omitted completed reasoning timing: {part}'
            duration = max(1, int((part['time']['end'] - part['time']['start']) / 1000 + 0.5))
            await expect(page.locator(selector + ' .think-duration')).to_have_text(f'{duration}s')
            tools = [p for p in m['parts'] if p['type'] == 'tool']
            if index < 2:
                assert len(tools) == 1, tools
                assert tools[0]['state']['status'] == 'completed', tools[0]
                expected.append(f'{m["info"]["id"]}_{tools[0]["id"]}_activity')
                await expect(page.locator(BODY + f' .tool-event[data-tool-part="{tools[0]["id"]}"]')).to_contain_text('SSE_READ_FIXTURE')
            else:
                tagged = next(p for p in m['parts'] if p['type'] == 'text' and '<think>' in p.get('text', ''))
                expected.append(f'{m["info"]["id"]}_{tagged["id"]}_think')
        timeline = ['user:' + next(iter(parents)), wid, 'text:Sequence SSE answer.']
        await assert_groups(page, {wid: len(expected)}, timeline)
        await assert_entries(page, expected)
        await record_sequence(page, 'live_completed')
        await expect(page.locator(BODY + ' .think[data-running]')).to_have_count(0)
        for selector in native:
            await expanded(page, selector, True)
        event_types = {frame.get('type') for frame in frames if frame.get('properties', {}).get('sessionID') == session['id']}
        assert 'message.part.delta' in event_types and 'session.status' in event_types, event_types
        await page.screenshot(path=str(evidence_dir / 'real-sequence-sse.png'))
        # Both reload and a new tab must rebuild keys/order/durations from GET /message.
        url = page.url
        expected_heads = await page.locator(BODY + ' .think-head').all_text_contents()
        await page.reload()
        await page.wait_for_function('S.live && S.spec', timeout=30000)
        await page.evaluate('sid=>openSession(sid)', session['id'])
        await page.wait_for_function('S.msgs.size>=4 && !S.running', timeout=30000)
        await assert_groups(page, {wid: len(expected)}, timeline)
        await open_work(page, wid)
        await assert_entries(page, expected)
        assert await page.locator(BODY + ' .think-head').all_text_contents() == expected_heads
        for selector in native:
            await expanded(page, selector, False)
        await record_sequence(page, 'reload')
        fresh = await page.context.new_page()
        try:
            # HTML routing is page-scoped on the original page.
            await fresh.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'), content_type='text/html; charset=utf-8'))
            await fresh.goto(url)
            await fresh.wait_for_function('S.live && S.spec', timeout=30000)
            await fresh.evaluate('sid=>openSession(sid)', session['id'])
            await fresh.wait_for_function('S.msgs.size>=4 && !S.running', timeout=30000)
            await assert_groups(fresh, {wid: len(expected)}, timeline)
            await open_work(fresh, wid)
            await assert_entries(fresh, expected)
            assert await fresh.locator(BODY + ' .think-head').all_text_contents() == expected_heads
            await expect(fresh.locator(BODY + ' .think[data-running]')).to_have_count(0)
            await expect(fresh.locator(BODY + ' .think-head[aria-expanded="true"]')).to_have_count(0)
            assert provider.sequence_calls == 3, 'Viewing/reloading Work initiated an extra model round'
            await record_sequence(fresh, 'fresh_page')
        finally:
            await fresh.close()
        (evidence_dir / 'sequence-sse-events.json').write_text(json.dumps(frames, indent=2), encoding='utf-8')
    except Exception:
        state = await page.evaluate('({sid:S.sid,running:S.running,errors:S.errors,messages:[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]})),html:document.querySelector("#main").innerHTML,workHtml:document.querySelector("#workLogDialog")?.innerHTML})')
        (evidence_dir / 'sequence-failure.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        await page.screenshot(path=str(evidence_dir / 'sequence-failure.png'))
        raise
    finally:
        (evidence_dir / 'sequence-sse-events.json').write_text(json.dumps(frames, indent=2), encoding='utf-8')
        (evidence_dir / 'sequence-api-snapshots.json').write_text(json.dumps(snapshots, indent=2), encoding='utf-8')
        (evidence_dir / 'sequence-rendered.json').write_text(json.dumps(rendered, indent=2), encoding='utf-8')
        page.remove_listener('response', capture_snapshot)
        for gate in provider.gates.values():
            gate.set()
        await cdp.detach()


async def main(sequence_only=False, browser_executable=None):
    fake_server = FakeProviderServer()
    work_dir = Path(tempfile.mkdtemp(prefix='reasoning-effort-proof-'))
    test_dir = work_dir / 'project'
    test_dir.mkdir()
    evidence_dir = work_dir / 'evidence'
    evidence_dir.mkdir()
    fixture_path = test_dir / 'sequence-fixture.txt'
    fixture_path.write_text('SSE_READ_FIXTURE\n', encoding='utf-8')
    fake_server.fixture_path = str(fixture_path)
    print(f'Evidence: {evidence_dir}', flush=True)

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        oc_port = s.getsockname()[1]

    # Initial config: models have NO variants declared
    initial_config = {
        'provider': {
            'fake-chat': {
                'name': 'Fake Chat Provider',
                'npm': '@ai-sdk/openai-compatible',
                'options': {'baseURL': fake_server.url, 'apiKey': 'test-fake-key'},
                'models': {
                    'chat-model': {
                        'name': 'Chat Model No Variants',
                        'limit': {'context': 32000, 'output': 8000},
                        'modalities': {'input': ['text'], 'output': ['text']}
                    }
                }
            },
            'fake-resp': {
                'name': 'Fake Responses Provider',
                'npm': '@ai-sdk/openai',
                'options': {'baseURL': fake_server.url, 'apiKey': 'test-fake-key'},
                'models': {
                    'resp-model': {
                        'name': 'Responses Model No Variants',
                        'limit': {'context': 32000, 'output': 8000},
                        'modalities': {'input': ['text'], 'output': ['text']}
                    }
                }
            },
            'fake-xhigh': {
                'name': 'Fake XHigh Provider',
                'npm': '@ai-sdk/openai-compatible',
                'options': {'baseURL': fake_server.url, 'apiKey': 'test-fake-key'},
                'models': {
                    'deepseek-v4-flash': {
                        'name': 'DeepSeek V4 Flash Model',
                        'limit': {'context': 32000, 'output': 8000},
                        'modalities': {'input': ['text'], 'output': ['text']}
                    }
                }
            },
            'fake-preserved': {
                'name': 'Fake Preserved Provider',
                'npm': '@ai-sdk/openai-compatible',
                'options': {'baseURL': fake_server.url, 'apiKey': 'test-fake-key'},
                'models': {
                    'custom-variants-model': {
                        'name': 'Custom Variants Model',
                        'limit': {'context': 32000, 'output': 8000},
                        'modalities': {'input': ['text'], 'output': ['text']},
                        'variants': {
                            'high': {'reasoningEffort': 'high'},
                            'max': {'reasoningEffort': 'max'}
                        }
                    }
                }
            }
        }
    }

    initial_config['enabled_providers'] = list(initial_config['provider'])
    initial_config['autoupdate'] = False
    env = isolated_server_env(work_dir, initial_config)

    proc = subprocess.Popen(
        ['D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe', 'serve', '--hostname', '127.0.0.1', '--port', str(oc_port)],
        env=env, cwd=test_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    origin = f'http://127.0.0.1:{oc_port}'
    results = {'checks': []}

    try:
        import urllib.request
        for _ in range(80):
            try:
                with urllib.request.urlopen(origin + '/global/health', timeout=1) as r:
                    if json.load(r).get('healthy'):
                        break
            except Exception:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError('Isolated OpenCode server did not become healthy')
        with urllib.request.urlopen(origin + '/path', timeout=10) as response:
            assert_isolated_paths(json.load(response), work_dir)
        print('PASS isolated OpenCode config/state/home', flush=True)

        # Set auth keys for providers
        for p_id in ['fake-chat', 'fake-resp', 'fake-xhigh', 'fake-preserved']:
            req = urllib.request.Request(
                origin + f'/auth/{p_id}',
                data=json.dumps({'type': 'api', 'key': 'test-key'}).encode(),
                headers={'Content-Type': 'application/json'},
                method='PUT'
            )
            with urllib.request.urlopen(req, timeout=10):
                pass

        encoded = base64.urlsafe_b64encode(str(test_dir).encode()).decode().rstrip('=')

        async with async_playwright() as pw:
            browser = await launch_browser(pw, browser_executable)
            context = await browser.new_context(viewport={'width': 424, 'height': 900})
            page = await context.new_page()
            page.set_default_timeout(5000)
            await page.context.grant_permissions(['local-network-access'], origin=origin)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            try:
                await page.wait_for_function('S.spec && S.live && S.provs.length >= 4', timeout=30000)
            except Exception:
                state = await page.evaluate('({spec:!!S.spec,live:S.live,providers:S.provs,errors:S.errors,specErr:S.specErr,health:S.health})')
                state['pageErrors'] = errors
                (evidence_dir / 'boot-failure.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
                await page.screenshot(path=str(evidence_dir / 'boot-failure.png'))
                raise
            if sequence_only:
                await page.click('#cModel')
                models = await page.evaluate('allModels()')
                chat_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-chat' and m['m'] == 'chat-model')
                await page.locator('#sheet .opt').nth(chat_idx).click()
                await real_sequence_sse(page, fake_server, evidence_dir)
                assert not errors, errors
                await browser.close()
                print('PASS strict_multi_message_same_parent_sse_and_reload', flush=True)
                return

            # -------------------------------------------------------------
            # Check 1: Chat Completions reasoning_effort & Thinking card with chevron
            # -------------------------------------------------------------
            print('--- Check 1: Chat Completions reasoning_effort & Thinking card ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            chat_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-chat' and m['m'] == 'chat-model')
            await page.locator('#sheet .opt').nth(chat_idx).click()

            # Reasoning chip must NOT be muted
            is_muted = await page.evaluate('document.querySelector("#cEff").classList.contains("muted")')
            assert not is_muted, 'Reasoning chip should not be muted'

            # Click Reasoning chip -> options sheet
            await page.evaluate("document.querySelector('#cEff').click()")
            options_text = await page.locator('#sheet .opt').all_inner_texts()
            assert 'Default' in options_text
            assert 'None' in options_text
            assert 'High' in options_text

            # Select 'High' (effort='high')
            high_btn = page.locator('#sheet .opt', has_text='High')
            await high_btn.click()

            chip_text_after = await page.evaluate('document.querySelector("#cEff span").textContent')
            assert chip_text_after == 'High', f'Expected "High", got "{chip_text_after}"'

            # Send message
            fake_server.received_calls.clear()
            await page.fill('#inp', 'Explain how reasoning effort works.')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            post_body = resp.request.post_data_json
            assert post_body.get('variant') == 'high'

            # Wait for response in feed
            await page.wait_for_function('!S.running && document.querySelector(".msg.ai")', timeout=30000)

            # Check what reached the fake provider at /v1/chat/completions
            chat_calls = [c for c in fake_server.received_calls if '/chat/completions' in c['path'] and 'title generator' not in str(c['body'])]
            assert len(chat_calls) >= 1
            actual_effort = chat_calls[-1]['body'].get('reasoning_effort')
            print(f'Provider received at /v1/chat/completions: reasoning_effort = {actual_effort}')
            assert actual_effort == 'high'

            # Verify the reasoning-only live feed row and its separate label/duration.
            thinking_cards = await page.locator('.think').count()
            assert thinking_cards == 1, 'This response has exactly one contiguous reasoning segment'
            native = await page.evaluate('[...S.msgs.values()].filter(m=>m.info.role==="assistant").map(m=>({id:m.info.id,part:[...m.parts.values()].find(p=>p.type==="reasoning" && p.text)?.id})).find(m=>m.part)')
            await expect(page.locator('.think')).to_have_attribute('data-part', f'{native["id"]}_{native["part"]}_think')
            caret_count = await page.locator('.think-head .caret').count()
            assert caret_count == 1, 'Thought must have an expandable chevron'
            assert await page.locator('.think-indicator, .reason-step, .think-steps, .reasoning-details').count() == 0
            await expect(page.locator('.think-label')).to_have_text('Reasoning')
            await expect(page.locator('.think-duration')).to_have_count(1)
            assert await page.locator('.think-head .think-icon, .think .tool-event, .think .topic').count() == 0
            assert await page.locator('.think-head').get_attribute('aria-controls')
            assert await page.locator('.think-head').get_attribute('aria-expanded') == 'false'
            await page.locator('.think-head').click()
            assert await page.locator('.think-head').get_attribute('aria-expanded') == 'true'
            thinking_content = await page.locator('.think .feed-text-window > .rtext').text_content()
            assert thinking_content == 'Thinking about reasoning effort in chat completion...'
            await page.locator('.think-head').click()
            results['checks'].append('chat_completions_reasoning_effort_high')

            # -------------------------------------------------------------
            # Check 2: No empty thinking card when model outputs no thinking
            # -------------------------------------------------------------
            print('--- Check 2: No fake thinking card on plain answer ---')
            fake_server.received_calls.clear()
            await page.fill('#inp', 'Say hello no_thinking')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            await page.wait_for_function('!S.running', timeout=30000)
            # The second assistant turn should have NO thinking card
            ai_msgs = await page.locator('.msg.ai').count()
            assert ai_msgs >= 2
            # Total thinking cards should still be 1 (from first turn only!)
            assert await page.locator('.think').count() == 1, 'Plain response should NOT create an empty Thought'
            results['checks'].append('no_empty_thinking_card_on_plain_response')

            # -------------------------------------------------------------
            # Check 3: Tag-based thinking <think>...</think>
            # -------------------------------------------------------------
            print('--- Check 3: Extract <think>...</think> tags into Thinking card ---')
            fake_server.received_calls.clear()
            await page.fill('#inp', 'Solve with think_tags')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            await page.wait_for_function('!S.running', timeout=30000)

            # Now thinking cards should be 2 (turn 1 + turn 3)
            assert await page.locator('.think').count() == 2
            latest_think = await page.locator('.think .rtext').nth(1).text_content()
            print(f'Extracted think tags content: "{latest_think}"')
            assert 'Analyzing the prompt carefully.' in latest_think
            # The visible message should NOT contain raw <think> tags
            latest_msg = await page.locator('.msg.ai').last.text_content()
            assert '<think>' not in latest_msg
            assert 'Here is the answer with think tags.' in latest_msg
            results['checks'].append('think_tags_extracted_cleanly')

            # -------------------------------------------------------------
            # Check 4: Responses model & separate per-model persistence
            # -------------------------------------------------------------
            print('--- Check 4: Responses model & separate per-model persistence ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            resp_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-resp' and m['m'] == 'resp-model')
            await page.locator('#sheet .opt').nth(resp_idx).click()

            # Separate storage check: resp-model should still have 'Reasoning' (Default)
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Reasoning'

            await page.evaluate("document.querySelector('#cEff').click()")
            low_btn = page.locator('#sheet .opt', has_text='Low')
            await low_btn.click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Low'

            # Switch back to chat-model: should still be 'High'
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            chat_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-chat' and m['m'] == 'chat-model')
            await page.locator('#sheet .opt').nth(chat_idx).click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'High'

            # Switch back to resp-model: should be 'Low'
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            resp_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-resp' and m['m'] == 'resp-model')
            await page.locator('#sheet .opt').nth(resp_idx).click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Low'

            # Send message with Responses
            fake_server.received_calls.clear()
            await page.fill('#inp', 'Explain reasoning in Responses API.')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            assert resp.request.post_data_json.get('variant') == 'low'

            await page.wait_for_function('!S.running', timeout=30000)
            resp_calls = [c for c in fake_server.received_calls if '/responses' in c['path'] and 'title generator' not in str(c['body'])]
            assert len(resp_calls) >= 1
            reasoning_obj = resp_calls[-1]['body'].get('reasoning')
            actual_effort = reasoning_obj.get('effort') if isinstance(reasoning_obj, dict) else None
            print(f'Provider received at /v1/responses: reasoning.effort = {actual_effort}')
            assert actual_effort == 'low'
            results['checks'].append('responses_reasoning_effort_low')

            # -------------------------------------------------------------
            # Check 5: xhigh option when model supports xhigh
            # -------------------------------------------------------------
            print('--- Check 5: xhigh support ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            xhigh_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-xhigh' and m['m'] == 'deepseek-v4-flash')
            await page.locator('#sheet .opt').nth(xhigh_idx).click()

            await page.evaluate("document.querySelector('#cEff').click()")
            xhigh_options = await page.locator('#sheet .opt').all_inner_texts()
            assert any('Very high' in opt for opt in xhigh_options)
            await page.locator('#sheet .opt', has_text='Very high').click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Very high'

            fake_server.received_calls.clear()
            await page.fill('#inp', 'Test xhigh prompt.')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            assert (await sent.value).request.post_data_json.get('variant') == 'xhigh'

            await page.wait_for_function('!S.running', timeout=30000)
            xhigh_calls = [c for c in fake_server.received_calls if 'title generator' not in str(c['body'])]
            assert len(xhigh_calls) >= 1
            assert xhigh_calls[-1]['body'].get('reasoning_effort') == 'xhigh'
            results['checks'].append('xhigh_supported_and_sent')

            # -------------------------------------------------------------
            # Check 6: Preserved custom variants & Caret toggle screenshot
            # -------------------------------------------------------------
            print('--- Check 6: Preserved variants & Caret screenshot ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            pres_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-preserved' and m['m'] == 'custom-variants-model')
            await page.locator('#sheet .opt').nth(pres_idx).click()

            await page.evaluate("document.querySelector('#cEff').click()")
            pres_options = await page.locator('#sheet .opt').all_inner_texts()
            assert 'Default' in pres_options
            assert any('High' in opt for opt in pres_options)
            assert any('Maximum' in opt or 'Max' in opt for opt in pres_options)
            await page.evaluate('closeAll()')
            results['checks'].append('existing_variants_preserved')

            # Expand thinking card and take proof screenshot
            thinking_detail = page.locator('.think').first
            await thinking_detail.locator('.think-head').click()
            assert await thinking_detail.locator('.think-head').get_attribute('aria-expanded') == 'true'
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(evidence_dir / 'reasoning-thinking-proof.png'))
            results['checks'].append('thinking_rendered_with_arrow_and_expandable')

            # Real provider -> OpenCode -> browser SSE, with two actual read tool rounds.
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            chat_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-chat' and m['m'] == 'chat-model')
            await page.locator('#sheet .opt').nth(chat_idx).click()
            await real_sequence_sse(page, fake_server, evidence_dir)
            results['checks'].append('strict_multi_message_same_parent_sse_and_reload')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        fake_server.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sequence-only', action='store_true', help='Run only the real multi-message SSE regression')
    parser.add_argument('--browser-executable', type=Path, help='Existing Chrome/Chromium (or PLAYWRIGHT_BROWSER_EXECUTABLE)')
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(main(sequence_only=args.sequence_only, browser_executable=args.browser_executable), timeout=150))
