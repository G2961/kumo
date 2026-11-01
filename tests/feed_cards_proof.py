"""Proof of feed cards, safe Markdown, diff line numbers, tool distinction, and ghost diff prevention."""
import asyncio
import base64
import json
import os
import socket
import subprocess
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

async def main():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        oc_port = s.getsockname()[1]

    env = dict(os.environ)
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'provider': {}})

    proc = subprocess.Popen(
        ['D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe', 'serve', '--hostname', '127.0.0.1', '--port', str(oc_port)],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    origin = f'http://127.0.0.1:{oc_port}'
    evidence_dir = ROOT / 'tests' / 'evidence'
    evidence_dir.mkdir(exist_ok=True)
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

        encoded = base64.urlsafe_b64encode(str(ROOT).encode()).decode().rstrip('=')

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={'width': 424, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(type(e).__name__))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            await page.wait_for_function('S.spec && S.live')

            # -------------------------------------------------------------
            # Check 1: Safe Markdown parsing & HTML sanitization
            # -------------------------------------------------------------
            print('--- Check 1: Safe Markdown parsing & HTML sanitization ---')
            md_test = """
### Header 3
Here is **bold text** and *italic text* with `inline_code()`.

Dangerous script: <script>alert(1)</script> and <img src=x onerror=alert(2)>.

Safe link: [OpenCode Documentation](https://opencode.ai/docs)
Dangerous link: [Malicious Link](javascript:alert(1))

- Bullet 1
- Bullet 2

1. Number 1
2. Number 2

| Feature | Status |
| --- | --- |
| Markdown | Supported |
| Security | Verified |

```python
def solve():
    return 42
```
"""
            await page.evaluate(f'''() => {{
                S.sid = 'ses_md_test';
                S.msgs.set('msg_user', {{
                    info: {{ id: 'msg_user', role: 'user', time: {{ created: Date.now() }} }},
                    parts: new Map([['p0', {{ type: 'text', text: 'Render markdown test' }}]])
                }});
                S.msgs.set('msg_ai', {{
                    info: {{ id: 'msg_ai', role: 'assistant', parentID: 'msg_user', time: {{ created: Date.now() }} }},
                    parts: new Map([['p1', {{ type: 'text', text: {json.dumps(md_test)} }}]])
                }});
                renderMsgs(true);
            }}''')

            ai_msg_html = await page.locator('.msg.ai').inner_html()
            # 1. Bold, italic, inline code
            assert '<strong>bold text</strong>' in ai_msg_html
            assert '<em>italic text</em>' in ai_msg_html
            assert '<code>inline_code()</code>' in ai_msg_html
            # 2. Header
            assert '<h5>Header 3</h5>' in ai_msg_html or '<h3>Header 3</h3>' in ai_msg_html
            # 3. HTML sanitization
            assert '<script>' not in ai_msg_html
            assert '&lt;script&gt;' in ai_msg_html
            assert '<img' not in ai_msg_html
            assert '&lt;img' in ai_msg_html
            # 4. Safe link vs dangerous link
            assert '<a href="https://opencode.ai/docs" target="_blank" rel="noopener noreferrer">OpenCode Documentation</a>' in ai_msg_html
            assert 'href="javascript:' not in ai_msg_html
            # 5. Lists
            assert '<ul><li>Bullet 1</li>' in ai_msg_html
            assert '<ol><li>Number 1</li>' in ai_msg_html
            # 6. Table
            assert '<table>' in ai_msg_html and '<th>Feature</th>' in ai_msg_html and '<td>Markdown</td>' in ai_msg_html
            # 7. Code block with Copy button
            assert '<div class="code-block">' in ai_msg_html
            assert 'class="copy-btn"' in ai_msg_html
            results['checks'].append('safe_markdown_tables_lists_code_blocks')

            # -------------------------------------------------------------
            # Check 2: Distinct Read vs Write signatures & error text
            # -------------------------------------------------------------
            print('--- Check 2: Distinct Read vs Write signatures & tool error text ---')
            await page.evaluate('''() => {
                S.msgs.clear();
                S.msgs.set('msg_tools', {
                    info: { id: 'msg_tools', role: 'assistant', time: { created: Date.now() } },
                    parts: new Map([
                        ['p_read', {
                            id: 'p_read', type: 'tool', tool: 'read',
                            state: { status: 'completed', input: { filePath: '/src/test.txt' }, output: '27 characters in this file.' }
                        }],
                        ['p_read_err', {
                            id: 'p_read_err', type: 'tool', tool: 'read',
                            state: { status: 'error', input: { filePath: '/src/test-2.txt' }, error: 'ENOENT: no such file or directory, open "/src/test-2.txt"' }
                        }],
                        ['p_write', {
                            id: 'p_write', type: 'tool', tool: 'write',
                            state: { status: 'completed', input: { filePath: '/src/test-2.txt', content: 'Fifteen chars!!' }, metadata: { diff: '--- /dev/null\\n+++ b/test-2.txt\\n@@ -0,0 +1 @@\\n+Fifteen chars!!' } }
                        }]
                    ])
                });
                renderMsgs(true);
            }''')

            # Verify read card says 'Read test.txt' with char count
            read_card = page.locator('.card', has_text='Read test.txt')
            assert await read_card.count() == 1
            assert '27 chars' in await read_card.inner_text()

            # Verify read error card displays full error text and 'Failed' status
            read_err_card = page.locator('.card', has_text='Read test-2.txt')
            assert await read_err_card.count() == 1
            assert 'Failed' in await read_err_card.inner_text()
            assert 'no such file or directory' in await read_err_card.inner_text()
            results['checks'].append('tool_error_displays_exact_message')

            # -------------------------------------------------------------
            # Check 3: Diff formatting strips ---/+++/@@ and adds line numbers
            # -------------------------------------------------------------
            print('--- Check 3: Clean diff line numbers without ---/+++/@@ ---')
            diff_text = await page.locator('.diff-line').all_text_contents()
            print('Rendered diff lines:', diff_text)
            diff_html = await page.locator('.change-card').first.inner_html()
            assert '--- /dev/null' not in diff_html
            assert '+++ b/test-2.txt' not in diff_html
            assert '@@ -0,0 +1 @@' not in diff_html
            assert await page.locator('.diff-num').count() >= 1
            assert await page.locator('.diff-sign').count() >= 1
            results['checks'].append('clean_diff_line_numbers_without_git_headers')

            # -------------------------------------------------------------
            # Check 4: Command header and Show more on long output
            # -------------------------------------------------------------
            print('--- Check 4: Command title, SSH badge, and Show more collapsible output ---')
            long_output = '\n'.join([f'line {i}: output data stream from remote server' for i in range(25)])
            await page.evaluate(f'''() => {{
                S.connections = [{{ id: 'conn_1', name: 'my-production-server' }}];
                S.msgs.set('msg_exec', {{
                    info: {{ id: 'msg_exec', role: 'assistant', time: {{ created: Date.now() }} }},
                    parts: new Map([
                        ['p_exec', {{
                            id: 'p_exec', type: 'tool', tool: 'mobile_ssh_ssh_exec',
                            state: {{ status: 'completed', input: {{ command: 'systemctl status nginx', connection_id: 'conn_1' }}, output: JSON.stringify({{ stdout: {json.dumps(long_output)}, stderr: '' }}) }}
                        }}]
                    ])
                }});
                renderMsgs(true);
            }}''')

            exec_card = page.locator('.card', has_text='systemctl status nginx')
            assert await exec_card.count() == 1
            # Command is action title
            assert 'systemctl status nginx' in await exec_card.locator('.tool-action').inner_text()
            # Host name is badge
            assert 'my-production-server' in await exec_card.locator('.ssh-badge').inner_text()
            # Status is unified 'Completed'
            assert 'Completed' in await exec_card.inner_text()

            # Collapsible 'Show more' button
            show_more = exec_card.locator('.show-more-btn')
            assert await show_more.count() == 1
            assert await show_more.inner_text() == 'Show more'
            pre_element = exec_card.locator('pre')
            assert 'collapsed-pre' in (await pre_element.get_attribute('class'))

            # Click Show more -> expands to Show less
            await show_more.click()
            assert await show_more.inner_text() == 'Show less'
            assert 'collapsed-pre' not in (await pre_element.get_attribute('class'))
            results['checks'].append('collapsible_output_show_more_toggle')

            # -------------------------------------------------------------
            # Check 5: Reproduce and prove absence of GHOST DIFF
            # -------------------------------------------------------------
            print('--- Check 5: Two consecutive writes to same SSH file without ghost diff ---')
            await page.evaluate('''() => {
                S.msgs.clear();
                S.remoteRuns.clear();

                // Turn 1: edit /root/test.txt (15 chars)
                S.remoteRuns.set('run_1', {
                    id: 'run_1', sessionID: S.sid, connectionID: 'conn_1',
                    kind: 'write', path: '/root/test.txt',
                    patch: '@@ -1,1 +1,1 @@\\n-old 27 characters\\n+15 characters!!',
                    additions: 1, deletions: 1, status: 'completed'
                });
                S.msgs.set('msg_turn1', {
                    info: { id: 'msg_turn1', role: 'assistant', time: { created: 1000 } },
                    parts: new Map([
                        ['p_write1', {
                            id: 'p_write1', type: 'tool', tool: 'mobile_ssh_ssh_write_file',
                            state: { status: 'completed', input: { path: '/root/test.txt', connection_id: 'conn_1' }, output: JSON.stringify({ runID: 'run_1', additions: 1, deletions: 1 }) }
                        }]
                    ])
                });

                // Turn 2: edit /root/test.txt again (34 chars)
                S.remoteRuns.set('run_2', {
                    id: 'run_2', sessionID: S.sid, connectionID: 'conn_1',
                    kind: 'write', path: '/root/test.txt',
                    patch: '@@ -1,1 +1,1 @@\\n-15 characters!!\\n+34 characters in second write now!!',
                    additions: 1, deletions: 1, status: 'completed'
                });
                S.msgs.set('msg_turn2', {
                    info: { id: 'msg_turn2', role: 'assistant', time: { created: 2000 } },
                    parts: new Map([
                        ['p_write2', {
                            id: 'p_write2', type: 'tool', tool: 'mobile_ssh_ssh_write_file',
                            state: { status: 'completed', input: { path: '/root/test.txt', connection_id: 'conn_1' }, output: JSON.stringify({ runID: 'run_2', additions: 1, deletions: 1 }) }
                        }]
                    ])
                });

                renderMsgs(true);
            }''')

            # Count total diff cards for /root/test.txt in the feed
            test_diff_cards = await page.locator('.change-card').count()
            print('Total change cards for test.txt:', test_diff_cards)
            # MUST be exactly 2 cards (Turn 1 and Turn 2), NOT 3! (No ghost diff of run_1 at the bottom)
            assert test_diff_cards == 2, f'Ghost diff detected! Expected exactly 2 diff cards, found {test_diff_cards}'
            results['checks'].append('two_consecutive_ssh_edits_without_ghost_diff')

            # Expand the second diff card and take proof screenshot
            await page.locator('.change-card summary').last.click()
            await page.wait_for_timeout(200)
            await page.screenshot(path=str(evidence_dir / 'feed-cards-and-diff-proof.png'))
            results['checks'].append('feed_cards_proof_screenshot_saved')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()

if __name__ == '__main__':
    asyncio.run(main())
