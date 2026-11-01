"""Proof of sidebar stability, accordion expansion, lazy loading, and title resolution."""
import asyncio
import base64
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

async def main():
    task_dir = ROOT / 'tests' / 'temp_sidebar_test'
    task_dir.mkdir(exist_ok=True)
    evidence_dir = ROOT / 'tests' / 'evidence'
    evidence_dir.mkdir(exist_ok=True)

    # 3 distinct projects for testing alphabetical stability: alpha, beta, gamma
    proj_a = task_dir / 'project-alpha'; proj_a.mkdir(exist_ok=True)
    proj_b = task_dir / 'project-beta'; proj_b.mkdir(exist_ok=True)
    proj_c = task_dir / 'project-gamma'; proj_c.mkdir(exist_ok=True)

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        oc_port = s.getsockname()[1]

    env = dict(os.environ)
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'provider': {}})

    proc = subprocess.Popen(
        ['D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe', 'serve', '--hostname', '127.0.0.1', '--port', str(oc_port)],
        env=env, cwd=proj_a, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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

        # Create sessions in project-alpha with default titles and real user messages
        def create_session_with_message(directory, prompt_text):
            r = urllib.request.Request(
                origin + f'/session?directory={directory}',
                data=json.dumps({}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            sess = json.load(urllib.request.urlopen(r))
            sid = sess['id']
            # Send prompt_async with user message
            prompt_body = {'parts': [{'type': 'text', 'text': prompt_text}], 'agent': 'build'}
            p_req = urllib.request.Request(
                origin + f'/session/{sid}/prompt_async?directory={directory}',
                data=json.dumps(prompt_body).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            try:
                urllib.request.urlopen(p_req)
            except Exception:
                pass
            return sid, sess.get('title')

        sid_a1, title_a1 = create_session_with_message(proj_a, 'Refactor login auth controller')
        sid_c1, title_c1 = create_session_with_message(proj_c, 'Fix memory leak in buffer pool')
        sid_c2, title_c2 = create_session_with_message(proj_c, 'Implement dark mode theme toggle')

        encoded = base64.urlsafe_b64encode(str(proj_a).encode()).decode().rstrip('=')

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
            await page.wait_for_function('S.spec && S.live')

            # Add projects beta and gamma to S.savedProjects
            await page.evaluate(f'''async () => {{
                S.savedProjects = ['{proj_c.as_posix()}', '{proj_b.as_posix()}', '{proj_a.as_posix()}'];
                lset('oc_projects', S.savedProjects);
                await loadProjects();
            }}''')

            # -------------------------------------------------------------
            # Check 1: Stable alphabetical order in drawer
            # -------------------------------------------------------------
            print('--- Check 1: Open drawer & verify stable project order ---')
            await page.click('#menu')
            await page.wait_for_selector('#drawer.on')

            titles = await page.locator('#drawer .proj-title').all_inner_texts()
            print('Project folder titles in drawer (sample):', titles[:10])
            test_titles = [t for t in titles if t in ['project-alpha', 'project-beta', 'project-gamma']]
            print('Test project folder titles:', test_titles)
            assert test_titles == ['project-alpha', 'project-beta', 'project-gamma'], f'Expected sorted order, got {test_titles}'
            results['checks'].append('stable_sorted_project_order')

            # -------------------------------------------------------------
            # Check 2: Tapping bottom folder does NOT reorder or move it
            # -------------------------------------------------------------
            print('--- Check 2: Tapping bottom folder does not move it or switch project ---')
            gamma_group = page.locator(f'.proj-group[data-dir="{proj_c.as_posix()}"]')
            gamma_header = gamma_group.locator('.proj-h')

            # Ensure into view first so Playwright click does not trigger initial scroll
            await gamma_header.scroll_into_view_if_needed()
            await page.wait_for_timeout(100)

            # Measure offsetTop and bounding box
            top_before = await gamma_header.evaluate('el => el.offsetTop')
            box_before = await gamma_header.bounding_box()
            current_dir_before = await page.evaluate('S.dir')

            # Tap on bottom folder
            await gamma_header.click()
            await page.wait_for_timeout(350)

            # Verify active project did NOT switch!
            current_dir_after = await page.evaluate('S.dir')
            assert current_dir_after == current_dir_before, f'Tapping folder must NOT switch active project! {current_dir_after} vs {current_dir_before}'

            # Verify order of folders did NOT change
            titles_after = await page.locator('#drawer .proj-title').all_inner_texts()
            test_titles_after = [t for t in titles_after if t in ['project-alpha', 'project-beta', 'project-gamma']]
            assert test_titles_after == ['project-alpha', 'project-beta', 'project-gamma'], f'Folder order changed: {test_titles_after}'

            # Verify bottom folder header position did NOT jump
            top_after = await gamma_header.evaluate('el => el.offsetTop')
            assert top_before == top_after, f'Folder offsetTop changed! {top_before} vs {top_after}'
            box_after = await gamma_header.bounding_box()
            assert abs(box_before['y'] - box_after['y']) < 2.0, f'Folder moved! Y before: {box_before["y"]}, after: {box_after["y"]}'

            # Verify chevron rotated
            caret_open = await gamma_header.locator('.caret.open').count()
            assert caret_open == 1, 'Caret should have rotated (.open)'
            results['checks'].append('tapping_bottom_folder_stays_in_place_without_reordering')

            # -------------------------------------------------------------
            # Check 3: Lazy loading sessions and clean title resolution
            # -------------------------------------------------------------
            print('--- Check 3: Lazy loading and title resolution from user prompt ---')
            # Wait for sessions in gamma to be loaded
            await page.wait_for_selector(f'.proj-inner[data-dir="{proj_c.as_posix()}"] button.th', timeout=10000)

            # Check that titles show user prompt instead of "New session - date"
            session_titles = await gamma_group.locator('.th-title').all_inner_texts()
            print('Gamma session titles in sidebar:', session_titles)
            assert any('Fix memory leak in buffer pool' in t for t in session_titles), f'Expected prompt text in titles, got {session_titles}'
            assert any('Implement dark mode theme toggle' in t for t in session_titles), f'Expected prompt text in titles, got {session_titles}'
            assert not any('New session - ' in t for t in session_titles), f'Found raw timestamp title: {session_titles}'
            results['checks'].append('first_message_title_resolution')

            # -------------------------------------------------------------
            # Check 4: Tap another folder to toggle accordion smoothly
            # -------------------------------------------------------------
            print('--- Check 4: Accordion toggle on beta ---')
            beta_group = page.locator(f'.proj-group[data-dir="{proj_b.as_posix()}"]')
            beta_header = beta_group.locator('.proj-h')
            await beta_header.click()
            await page.wait_for_timeout(350)
            assert await beta_header.locator('.caret.open').count() == 1
            # Gamma is still open (or toggleable) and still at the bottom
            titles_check4 = await page.locator('#drawer .proj-title').all_inner_texts()
            test_titles_check4 = [t for t in titles_check4 if t in ['project-alpha', 'project-beta', 'project-gamma']]
            assert test_titles_check4 == ['project-alpha', 'project-beta', 'project-gamma']
            results['checks'].append('accordion_smooth_expansion')

            # -------------------------------------------------------------
            # Check 5: Active project switches when opening session
            # -------------------------------------------------------------
            print('--- Check 5: Switch active project via session click ---')
            gamma_session_btn = gamma_group.locator('button.th', has_text='Fix memory leak in buffer pool').first
            await gamma_session_btn.click()

            # Drawer should close
            await page.wait_for_selector('#drawer:not(.on)')
            await page.wait_for_function('d => S.dir && S.dir.replace(/\\\\/g, "/") === d', arg=proj_c.as_posix())
            await page.wait_for_function('() => document.querySelector("#title").textContent !== "New thread"')

            # Active project must now be project-gamma!
            new_active_dir = await page.evaluate('S.dir')
            assert new_active_dir.replace('\\', '/') == proj_c.as_posix(), f'Expected active project to switch to gamma, got {new_active_dir}'
            header_title = await page.locator('#title').inner_text()
            print(f'Header title after session open: "{header_title}"')
            assert 'Fix memory leak in buffer pool' in header_title
            results['checks'].append('active_project_switches_on_session_open')

            # -------------------------------------------------------------
            # Check 6: Active project switches on New thread in folder
            # -------------------------------------------------------------
            print('--- Check 6: New thread in specific folder ---')
            await page.click('#menu')
            await page.wait_for_selector('#drawer.on')

            # Click '+' on project-beta
            beta_plus = beta_group.locator('button[data-a="new-in-dir"]')
            await beta_plus.click()

            # Drawer should close
            await page.wait_for_selector('#drawer:not(.on)')
            beta_active_dir = await page.evaluate('S.dir')
            assert beta_active_dir.replace('\\', '/') == proj_b.as_posix(), f'Expected active project to switch to beta, got {beta_active_dir}'
            home_proj = await page.locator('.home .proj').inner_text()
            print(f'Home project after new thread: "{home_proj}"')
            assert 'project-beta' in home_proj
            results['checks'].append('active_project_switches_on_new_thread_in_folder')

            # -------------------------------------------------------------
            # Check 7: Proof screenshot of accordion
            # -------------------------------------------------------------
            print('--- Check 7: Screenshot of sidebar accordion ---')
            await page.click('#menu')
            await page.wait_for_selector('#drawer.on')
            await page.wait_for_timeout(350)
            await page.screenshot(path=str(evidence_dir / 'sidebar-accordion-proof.png'))
            results['checks'].append('sidebar_screenshot_saved')

            assert len(errors) == 0, f'Page errors encountered: {errors}'
            results['pageErrors'] = errors
            print('\nALL CHECKS PASSED!')
            print(json.dumps(results, indent=2))
    finally:
        proc.terminate()
        import shutil
        shutil.rmtree(task_dir, ignore_errors=True)

if __name__ == '__main__':
    asyncio.run(main())
