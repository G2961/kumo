"""Proof of reasoning effort handling via real OpenCode and fake OpenAI-compatible / Responses server."""
import asyncio
import base64
import json
import os
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
LOGS = []

class FakeProviderServer:
    def __init__(self):
        self.received_calls = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
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

                if '/chat/completions' in self.path:
                    def chunk(delta, reason=None):
                        return 'data: ' + json.dumps({
                            'id': 'chatcmpl_test',
                            'object': 'chat.completion.chunk',
                            'created': 0,
                            'model': body.get('model', 'test'),
                            'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]
                        }) + '\n\n'

                    if is_title:
                        payload = (chunk({'content': 'Title OK'}, 'stop') + 'data: [DONE]\n\n').encode()
                    else:
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
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


async def main():
    fake_server = FakeProviderServer()
    test_dir = ROOT / 'tests' / 'temp_reasoning_test'
    test_dir.mkdir(exist_ok=True)
    evidence_dir = ROOT / 'tests' / 'evidence'
    evidence_dir.mkdir(exist_ok=True)

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

    env = dict(os.environ)
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(initial_config)

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

        # Ensure auth keys for providers
        for p_id in ['fake-chat', 'fake-resp', 'fake-xhigh', 'fake-preserved']:
            req = urllib.request.Request(
                origin + f'/auth/{p_id}',
                data=json.dumps({'type': 'api', 'key': 'test-key'}).encode(),
                headers={'Content-Type': 'application/json'},
                method='PUT'
            )
            urllib.request.urlopen(req)

        encoded = base64.urlsafe_b64encode(str(test_dir).encode()).decode().rstrip('=')

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={'width': 424, 'height': 900})
            await page.context.grant_permissions(['local-network-access'], origin=origin)
            errors = []
            page.on('pageerror', lambda e: errors.append(type(e).__name__))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            await page.wait_for_function('S.spec && S.live && S.provs.length >= 4')

            # -------------------------------------------------------------
            # Check 1: Chat Completions model without pre-declared variants
            # -------------------------------------------------------------
            print('--- Check 1: Chat Completions reasoning_effort ---')
            # Select fake-chat / chat-model
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            chat_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-chat' and m['m'] == 'chat-model')
            await page.locator('#sheet .opt').nth(chat_idx).click()

            # Reasoning chip must NOT be muted
            is_muted = await page.evaluate('document.querySelector("#cEff").classList.contains("muted")')
            assert not is_muted, 'Reasoning chip should not be muted'
            chip_text = await page.evaluate('document.querySelector("#cEff span").textContent')
            assert chip_text == 'Reasoning', f'Expected "Reasoning", got "{chip_text}"'

            # Click Reasoning chip -> options sheet
            await page.click('#cEff')
            options_text = await page.locator('#sheet .opt').all_inner_texts()
            print('Chat model options:', options_text)
            assert 'Default' in options_text
            assert 'None' in options_text
            assert 'High' in options_text

            # Select 'High' (effort='high')
            high_btn = page.locator('#sheet .opt', has_text='High')
            await high_btn.click()

            chip_text_after = await page.evaluate('document.querySelector("#cEff span").textContent')
            assert chip_text_after == 'High', f'Expected "High", got "{chip_text_after}"'
            effort_stored = await page.evaluate('S.efforts[modelKey()]')
            assert effort_stored == 'high', f'Expected "high", got "{effort_stored}"'

            # Send a message
            fake_server.received_calls.clear()
            await page.fill('#inp', 'Explain how reasoning effort works.')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            post_body = resp.request.post_data_json
            assert post_body.get('variant') == 'high', f'Expected prompt_async variant=high, got {post_body.get("variant")}'

            # Wait for response in feed
            await page.wait_for_function('!S.running && document.querySelector(".msg.ai")', timeout=30000)

            # Check what reached the fake provider at /v1/chat/completions
            chat_calls = [c for c in fake_server.received_calls if '/chat/completions' in c['path'] and 'title generator' not in str(c['body'])]
            assert len(chat_calls) >= 1, 'No prompt call reached /v1/chat/completions'
            actual_effort = chat_calls[-1]['body'].get('reasoning_effort')
            print(f'Provider received at /v1/chat/completions: reasoning_effort = {actual_effort}')
            assert actual_effort == 'high', f'Expected reasoning_effort="high", got {actual_effort}'

            # Verify Thinking card is displayed in feed
            print('S.msgs:', await page.evaluate('[...S.msgs.values()].map(m => ({role: m.info.role, parts: [...m.parts.values()]}))'))
            print('Thinking card html:', await page.locator('.thinking-card').all_inner_texts())
            print('Thinking card outerHTML:', await page.locator('.thinking-card').first.evaluate('el => el.outerHTML'))
            thinking_cards = await page.locator('.thinking-card').count()
            assert thinking_cards >= 1, 'Thinking card should be in feed'
            thinking_content = await page.locator('.thinking-card pre').text_content()
            print(f'Thinking content in feed: "{thinking_content}"')
            assert 'Thinking about reasoning effort in chat completion' in thinking_content
            results['checks'].append('chat_completions_reasoning_effort_high')

            # -------------------------------------------------------------
            # Check 2: Responses model without pre-declared variants
            # -------------------------------------------------------------
            print('--- Check 2: Responses reasoning.effort ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            resp_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-resp' and m['m'] == 'resp-model')
            await page.locator('#sheet .opt').nth(resp_idx).click()

            # Separate storage check: resp-model should still have 'Reasoning' (Default)
            chip_text = await page.evaluate('document.querySelector("#cEff span").textContent')
            assert chip_text == 'Reasoning', f'Expected "Reasoning" for separate model, got "{chip_text}"'

            # Click Reasoning chip and select 'Low'
            await page.click('#cEff')
            low_btn = page.locator('#sheet .opt', has_text='Low')
            await low_btn.click()

            chip_text_after = await page.evaluate('document.querySelector("#cEff span").textContent')
            assert chip_text_after == 'Low', f'Expected "Low", got "{chip_text_after}"'
            effort_stored = await page.evaluate('S.efforts[modelKey()]')
            assert effort_stored == 'low', f'Expected "low", got "{effort_stored}"'

            # Send a message
            fake_server.received_calls.clear()
            await page.fill('#inp', 'Explain reasoning in Responses API.')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            post_body = resp.request.post_data_json
            assert post_body.get('variant') == 'low', f'Expected prompt_async variant=low, got {post_body.get("variant")}'

            # Wait for response in feed
            try:
                await page.wait_for_function('!S.running && document.querySelector(".msg.ai")', timeout=10000)
            except Exception as e:
                print('Check 2 received calls:', [(c['path'], list(c['body'].keys()), 'title' in str(c['body'])) for c in fake_server.received_calls])
                print('Check 2 S.msgs:', await page.evaluate('[...S.msgs.values()].map(m => ({role: m.info.role, error: m.info.error, parts: [...m.parts.values()]}))'))
                print('Check 2 S.status:', await page.evaluate('S.status'))
                print('Check 2 lastErr:', await page.evaluate('S.lastErr'))
                raise

            # Check what reached the fake provider at /v1/responses
            resp_calls = [c for c in fake_server.received_calls if '/responses' in c['path'] and 'title generator' not in str(c['body'])]
            assert len(resp_calls) >= 1, 'No prompt call reached /v1/responses'
            reasoning_obj = resp_calls[-1]['body'].get('reasoning')
            actual_effort = reasoning_obj.get('effort') if isinstance(reasoning_obj, dict) else None
            print(f'Provider received at /v1/responses: reasoning.effort = {actual_effort}')
            assert actual_effort == 'low', f'Expected reasoning.effort="low", got {actual_effort}'

            # Verify Thinking card is displayed in feed
            thinking_cards = await page.locator('.thinking-card').count()
            assert thinking_cards >= 1, 'Thinking card should be in feed'
            results['checks'].append('responses_reasoning_effort_low')

            # -------------------------------------------------------------
            # Check 3: Separate per-model persistence & reload
            # -------------------------------------------------------------
            print('--- Check 3: Separate per-model persistence ---')
            # Switch back to chat-model: should be 'High'
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            chat_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-chat' and m['m'] == 'chat-model')
            await page.locator('#sheet .opt').nth(chat_idx).click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'High'
            assert await page.evaluate('S.efforts[modelKey()]') == 'high'

            # Switch to resp-model: should be 'Low'
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            resp_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-resp' and m['m'] == 'resp-model')
            await page.locator('#sheet .opt').nth(resp_idx).click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Low'
            assert await page.evaluate('S.efforts[modelKey()]') == 'low'

            # Reload page and verify resp-model is still 'Low'
            await page.reload()
            await page.wait_for_function('S.spec && S.live && S.provs.length >= 4')
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Low'
            assert await page.evaluate('S.efforts[modelKey()]') == 'low'
            results['checks'].append('separate_model_persistence_and_reload')

            # -------------------------------------------------------------
            # Check 4: xhigh option when model supports xhigh
            # -------------------------------------------------------------
            print('--- Check 4: xhigh support ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            xhigh_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-xhigh' and m['m'] == 'deepseek-v4-flash')
            await page.locator('#sheet .opt').nth(xhigh_idx).click()

            await page.click('#cEff')
            xhigh_options = await page.locator('#sheet .opt').all_inner_texts()
            print('DeepSeek xhigh options:', xhigh_options)
            assert any('Very high' in opt or 'xhigh' in opt for opt in xhigh_options), f'Expected Very high/xhigh in options, got {xhigh_options}'

            very_high_btn = page.locator('#sheet .opt', has_text='Very high')
            await very_high_btn.click()
            assert await page.evaluate('document.querySelector("#cEff span").textContent') == 'Very high'
            assert await page.evaluate('S.efforts[modelKey()]') == 'xhigh'

            fake_server.received_calls.clear()
            await page.fill('#inp', 'Test xhigh prompt.')
            async with page.expect_response(lambda r: '/prompt_async' in r.url) as sent:
                await page.click('#send')
            resp = await sent.value
            assert resp.status == 204
            post_body = resp.request.post_data_json
            assert post_body.get('variant') == 'xhigh', f'Expected variant=xhigh, got {post_body.get("variant")}'

            await page.wait_for_function('!S.running && document.querySelector(".msg.ai")', timeout=30000)
            xhigh_calls = [c for c in fake_server.received_calls if 'title generator' not in str(c['body'])]
            assert len(xhigh_calls) >= 1
            assert xhigh_calls[-1]['body'].get('reasoning_effort') == 'xhigh', f'Expected reasoning_effort="xhigh", got {xhigh_calls[-1]["body"].get("reasoning_effort")}'
            results['checks'].append('xhigh_supported_and_sent')

            # -------------------------------------------------------------
            # Check 5: Preserve existing declared variants
            # -------------------------------------------------------------
            print('--- Check 5: Preserved custom variants ---')
            await page.click('#cModel')
            models = await page.evaluate('allModels()')
            pres_idx = next(i for i, m in enumerate(models) if m['p'] == 'fake-preserved' and m['m'] == 'custom-variants-model')
            await page.locator('#sheet .opt').nth(pres_idx).click()

            await page.click('#cEff')
            pres_options = await page.locator('#sheet .opt').all_inner_texts()
            print('Preserved model options:', pres_options)
            await page.evaluate('closeAll()')
            # Should have Default, Minimal/High, Maximum/Max (its own declared variants, preserved)
            assert 'Default' in pres_options
            assert any('High' in opt for opt in pres_options)
            assert any('Maximum' in opt or 'Max' in opt for opt in pres_options)
            results['checks'].append('existing_variants_preserved')

            # -------------------------------------------------------------
            # Check 6: Expand Thinking card and take proof screenshot
            # -------------------------------------------------------------
            print('--- Check 6: Thinking card UI screenshot ---')
            thinking_detail = page.locator('.thinking-card').first
            await thinking_detail.locator('summary').click()
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(evidence_dir / 'reasoning-thinking-proof.png'))
            results['checks'].append('thinking_rendered_and_expandable')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()
        fake_server.close()


if __name__ == '__main__':
    asyncio.run(main())
