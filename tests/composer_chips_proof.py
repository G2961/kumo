"""Proof of composer touch targets, SSH labels, gradient fade, dark theme contrast, and narrow screens."""
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
            errors = []
            page.on('pageerror', lambda e: errors.append(type(e).__name__))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            await page.wait_for_function('S.spec && S.live')

            # -------------------------------------------------------------
            # Check 1: Minimum 44px touch targets across composer & header
            # -------------------------------------------------------------
            print('--- Check 1: Minimum 44px touch targets ---')
            boxes = await page.evaluate('''() => {
                const getBox = sel => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return { width: r.width, height: r.height };
                };
                return {
                    plus: getBox('#plus'),
                    send: getBox('#send'),
                    cModel: getBox('#cModel'),
                    cEnv: getBox('#cEnv'),
                    menu: getBox('#menu'),
                    gear: getBox('#gear'),
                    commit: getBox('#commit')
                };
            }''')
            print('Touch target sizes:', boxes)
            for name, box in boxes.items():
                assert box is not None, f'Missing element: {name}'
                assert box['height'] >= 43.5, f'{name} height < 44px: {box["height"]}'
                if name in ['plus', 'send', 'menu', 'gear']:
                    assert box['width'] >= 43.5, f'{name} width < 44px: {box["width"]}'
            results['checks'].append('all_touch_targets_min_44px')

            # -------------------------------------------------------------
            # Check 2: Clear labels and SSH connection badge
            # -------------------------------------------------------------
            print('--- Check 2: SSH connection label and icon ---')
            # Test Local mode
            local_label = await page.locator('#cEnv').inner_text()
            print('Local connection label:', local_label)
            assert 'Local' in local_label
            assert 'Build' in local_label or 'build' in local_label.lower()

            # Test SSH mode
            await page.evaluate('''() => {
                S.connections = [{ id: 'ssh_prod', name: '2KopeikiAeza', host: 'server.aeza.net' }];
                S.connectionID = 'ssh_prod';
                chips();
            }''')

            ssh_label = await page.locator('#cEnv').inner_text()
            print('SSH connection label:', ssh_label)
            assert 'SSH: 2KopeikiAeza' in ssh_label, f'Expected "SSH: 2KopeikiAeza", got "{ssh_label}"'
            # Must have terminal icon
            assert await page.locator('#cEnv svg').count() >= 1
            results['checks'].append('clear_ssh_connection_label_and_icon')

            # -------------------------------------------------------------
            # Check 3: Feed bottom spacing and gradient fade
            # -------------------------------------------------------------
            print('--- Check 3: Feed padding and soft gradient mask ---')
            padding_bottom = await page.evaluate('getComputedStyle(document.querySelector("main")).paddingBottom')
            print('Main padding-bottom:', padding_bottom)
            assert int(float(padding_bottom.replace('px', ''))) >= 24, f'Expected main padding-bottom >= 24px, got {padding_bottom}'

            footer_gradient = await page.evaluate('''() => {
                const f = getComputedStyle(document.querySelector("footer"));
                return {
                    bg: f.background || f.backgroundImage,
                    position: f.position
                };
            }''')
            print('Footer gradient:', footer_gradient)
            assert 'gradient' in footer_gradient['bg'].lower()
            results['checks'].append('feed_bottom_spacing_and_gradient')

            # -------------------------------------------------------------
            # Check 4: Dark theme secondary text contrast
            # -------------------------------------------------------------
            print('--- Check 4: Dark theme secondary text contrast ---')
            await page.click('#gear')
            await page.click('#ap')
            await page.click('[data-theme="dark"]')

            dark_colors = await page.evaluate('''() => {
                const s = getComputedStyle(document.documentElement);
                return {
                    bg: s.getPropertyValue('--bg').trim(),
                    mut: s.getPropertyValue('--mut').trim()
                };
            }''')
            print('Dark theme colors:', dark_colors)
            assert dark_colors['mut'].lower() in ['#a9abb6', '#b0b3c2'], f'Expected high-contrast mut #A9ABB6 or #b0b3c2, got {dark_colors["mut"]}'
            results['checks'].append('dark_theme_contrast_improved')

            # -------------------------------------------------------------
            # Check 5: Larger monospace font for diffs
            # -------------------------------------------------------------
            print('--- Check 5: Larger monospace font for diffs ---')
            # Render a diff line to test font size
            await page.evaluate('''() => {
                document.querySelector("#main").innerHTML = `
                    <div class="diff-line a"><span class="diff-num">1</span><span class="diff-sign">+</span><span class="diff-text">test diff font</span></div>
                `;
            }''')
            font_size = await page.evaluate('getComputedStyle(document.querySelector(".diff-line")).fontSize')
            print('Diff line font-size:', font_size)
            # Default 13.5px at scale 1.0
            size_val = float(font_size.replace('px', ''))
            assert size_val >= 13.0, f'Diff font-size too small: {size_val}px'
            results['checks'].append('larger_diff_monospace_font')

            # -------------------------------------------------------------
            # Check 6: Narrow screen (360px & 320px) composer fit
            # -------------------------------------------------------------
            print('--- Check 6: Narrow screen composer responsiveness ---')
            # 360px viewport
            await page.set_viewport_size({'width': 360, 'height': 800})
            await page.wait_for_timeout(100)
            send_box_360 = await page.locator('#send').bounding_box()
            assert send_box_360['x'] + send_box_360['width'] <= 360, 'Send button pushed off-screen at 360px!'
            assert send_box_360['height'] >= 44

            # 320px extreme narrow viewport
            await page.set_viewport_size({'width': 320, 'height': 700})
            await page.wait_for_timeout(100)
            send_box_320 = await page.locator('#send').bounding_box()
            assert send_box_320['x'] + send_box_320['width'] <= 320, 'Send button pushed off-screen at 320px!'
            assert send_box_320['height'] >= 44
            results['checks'].append('narrow_screen_composer_fit_360_320')

            # -------------------------------------------------------------
            # Check 7: Increased font scale (140%)
            # -------------------------------------------------------------
            print('--- Check 7: Increased font scale (140%) ---')
            await page.set_viewport_size({'width': 412, 'height': 892})
            await page.evaluate('document.documentElement.style.setProperty("--font-scale", 1.4)')
            await page.wait_for_timeout(100)
            scaled_box = await page.locator('#send').bounding_box()
            assert scaled_box['height'] >= 44
            results['checks'].append('increased_font_scale_accessible')

            # -------------------------------------------------------------
            # Check 8: Open keyboard docking and screenshots
            # -------------------------------------------------------------
            print('--- Check 8: Open keyboard simulation (height 450px) & screenshots ---')
            await page.set_viewport_size({'width': 412, 'height': 450})
            await page.wait_for_timeout(150)
            comp_box = await page.locator('.comp').bounding_box()
            assert comp_box['y'] + comp_box['height'] <= 450, 'Composer obscured by keyboard!'
            assert comp_box['height'] >= 60

            # Proof screenshot with dark theme, SSH badge, and keyboard docked composer
            await page.screenshot(path=str(evidence_dir / 'composer-chips-dark-keyboard.png'))

            # Proof screenshot in light theme
            await page.evaluate('''() => {
                lset('oc_theme', 'light');
                document.documentElement.dataset.theme = 'light';
                document.documentElement.style.setProperty('--font-scale', 1.0);
            }''')
            await page.set_viewport_size({'width': 412, 'height': 892})
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(evidence_dir / 'composer-chips-light.png'))
            results['checks'].append('composer_screenshots_saved')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()

if __name__ == '__main__':
    asyncio.run(main())
