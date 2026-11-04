"""Offline Chrome proof of Auto-approve consent and real UI-selected submit modes.

Run with the existing Python venv and PLAYWRIGHT_BROWSER_EXECUTABLE pointing to
Chrome. Only boot/network/native transport are fixtures; all picks and submits
use real clicks against the live HTML. No provider, SSH, build or install runs.
"""
import argparse
import asyncio
import copy
import re

from playwright.async_api import async_playwright, expect
from reasoning_activity_proof import ORIGIN, boot_page, launch_browser
from ssh_optional_proof import Fixture, SID, WRITES, check_local, check_ssh, effective, rule

LABELS = {'plan': 'Plan', 'build': 'Ask first', 'auto': 'Auto-approve'}
MODES = list(LABELS)
ON = re.compile(r'\bon\b')


async def pick_mode(page, mode):
    # Hit the actual nested label/subtitle, not onclick(), dispatchEvent or S.agent.
    await page.locator('#cPerm > span').click()
    await expect(page.locator('#sheet h3')).to_have_text('Mode')
    await expect(page.locator('#sheet .opt')).to_have_count(3)
    choice = page.locator('#sheet .opt').last if mode == 'auto' else page.locator('#sheet .opt').nth(MODES.index(mode))
    await expect(choice).to_contain_text(LABELS[mode])
    await choice.locator('.m').click()


async def assert_closed(page):
    await expect(page.locator('#sheet')).not_to_have_class(ON)
    await expect(page.locator('#dim')).not_to_have_class(ON)


async def assert_warning(page):
    # Text alone is insufficient: the old bug left the confirmation in a closed sheet.
    await expect(page.locator('#sheet')).to_have_class(ON)
    await expect(page.locator('#dim')).to_have_class(ON)
    await expect(page.locator('#sheet h3')).to_have_text('Auto-approve')
    await expect(page.get_by_role('button', name='Enable', exact=True)).to_be_visible()
    await expect(page.get_by_role('button', name='Cancel', exact=True)).to_be_visible()
    await expect(page.locator('#sheet .note')).to_contain_text('including on SSH servers')


async def assert_mode(page, mode, warned, stored=True):
    await expect(page.locator('#cPerm > span')).to_have_text(LABELS[mode])
    assert await page.evaluate('S.agent') == mode
    assert await page.evaluate('LS("oc_agent")') == (mode if stored else None)
    assert await page.evaluate('LS("oc_auto_warned")') is warned
    assert await page.locator('#cPerm').evaluate("el=>el.classList.contains('danger')") == (mode == 'auto')


async def storage(page):
    return await page.evaluate('({...localStorage})')


async def persistent_page(browser, theme):
    context, seeded, errors, unexpected = await boot_page(browser, theme=theme)
    # boot_page's page-specific init script clears storage on every navigation.
    # A second page shares its isolated context/routes but has no reset script,
    # so reload below exercises production LS initialization, not re-seeding.
    page = await context.new_page()
    page.set_default_timeout(3000)
    page.set_default_navigation_timeout(15000)
    page.on('pageerror', lambda error: errors.append(str(error)))
    await seeded.close()
    await page.goto(ORIGIN + '/_ui/index.html')
    await page.evaluate('chips()')
    return context, page, errors, unexpected


async def reload_ui(page):
    await page.reload()
    await page.wait_for_function('typeof chips === "function"')
    # Offline boot is skipped, but S and its localStorage initialization are real.
    await page.evaluate('chips()')


async def consent(browser):
    for initial, theme in [('build', 'dark'), ('plan', 'light')]:
        context, page, errors, unexpected = await persistent_page(browser, theme)
        try:
            await assert_mode(page, 'build', None, stored=False)
            await pick_mode(page, initial)
            await assert_closed(page)
            await assert_mode(page, initial, None)
            before = await storage(page)
            for dismiss in ['Cancel', 'backdrop']:
                await pick_mode(page, 'auto')
                await assert_warning(page)
                await assert_mode(page, initial, None)
                await page.locator('#sheet h3').click()
                await assert_warning(page)
                assert await storage(page) == before
                if dismiss == 'Cancel':
                    await page.get_by_role('button', name='Cancel', exact=True).click()
                else:
                    await page.locator('#dim').click(position={'x': 10, 'y': 10})
                await assert_closed(page)
                assert await storage(page) == before
                await reload_ui(page)
                await assert_mode(page, initial, None)
                assert await storage(page) == before

            await pick_mode(page, 'auto')
            await assert_warning(page)
            assert await storage(page) == before
            await page.get_by_role('button', name='Enable', exact=True).click()
            await assert_closed(page)
            await assert_mode(page, 'auto', True)
            await reload_ui(page)
            await assert_mode(page, 'auto', True)

            # Current auto, Plan -> auto, and Ask first -> auto never ask again.
            for mode in ['auto', 'plan', 'auto', 'build', 'auto']:
                await pick_mode(page, mode)
                await assert_closed(page)
                await assert_mode(page, mode, True)
                await reload_ui(page)
                await assert_mode(page, mode, True)
            assert not errors, errors
            assert not unexpected, unexpected
        finally:
            await context.close()
        print(f'PASS consent_{initial}_{theme}: Cancel/backdrop unchanged across reload; Enable once; durable modes/warning', flush=True)


async def nested_options(browser):
    context, page, errors, unexpected = await boot_page(browser)
    started, release, loaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    patches = []
    models = {
        'reasoner': {'id': 'reasoner', 'name': 'Reasoner', 'capabilities': {'reasoning': True}, 'variants': {}},
        'plain': {'id': 'plain', 'name': 'Plain', 'capabilities': {}, 'variants': {}},
    }

    async def patch_config(route):
        assert route.request.method == 'PATCH'
        body = route.request.post_data_json
        patches.append(body)
        started.set()
        await release.wait()
        models['reasoner']['variants'] = body['provider']['fixture']['models']['reasoner']['variants']
        await route.fulfill(json={})

    async def providers(route):
        assert route.request.method == 'GET'
        await route.fulfill(json={'providers': [{'id': 'fixture', 'name': 'Fixture', 'models': models}]})
        loaded.set()

    await page.route('**/global/config*', patch_config)
    await page.route('**/config/providers*', providers)
    try:
        await page.evaluate('''models=>{
          S.provs=[{id:'fixture',name:'Fixture',models:Object.values(models)}];
          S.model={p:'fixture',m:'reasoner'};chips();
        }''', models)
        await page.locator('#cModel > span').click()
        await page.locator('#sheetEffort > span').first.click()
        await expect(page.locator('#sheet h3')).to_have_text('Reasoning effort')
        # Selected Default has a nested checkmark span, exercising delegation too.
        await page.locator('#sheet .opt').first.locator('.r').click()
        await assert_closed(page)
        assert await page.evaluate('S.efforts[modelKey()]') is None
        await page.locator('#cModel > span').click()
        await page.locator('#sheetEffort > span').first.click()
        await page.get_by_role('button', name='High', exact=True).click()
        await asyncio.wait_for(started.wait(), 5)
        await assert_closed(page)
        await expect(page.locator('#cEff > span')).to_have_text('High')

        # A new nested confirmation must survive the older async effort callback.
        await pick_mode(page, 'auto')
        await assert_warning(page)
        release.set()
        await asyncio.wait_for(loaded.wait(), 5)
        await page.wait_for_function('!!S.provs[0].models[0].variants.high')
        await assert_warning(page)
        await page.get_by_role('button', name='Cancel', exact=True).click()
        await assert_mode(page, 'build', None, stored=False)
        assert len(patches) == 1
        assert set(patches[0]) == {'provider'}
        assert await page.evaluate('LS("oc_efforts")[modelKey()]') == 'high'

        await page.locator('#cModel > span').click()
        await page.locator('#sheet [data-m="plain"]').click()
        await assert_closed(page)
        await expect(page.locator('#cModel > span')).to_have_text('Plain')
        await expect(page.locator('#cEff')).to_be_hidden()
        await page.locator('#cModel > span').click()
        await page.locator('#sheet [data-m="reasoner"]').click()
        await expect(page.locator('#cEff > span')).to_have_text('High')
        await page.locator('#cEff > span').click()
        await page.get_by_role('button', name='Default', exact=True).click()
        await assert_closed(page)
        await expect(page.locator('#cEff')).to_be_hidden()
        assert await page.evaluate('LS("oc_efforts")[modelKey()]') is None
        assert await page.evaluate('LS("oc_model")') == {'p': 'fixture', 'm': 'reasoner'}
        assert len(patches) == 1

        await page.locator('#menu').click()
        await page.locator('#drawer [data-a="settings"]').click()
        await page.locator('[data-sa="theme"] > span').nth(1).click()
        await page.get_by_role('button', name='Light', exact=True).click()
        await assert_closed(page)
        await expect(page.locator('#settingsScreen')).to_be_visible()
        await expect(page.locator('[data-sa="theme"] .m')).to_have_text('Light')
        assert await page.evaluate('LS("oc_theme")') == 'light'
        await expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        assert not errors, errors
        assert not unexpected, unexpected
    finally:
        release.set()
        await context.close()
    print('PASS nested model/effort/Default/theme clicks and delayed effort completion preserving confirmation', flush=True)


async def submit_modes(browser):
    guarded_config = {'permission': {
        **{tool: {'*': 'ask', 'protected/*': 'deny'} for tool in WRITES},
        'read': {'secrets/*': 'deny', 'sensitive/*': 'ask'},
        'bash': {'*': 'allow', 'rm *': 'deny'},
        'mobile_ssh_ssh_exec': {'*': 'ask', 'rm *': 'deny'},
        'mobile_ssh_ssh_write_file': {'*': 'ask', 'protected/*': 'deny'},
    }}
    for connected in [False, True]:
        for guarded in [False, True]:
            baseline = [rule('grep', 'deny', 'credentials/*')] if guarded else []
            f = await Fixture().start(browser, connected=connected,
                                      config=copy.deepcopy(guarded_config) if guarded else None,
                                      previous=baseline)
            try:
                warned = None
                for mode in ['build', 'auto', 'plan', 'build', 'auto']:
                    count = len(f.requests)
                    def assert_rules_patch(label):
                        # A mode change with a live session applies the ruleset immediately:
                        # the PATCH rides along with its baseline/config lookups, and
                        # prompt_async is never sent by a mode change.
                        new = f.requests[count:]
                        patches = [r for r in new if r['method'] == 'PATCH' and r['path'].endswith('/session/' + SID)]
                        assert len(patches) == 1 and 'permission' in (patches[0]['body'] or {}), f'{label}: {new!r}'
                        assert not any(r['path'].endswith('/prompt_async') for r in new), f'{label}: {new!r}'
                    await pick_mode(f.page, mode)
                    if mode == 'auto' and not warned:
                        await assert_warning(f.page)
                        await assert_mode(f.page, 'build', None)
                        assert len(f.requests) == count, 'Opening the warning must not send anything'
                        await f.page.get_by_role('button', name='Enable', exact=True).click()
                        warned = True
                    await assert_closed(f.page)
                    await assert_mode(f.page, mode, warned)
                    # The ruleset PATCH rides the same click handler; give the
                    # async fire-and-forget call a bounded moment to hit the wire.
                    for _ in range(40):
                        if len(f.requests) > count:
                            break
                        await f.page.wait_for_timeout(25)
                    assert_rules_patch('mode change')
                    body = await f.send(f'UI-selected {mode}: inspect local code; use SSH only if needed.')
                    assert body['agent'] == ('plan' if mode == 'plan' else 'build')
                    check_local(f.permission, mode)
                    check_ssh(f.permission, mode, connected)
                    if guarded:
                        for tool in WRITES:
                            assert effective(f.permission, tool, 'protected/main.js') == 'deny'
                        for tool, path in [('read', 'secrets/key'), ('bash', 'rm file'),
                                           ('grep', 'credentials/key'),
                                           ('mobile_ssh_ssh_exec', 'rm file'),
                                           ('mobile_ssh_ssh_write_file', 'protected/main.js')]:
                            assert effective(f.permission, tool, path) == 'deny', (mode, tool, path)
                        assert effective(f.permission, 'read', 'sensitive/key') == 'ask'
                await f.finish()
            finally:
                await f.context.close()
            print(f'PASS UI submit transitions build/auto/plan/build/auto: ssh={connected}, configured_denies={guarded}', flush=True)


async def main(cases):
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            for name, case in [('consent', consent), ('nested', nested_options), ('submit', submit_modes)]:
                if not cases or name in cases:
                    await case(browser)
        finally:
            await browser.close()
    print('PASS requested offline Auto-approve proofs', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=['consent', 'nested', 'submit'])
    asyncio.run(main(parser.parse_args().case))
