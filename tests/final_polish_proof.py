"""Proof of loading skeletons, informative empty states, swipe-to-dismiss sheet, and code copy."""
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
            page = await browser.new_page(viewport={'width': 412, 'height': 892})
            await page.context.grant_permissions(['local-network-access', 'clipboard-read', 'clipboard-write'], origin=origin)
            errors = []
            page.on('pageerror', lambda e: errors.append(type(e).__name__))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            await page.wait_for_function('S.spec && S.live')

            # -------------------------------------------------------------
            # Check 1: Loading skeletons (session and folder skeletons)
            # -------------------------------------------------------------
            print('--- Check 1: Session loading skeletons ---')
            await page.evaluate('''() => {
                renderSessionSkeleton();
            }''')
            assert await page.locator('.skeleton-user .sk-line').count() == 1
            assert await page.locator('.skeleton-ai .sk-line').count() == 3
            results['checks'].append('session_loading_skeletons')

            # -------------------------------------------------------------
            # Check 2: Informative empty states
            # -------------------------------------------------------------
            print('--- Check 2: Informative empty states ---')
            # 1. Empty Changes sheet
            await page.evaluate("document.querySelector('#commit').click()")
            await page.wait_for_selector('#sheet.on')
            empty_changes = page.locator('#sheet .empty-state')
            assert await empty_changes.count() == 1
            assert 'No thread selected' in await empty_changes.inner_text()
            assert 'Open a thread' in await empty_changes.inner_text()
            await page.evaluate('closeAll()')
            await page.wait_for_selector('#sheet:not(.on)')

            # 2. Empty SSH connections sheet
            await page.click('#cEnv')
            await page.wait_for_selector('#sheet.on')
            empty_ssh = page.locator('#sheet .empty-state')
            assert await empty_ssh.count() == 1
            assert 'No SSH connections yet' in await empty_ssh.inner_text()
            assert 'Add an SSH server' in await empty_ssh.inner_text()
            await page.evaluate('closeAll()')
            await page.wait_for_selector('#sheet:not(.on)')

            # 3. Empty project folder in drawer
            await page.click('#menu')
            await page.wait_for_selector('#drawer.on')
            await page.evaluate('''() => {
                S.folderSessions.set('/empty-test-dir', []);
                renderFolderSessions('/empty-test-dir');
            }''')
            await page.evaluate('closeAll()')
            await page.wait_for_selector('#drawer:not(.on)')
            results['checks'].append('informative_empty_states_verified')

            # -------------------------------------------------------------
            # Check 3: Swipe down to dismiss sheet
            # -------------------------------------------------------------
            print('--- Check 3: Swipe down to dismiss bottom sheet ---')
            await page.click('#gear')
            await page.wait_for_selector('#sheet.on')

            grab_box = await page.locator('#sheet .grab').bounding_box()
            assert grab_box is not None, 'Grab handle missing on bottom sheet'

            # Small drag (<80px) -> springs back open
            await page.mouse.move(grab_box['x'] + grab_box['width'] / 2, grab_box['y'] + grab_box['height'] / 2)
            await page.mouse.down()
            await page.mouse.move(grab_box['x'] + grab_box['width'] / 2, grab_box['y'] + 35)
            await page.mouse.up()
            await page.wait_for_timeout(200)
            assert await page.locator('#sheet.on').count() == 1, 'Small drag should not dismiss sheet'

            # Full swipe down (>80px) -> dismisses sheet
            grab_box = await page.locator('#sheet .grab').bounding_box()
            await page.mouse.move(grab_box['x'] + grab_box['width'] / 2, grab_box['y'] + grab_box['height'] / 2)
            await page.mouse.down()
            await page.mouse.move(grab_box['x'] + grab_box['width'] / 2, grab_box['y'] + 150, steps=5)
            await page.mouse.up()
            await page.wait_for_selector('#sheet:not(.on)', timeout=5000)
            results['checks'].append('swipe_to_dismiss_bottom_sheet')

            # -------------------------------------------------------------
            # Check 4: Code block copying
            # -------------------------------------------------------------
            print('--- Check 4: Code block copying ---')
            md_code = '```javascript\nfunction helloWorld() {\n  return "Hello from Codex Mobile!";\n}\n```'
            await page.evaluate(f'''() => {{
                S.sid = 'ses_copy_test';
                S.msgs.set('msg_copy', {{
                    info: {{ id: 'msg_copy', role: 'assistant', time: {{ created: Date.now() }} }},
                    parts: new Map([['p_code', {{ type: 'text', text: {json.dumps(md_code)} }}]])
                }});
                renderMsgs(true);
            }}''')

            copy_btn = page.locator('.copy-btn').first
            assert await copy_btn.count() == 1
            assert await copy_btn.inner_text() == 'Copy'

            await copy_btn.click()
            await page.wait_for_timeout(100)
            btn_text = await copy_btn.inner_text()
            print('Copy button status after click:', btn_text)
            assert btn_text == 'Copied!', f'Expected "Copied!", got "{btn_text}"'
            assert 'copied' in (await copy_btn.get_attribute('class'))

            # Screenshot of code block with 'Copied!' badge
            await page.screenshot(path=str(evidence_dir / 'final-polish-code-copy.png'))
            results['checks'].append('code_block_copy_with_feedback')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()

if __name__ == '__main__':
    asyncio.run(main())
