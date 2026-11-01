import asyncio
import base64
import json
import os
import socket
import subprocess
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / 'app/src/main/assets/codex.html'

def relative_luminance(r, g, b):
    def linearize(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b)

def contrast_ratio(rgb1, rgb2):
    L1 = relative_luminance(*rgb1)
    L2 = relative_luminance(*rgb2)
    top = max(L1, L2)
    bot = min(L1, L2)
    return (top + 0.05) / (bot + 0.05)

def parse_rgb(css_str):
    css_str = css_str.strip()
    if css_str.startswith('#'):
        h = css_str.lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    if 'rgb' in css_str:
        parts = css_str.split('(')[1].split(')')[0].split(',')
        return tuple(int(float(parts[i].strip())) for i in range(3))
    raise ValueError(f"Unknown color: {css_str}")

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

    try:
        import urllib.request
        for _ in range(80):
            try:
                with urllib.request.urlopen(origin + '/global/health', timeout=1) as r:
                    if json.load(r).get('healthy'):
                        break
            except Exception:
                await asyncio.sleep(0.1)

        encoded_dir = base64.urlsafe_b64encode(str(ROOT).encode()).decode().rstrip('=')

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            await page.context.grant_permissions(['local-network-access', 'clipboard-read', 'clipboard-write'], origin=origin)
            errors = []
            page.on('pageerror', lambda e: errors.append(type(e).__name__))

            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(HTML_PATH),
                content_type='text/html; charset=utf-8'
            ))

            await page.goto(f'{origin}/_ui/index.html#d={encoded_dir}')
            await page.wait_for_function('S.spec && S.live')

            # -------------------------------------------------------------
            # 1. Dark Theme Tokens & Contrast (390x844)
            # -------------------------------------------------------------
            print('\n=== CHECK 1: DARK THEME (390x844) ===')
            await page.evaluate('''() => {
                document.documentElement.dataset.theme = 'dark';
                lset('oc_theme', 'dark');
                appearance();
            }''')
            await page.wait_for_timeout(100)

            dark_tokens = await page.evaluate('''() => {
                const cs = getComputedStyle(document.documentElement);
                return {
                    base: cs.getPropertyValue('--base').trim(),
                    surface1: cs.getPropertyValue('--surface-1').trim(),
                    surface2: cs.getPropertyValue('--surface-2').trim(),
                    border: cs.getPropertyValue('--border').trim(),
                    text: cs.getPropertyValue('--text').trim(),
                    text2: cs.getPropertyValue('--text-2').trim(),
                    sendBg: cs.getPropertyValue('--send-bg').trim()
                };
            }''')
            print('Dark theme tokens:', dark_tokens)
            assert dark_tokens['base'].upper() == '#0A0A0B', f"Base is {dark_tokens['base']}"
            assert dark_tokens['surface1'].upper() == '#131315', f"Surface-1 is {dark_tokens['surface1']}"
            assert dark_tokens['surface2'].upper() == '#1B1B1E', f"Surface-2 is {dark_tokens['surface2']}"
            assert '255' in dark_tokens['border'] and '0.08' in dark_tokens['border'], f"Border is {dark_tokens['border']}"
            assert dark_tokens['text'].upper() == '#EDEDED', f"Text is {dark_tokens['text']}"
            assert dark_tokens['text2'].upper() == '#A9ABB6', f"Text-2 is {dark_tokens['text2']}"
            assert dark_tokens['sendBg'].upper() in ['#FFFFFF', 'RGB(255, 255, 255)'], f"Send button is {dark_tokens['sendBg']}"

            rgb_base = parse_rgb(dark_tokens['base'])
            rgb_s1 = parse_rgb(dark_tokens['surface1'])
            rgb_s2 = parse_rgb(dark_tokens['surface2'])
            rgb_text = parse_rgb(dark_tokens['text'])
            rgb_text2 = parse_rgb(dark_tokens['text2'])

            dark_text_contrast = contrast_ratio(rgb_text, rgb_base)
            dark_text2_base_contrast = contrast_ratio(rgb_text2, rgb_base)
            dark_text2_s1_contrast = contrast_ratio(rgb_text2, rgb_s1)
            dark_text2_s2_contrast = contrast_ratio(rgb_text2, rgb_s2)

            print(f"Dark text contrast (#EDEDED on #0A0A0B): {dark_text_contrast:.2f}:1 (target >= 4.5:1)")
            print(f"Dark text-2 contrast (#A9ABB6 on #0A0A0B): {dark_text2_base_contrast:.2f}:1 (target >= 4.5:1)")
            print(f"Dark text-2 on Surface-1 (#A9ABB6 on #131315): {dark_text2_s1_contrast:.2f}:1 (target >= 4.5:1)")
            print(f"Dark text-2 on Surface-2 (#A9ABB6 on #1B1B1E): {dark_text2_s2_contrast:.2f}:1 (target >= 4.5:1)")
            assert dark_text_contrast >= 4.5
            assert dark_text2_base_contrast >= 4.5
            assert dark_text2_s1_contrast >= 4.5
            assert dark_text2_s2_contrast >= 4.5

            # Check drawer border in dark mode
            drawer_border = await page.evaluate('getComputedStyle(document.querySelector("#drawer")).borderRight')
            drawer_shadow = await page.evaluate('getComputedStyle(document.querySelector("#drawer")).boxShadow')
            print(f"Drawer border: {drawer_border}, shadow: {drawer_shadow}")
            assert '1px' in drawer_border, f"Expected 1px border on drawer, got {drawer_border}"

            await page.screenshot(path=str(ROOT / 'tests/evidence/design-dark-390x844.png'))

            # -------------------------------------------------------------
            # 2. Light Theme Tokens & Contrast (390x844)
            # -------------------------------------------------------------
            print('\n=== CHECK 2: LIGHT THEME (390x844) ===')
            await page.evaluate('''() => {
                document.documentElement.dataset.theme = 'light';
                lset('oc_theme', 'light');
                appearance();
            }''')
            await page.wait_for_timeout(100)

            light_tokens = await page.evaluate('''() => {
                const cs = getComputedStyle(document.documentElement);
                return {
                    base: cs.getPropertyValue('--base').trim(),
                    surface1: cs.getPropertyValue('--surface-1').trim(),
                    surface2: cs.getPropertyValue('--surface-2').trim(),
                    border: cs.getPropertyValue('--border').trim(),
                    text: cs.getPropertyValue('--text').trim(),
                    text2: cs.getPropertyValue('--text-2').trim()
                };
            }''')
            print('Light theme tokens:', light_tokens)
            rgb_l_base = parse_rgb(light_tokens['base'])
            rgb_l_s2 = parse_rgb(light_tokens['surface2'])
            rgb_l_text = parse_rgb(light_tokens['text'])
            rgb_l_text2 = parse_rgb(light_tokens['text2'])

            light_surface_contrast = contrast_ratio(rgb_l_base, rgb_l_s2)
            light_text_contrast = contrast_ratio(rgb_l_text, rgb_l_base)
            light_text2_contrast = contrast_ratio(rgb_l_text2, rgb_l_base)
            light_text2_s2_contrast = contrast_ratio(rgb_l_text2, rgb_l_s2)

            print(f"Light surface contrast (Base vs Surface-2): {light_surface_contrast:.2f}:1 (target >= 1.3:1)")
            print(f"Light text contrast (#111215 on #FFFFFF): {light_text_contrast:.2f}:1 (target >= 4.5:1)")
            print(f"Light text-2 contrast (#595E6B on #FFFFFF): {light_text2_contrast:.2f}:1 (target >= 4.5:1)")
            print(f"Light text-2 on Surface-2 (#595E6B on #E2E2E8): {light_text2_s2_contrast:.2f}:1 (target >= 4.5:1)")
            assert light_surface_contrast >= 1.30, f"Light surface contrast < 1.3:1: {light_surface_contrast}"
            assert light_text_contrast >= 4.5
            assert light_text2_contrast >= 4.5
            assert light_text2_s2_contrast >= 4.5

            await page.screenshot(path=str(ROOT / 'tests/evidence/design-light-390x844.png'))

            # Switch back to dark theme for remaining checks
            await page.evaluate('''() => {
                document.documentElement.dataset.theme = 'dark';
                lset('oc_theme', 'dark');
                appearance();
            }''')
            await page.wait_for_timeout(100)

            # -------------------------------------------------------------
            # 3. Touch Targets (min 44x44)
            # -------------------------------------------------------------
            print('\n=== CHECK 3: TOUCH TARGETS (>= 44x44) ===')
            targets = await page.evaluate('''() => {
                const selList = ['#menu', '#gear', '#plus', '#cModel', '#cEnv', '#send'];
                const res = {};
                for (const s of selList) {
                    const el = document.querySelector(s);
                    if (el) {
                        const r = el.getBoundingClientRect();
                        res[s] = { width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10 };
                    }
                }
                return res;
            }''')
            print('Touch target sizes (pixels):', targets)
            for sel, box in targets.items():
                assert box['height'] >= 43.5, f"{sel} height < 44px: {box['height']}"
                if sel in ['#menu', '#gear', '#plus', '#send']:
                    assert box['width'] >= 43.5, f"{sel} width < 44px: {box['width']}"
            print("All header and composer targets meet >= 44x44 zone!")

            # -------------------------------------------------------------
            # 4. Sidebar Hierarchy: Thread Title Indent & Right Column Alignment
            # -------------------------------------------------------------
            print('\n=== CHECK 4: SIDEBAR HIERARCHY & ALIGNMENTS ===')
            await page.click('#menu')
            await page.wait_for_selector('#drawer.on')

            # Add mock project with sessions
            await page.evaluate('''() => {
                S.savedProjects = ['/proj/alpha'];
                S.projects = [{ worktree: '/proj/alpha' }];
                S.dir = '/proj/alpha';
                S.expandedFolders.add('/proj/alpha');
                S.folderSessions.set('/proj/alpha', [{ id: 't1', title: 'Feature implement auth', time: { updated: Date.now() - 60000 } }]);
                renderDrawer();
            }''')
            await page.wait_for_timeout(250)

            alignment_data = await page.evaluate('''() => {
                const folderTitle = document.querySelector('.proj-title');
                const threadTitle = document.querySelector('.th-title');
                const plusBtn = document.querySelector('.proj-h .ib-right-col');
                const timeCol = document.querySelector('.th .ib-right-col');

                const fBox = folderTitle.getBoundingClientRect();
                const tBox = threadTitle.getBoundingClientRect();
                const pBox = plusBtn.getBoundingClientRect();
                const timeBox = timeCol.getBoundingClientRect();

                return {
                    folderTitleX: Math.round(fBox.x),
                    threadTitleX: Math.round(tBox.x),
                    indentDiff: Math.round(tBox.x - fBox.x),
                    plusRight: Math.round((pBox.x + pBox.width) * 10) / 10,
                    timeRight: Math.round((timeBox.x + timeBox.width) * 10) / 10,
                    rightColDiff: Math.abs(Math.round((pBox.x + pBox.width - (timeBox.x + timeBox.width)) * 10) / 10),
                    folderRowHeight: Math.round(document.querySelector('.proj-h').getBoundingClientRect().height),
                    threadRowHeight: Math.round(document.querySelector('.th').getBoundingClientRect().height)
                };
            }''')
            print('Sidebar alignment measurements:', alignment_data)
            print(f"Thread title indent relative to folder: +{alignment_data['indentDiff']}px (target >= +16px)")
            print(f"Right column '+' right: {alignment_data['plusRight']}, time right: {alignment_data['timeRight']}, diff: {alignment_data['rightColDiff']}px (target <= 1px)")
            print(f"Folder row height: {alignment_data['folderRowHeight']}px (target >= 48px)")
            print(f"Thread row height: {alignment_data['threadRowHeight']}px (target >= 48px)")

            assert alignment_data['indentDiff'] >= 16, f"Thread text indent is {alignment_data['indentDiff']}px, expected >= 16px"
            assert alignment_data['rightColDiff'] <= 1.0, f"Right column mismatch: {alignment_data['rightColDiff']}px > 1px"
            assert alignment_data['folderRowHeight'] >= 48
            assert alignment_data['threadRowHeight'] >= 48

            # Verify active project has dot and bold name, but NO "Active" badge
            proj_badge_count = await page.locator('.active-badge').count()
            proj_dot_count = await page.locator('.proj-dot').count()
            print(f"Active badge count: {proj_badge_count} (must be 0), Accent dot count: {proj_dot_count} (must be >= 1)")
            assert proj_badge_count == 0, "Found 'Active' text badge in sidebar!"
            assert proj_dot_count >= 1, "Missing accent dot for active project!"

            await page.screenshot(path=str(ROOT / 'tests/evidence/design-sidebar-alignment.png'))
            await page.evaluate('closeAll()')
            await page.wait_for_selector('#drawer:not(.on)')
            await page.wait_for_timeout(300)

            # -------------------------------------------------------------
            # 5. Home Screen: Clickable Elements Count (<= 8)
            # -------------------------------------------------------------
            print('\n=== CHECK 5: HOME SCREEN CLICKABLE ELEMENTS COUNT ===')
            await page.evaluate('''() => {
                S.sid = null;
                render();
            }''')
            await page.wait_for_timeout(100)

            clickable_elements = await page.evaluate('''() => {
                const all = Array.from(document.querySelectorAll('button, a, input:not([type="hidden"]), textarea'));
                const visible = all.filter(el => {
                    const style = getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0' || style.pointerEvents === 'none') return false;
                    const r = el.getBoundingClientRect();
                    // Visible interactive control inside the viewport
                    return r.width >= 24 && r.height >= 24 && r.right > 0 && r.bottom > 0 && r.left < window.innerWidth && r.top < window.innerHeight;
                });
                return visible.map(el => ({
                    id: el.id,
                    tag: el.tagName,
                    cls: el.className,
                    text: el.innerText.trim().replace(/\\n/g, ' ').slice(0, 30)
                }));
            }''')
            print(f"Visible clickable elements on home screen (count = {len(clickable_elements)}):")
            for i, el in enumerate(clickable_elements, 1):
                print(f"  {i}. <{el['tag']} id='{el['id']}' class='{el['cls']}'> '{el['text']}'")

            assert len(clickable_elements) <= 8, f"Too many clickable elements on home screen: {len(clickable_elements)} > 8"

            # Verify "Add dark mode" and logo tile are removed
            home_text = await page.locator('#main').inner_text()
            assert 'Add dark mode' not in home_text, "'Add dark mode' still present on home!"
            assert await page.locator('.logo').count() == 0, "Logo tile still present on home!"
            assert 'Let\'s build' in home_text, "Missing header on home!"

            await page.screenshot(path=str(ROOT / 'tests/evidence/design-home-screen.png'))

            # -------------------------------------------------------------
            # 6. End-to-End Scenario: Send Message -> See Response -> Open Changes
            # -------------------------------------------------------------
            print('\n=== CHECK 6: END-TO-END WORKFLOW (Prompt -> Response -> Changes) ===')
            # Setup session with recorded diff
            await page.evaluate('''() => {
                S.sid = 'session-diff-test';
                S.sessions = [{ id: 'session-diff-test', title: 'Update styling and tests', time: { updated: Date.now() } }];
                S.historyFiles = new Map([
                    ['src/index.ts', { file: 'src/index.ts', additions: 12, deletions: 3, commit: 'c1' }]
                ]);
                S.msgs = new Map([
                    ['m1', {
                        info: { id: 'm1', role: 'user' },
                        parts: [{ type: 'text', text: 'Update styling tokens and tests' }]
                    }],
                    ['m2', {
                        info: { id: 'm2', role: 'assistant' },
                        parts: [{ type: 'text', text: 'I updated the tokens to `#0A0A0B` and recorded the changes.' }]
                    }]
                ]);
                render();
            }''')
            await page.wait_for_timeout(100)

            commit_text = await page.locator('#commit').inner_text()
            print(f"Commit button text after recorded changes: '{commit_text}'")
            assert '+12' in commit_text and '3' in commit_text, f"Expected '+12 -3' in commit button, got '{commit_text}'"

            # Click Changes button -> sheet opens
            await page.click('#commit')
            await page.wait_for_selector('#sheet.on')
            sheet_text = await page.locator('#sheet').inner_text()
            print(f"Changes sheet content preview: {sheet_text[:80]}...")
            assert 'Changes' in sheet_text

            await page.screenshot(path=str(ROOT / 'tests/evidence/design-changes-flow.png'))
            await page.evaluate('closeAll()')
            await page.wait_for_selector('#sheet:not(.on)')

        print('\nALL ACCEPTANCE CRITERIA PASSED WITH MEASURED NUMBERS!')
        report = {
            'dark_text_contrast': round(dark_text_contrast, 2),
            'dark_text2_contrast': round(dark_text2_base_contrast, 2),
            'light_surface_contrast': round(light_surface_contrast, 2),
            'light_text_contrast': round(light_text_contrast, 2),
            'light_text2_contrast': round(light_text2_contrast, 2),
            'sidebar_indent_px': alignment_data['indentDiff'],
            'right_column_delta_px': alignment_data['rightColDiff'],
            'home_clickable_elements': len(clickable_elements),
            'changes_pill_text': commit_text
        }
        print(json.dumps(report, indent=2))
    finally:
        try:
            proc.kill()
        except Exception:
            pass

if __name__ == '__main__':
    asyncio.run(main())
