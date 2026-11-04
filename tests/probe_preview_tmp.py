"""One-off probe: stage note preview + expand. Not a proof."""
import asyncio, sys
from playwright.async_api import async_playwright
sys.path.insert(0, 'tests')
from reasoning_activity_proof import A1, U1, boot_page, launch_browser, message, reason, seed, text, tool, user
from work_log_proof import BODY, open_work, work_id

REASON = 'Looking at the server structure first. Then I will patch the Range crash and review uploads.'


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark')
        parts = [reason('r1', REASON), tool('read1', 'read', {'filePath': 'server.js'}, 'CODE'), text('a', 'done')]
        await seed(page, [user(), message(A1, 'assistant', parts, U1, 9000)])
        await open_work(page, work_id(A1, 'r1'))

        before = await page.evaluate('''()=>{
          const note=document.querySelector('#workLogBody .stage-note');
          const t=note.querySelector('.stage-text');
          return {preview:t.textContent, expanded:note.hasAttribute('data-open'),
                  bodyRows:note.querySelector('.stage-body').getBoundingClientRect().height};
        }''')
        print('BEFORE:', before)
        await page.locator(BODY + ' .stage-head').click()
        await page.wait_for_timeout(400)
        after = await page.evaluate('''()=>{
          const note=document.querySelector('#workLogBody .stage-note');
          const body=note.querySelector('.stage-body');
          return {expanded:note.hasAttribute('data-open'), aria:note.querySelector('.stage-head').getAttribute('aria-expanded'),
                  bodyH:body.getBoundingClientRect().height,
                  fullText:body.querySelector('.rtext').textContent.length};
        }''')
        print('AFTER tap:', after)
        await page.screenshot(path='tests/evidence/preview_probe.png')
        await browser.close()

asyncio.run(main())
