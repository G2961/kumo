"""Offline visual probe: strict reasoning/tool/reasoning/tool/answer sequence.

Uses the same real-asset fixture as reasoning_activity_proof.py; no API/model calls.
Evidence is written outside the repository in a printed temporary directory.
"""
import argparse
import asyncio
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

from reasoning_activity_proof import (A1, U1, activity_segment, assert_sequence, boot_page,
                                      launch_browser, message, open_thought, reason, seed, text, thought, tool, user)


async def main(browser_executable=None):
    out = Path(tempfile.mkdtemp(prefix='probe-think-'))
    print(f'Evidence: {out}', flush=True)
    async with async_playwright() as pw:
        browser = await launch_browser(pw, browser_executable)
        try:
            for theme in ['dark', 'light']:
                context, page, errors, requests = await boot_page(browser, theme)
                try:
                    read_out = '\n'.join(f'{i}: line {i} of index.html {{' for i in range(1, 60))
                    await seed(page, [user(), message(A1, 'assistant', [
                        reason('r1', 'Пользователь просит вопрос.\n\nПроверю структуру проекта.', 1000, 2500),
                        tool('t1', 'glob', {'pattern': '**/*'}, 'src/index.html\nsrc/app.js'),
                        reason('r2', 'Выберу уточнение по найденному файлу.', 2500, 4000),
                        tool('t2', 'read', {'filePath': 'index.html'}, read_out),
                        text('answer', 'Вот структура проекта.')], U1, 9000)])
                    await assert_sequence(page, [f'{A1}_r1_think', f'{A1}_t1_activity', f'{A1}_r2_think',
                                                 f'{A1}_t2_activity', 'text:Вот структура проекта.'])
                    await open_thought(page, thought(A1, 'r1'))
                    await open_thought(page, thought(A1, 'r2'))
                    await expect(page.locator('.think .rtext')).to_have_text([
                        'Пользователь просит вопрос.\n\nПроверю структуру проекта.',
                        'Выберу уточнение по найденному файлу.'])
                    await expect(page.locator('.think .card, .think .tool-event, .topic')).to_have_count(0)
                    await expect(page.locator('.think-indicator, .reason-step, .think-steps, .reasoning-details')).to_have_count(0)
                    await expect(page.locator('.think-label')).to_have_text(['Reasoning', 'Reasoning'])
                    await expect(page.locator(thought(A1, 'r1') + ' .think-duration')).to_have_text('2s')
                    await expect(page.locator(thought(A1, 'r2') + ' .think-duration')).to_have_text('2s')
                    await page.screenshot(path=str(out / f'{theme}-think-open.png'))
                    activity = page.locator(activity_segment(A1, 't2'))
                    await expect(activity.locator(':scope > summary')).not_to_contain_text('Activity')
                    await expect(activity.locator(':scope > summary')).to_contain_text('Read')
                    await activity.locator(':scope > summary').click()
                    read_card = activity
                    await expect(read_card).to_have_attribute('data-tool-part', 't2')
                    await expect(read_card.locator('details, .card')).to_have_count(0)
                    assert await read_card.locator('.tool-output').text_content() == read_out
                    gap = await read_card.evaluate('''e=>{
                      const summary=e.querySelector('summary').getBoundingClientRect();
                      return e.querySelector('.tool-output').getBoundingClientRect().top-summary.bottom;
                    }''')
                    assert gap >= -1, f'Read output overlaps its summary: {gap}px'
                    await page.screenshot(path=str(out / f'{theme}-activity-read-open.png'))
                    assert not errors, errors
                    assert not requests, requests
                    print(f'PASS {theme}: ordered per-part segments; read output below its own summary ({gap}px)')
                finally:
                    await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable', type=Path, help='Existing Chrome/Chromium (or PLAYWRIGHT_BROWSER_EXECUTABLE)')
    asyncio.run(main(parser.parse_args().browser_executable))
