"""Proof of WebView layout, non-stretchable pages, keyboard resizing, and selection rules."""
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

        # -------------------------------------------------------------
        # Check 1: Kotlin & Manifest configuration
        # -------------------------------------------------------------
        print('--- Check 1: Verify Kotlin and AndroidManifest configuration ---')
        manifest_text = (ROOT / 'app/src/main/AndroidManifest.xml').read_text('utf-8')
        assert 'android:windowSoftInputMode="adjustResize"' in manifest_text
        results['checks'].append('manifest_adjust_resize')

        kt_text = (ROOT / 'app/src/main/java/space/g2961/opencode/MainActivity.kt').read_text('utf-8')
        assert 'overScrollMode = View.OVER_SCROLL_NEVER' in kt_text
        assert 'setSupportZoom(false)' in kt_text
        assert 'builtInZoomControls = false' in kt_text
        assert 'displayZoomControls = false' in kt_text
        assert 'SOFT_INPUT_ADJUST_RESIZE' in kt_text
        results['checks'].append('kotlin_overscroll_zoom_and_resize')

        encoded = base64.urlsafe_b64encode(str(ROOT).encode()).decode().rstrip('=')

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={'width': 412, 'height': 892})
            errors = []
            page.on('pageerror', lambda e: errors.append(type(e).__name__))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            await page.wait_for_function('S.spec && S.live')

            # -------------------------------------------------------------
            # Check 2: Viewport meta tag prevents zoom/scaling
            # -------------------------------------------------------------
            print('--- Check 2: Viewport meta tag restrictions ---')
            viewport_content = await page.locator('meta[name="viewport"]').get_attribute('content')
            print('Viewport content:', viewport_content)
            assert 'maximum-scale=1' in viewport_content
            assert 'user-scalable=no' in viewport_content
            results['checks'].append('viewport_zoom_disabled')

            # -------------------------------------------------------------
            # Check 3: CSS overflow, overscroll-behavior, and 100dvh
            # -------------------------------------------------------------
            print('--- Check 3: CSS overflow, dvh height, and overscroll-behavior ---')
            html_styles = await page.evaluate('''() => {
                const h = getComputedStyle(document.documentElement);
                const b = getComputedStyle(document.body);
                const a = getComputedStyle(document.querySelector("#app"));
                const m = getComputedStyle(document.querySelector("main"));
                return {
                    htmlOverflow: h.overflow,
                    htmlOverscroll: h.overscrollBehaviorY || h.overscrollBehavior,
                    bodyOverflow: b.overflow,
                    bodyOverscroll: b.overscrollBehaviorY || b.overscrollBehavior,
                    appHeight: a.height,
                    mainOverflow: m.overflowY,
                    mainOverscroll: m.overscrollBehaviorY || m.overscrollBehavior
                };
            }''')
            print('Computed scroll styles:', html_styles)
            assert html_styles['htmlOverflow'] == 'hidden'
            assert html_styles['bodyOverflow'] == 'hidden'
            assert html_styles['htmlOverscroll'] == 'none'
            assert html_styles['bodyOverscroll'] == 'none'
            assert html_styles['mainOverflow'] == 'auto'
            assert html_styles['mainOverscroll'] in ['contain', 'none']
            results['checks'].append('css_overflow_and_overscroll_containment')

            # -------------------------------------------------------------
            # Check 4: user-select rules (none for UI, text for messages)
            # -------------------------------------------------------------
            print('--- Check 4: user-select rules ---')
            # Add dummy message to test text selection
            await page.evaluate('''() => {
                document.querySelector("#main").innerHTML = `
                    <div class="msg user">Selectable user text</div>
                    <div class="msg ai">Selectable assistant text</div>
                    <pre class="card"><code>Selectable code block</code></pre>
                `;
            }''')

            select_styles = await page.evaluate('''() => {
                const header = getComputedStyle(document.querySelector("header"));
                const button = getComputedStyle(document.querySelector("#menu"));
                const chip = getComputedStyle(document.querySelector("#cModel"));
                const userMsg = getComputedStyle(document.querySelector(".msg.user"));
                const aiMsg = getComputedStyle(document.querySelector(".msg.ai"));
                const pre = getComputedStyle(document.querySelector("pre"));
                const textarea = getComputedStyle(document.querySelector("#inp"));
                return {
                    headerSelect: header.userSelect,
                    buttonSelect: button.userSelect,
                    chipSelect: chip.userSelect,
                    userMsgSelect: userMsg.userSelect,
                    aiMsgSelect: aiMsg.userSelect,
                    preSelect: pre.userSelect,
                    textareaSelect: textarea.userSelect
                };
            }''')
            print('Computed user-select styles:', select_styles)
            assert select_styles['headerSelect'] == 'none'
            assert select_styles['buttonSelect'] == 'none'
            assert select_styles['chipSelect'] == 'none'
            assert select_styles['userMsgSelect'] in ['text', 'auto']
            assert select_styles['aiMsgSelect'] in ['text', 'auto']
            assert select_styles['preSelect'] in ['text', 'auto']
            assert select_styles['textareaSelect'] in ['text', 'auto']
            results['checks'].append('user_select_ui_none_content_text')

            # -------------------------------------------------------------
            # Check 5: Tap highlight disabled
            # -------------------------------------------------------------
            print('--- Check 5: Tap highlight disabled ---')
            tap_highlight = await page.evaluate('''() => {
                const b = getComputedStyle(document.querySelector("#send"));
                return b.webkitTapHighlightColor;
            }''')
            print('Tap highlight color:', tap_highlight)
            assert tap_highlight in ['rgba(0, 0, 0, 0)', 'transparent']
            results['checks'].append('tap_highlight_disabled')

            # -------------------------------------------------------------
            # Check 6: Keyboard resize simulation (adjustResize)
            # -------------------------------------------------------------
            print('--- Check 6: Keyboard resize simulation ---')
            # Viewport simulates virtual keyboard opening (height shrinks from 892 to 460)
            await page.set_viewport_size({'width': 412, 'height': 460})
            await page.wait_for_timeout(150)

            composer_box = await page.locator('.comp').bounding_box()
            viewport_size = page.viewport_size
            print('Composer position with keyboard:', composer_box, 'Viewport height:', viewport_size['height'])
            # Composer bottom must be within viewport height
            composer_bottom = composer_box['y'] + composer_box['height']
            assert composer_bottom <= viewport_size['height'], f'Composer obscured by keyboard! bottom={composer_bottom}, viewport={viewport_size["height"]}'
            assert composer_box['y'] > 0, 'Composer is visible above bottom'
            results['checks'].append('composer_visible_with_keyboard_adjust_resize')

            # Screenshot showing composer docked with keyboard
            evidence_dir = ROOT / 'tests' / 'evidence'
            await page.screenshot(path=str(evidence_dir / 'webview-keyboard-composer.png'))
            results['checks'].append('keyboard_composer_screenshot_saved')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()

if __name__ == '__main__':
    asyncio.run(main())
