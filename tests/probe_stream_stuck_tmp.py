"""One-off probe: mid-close SSE delta -> rebuild -> stuck disclosure attrs? Not a proof."""
import asyncio, json, sys
from playwright.async_api import async_playwright
sys.path.insert(0, 'tests')
from reasoning_activity_proof import A1, SID, U1, activity_segment, boot_page, event, launch_browser, message, seed, tool, user
from work_log_proof import BODY, close_work, open_work, work_id

OUTPUT = '\n'.join(f'line {i}: stdout' for i in range(40))


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark')

        async def part(suffix=''):
            return tool('local', 'list', {'path': 'src'}, OUTPUT + suffix)

        await seed(page, [user(), message(A1, 'assistant', [await part()], U1, 9000)], running=True)
        await open_work(page, work_id(A1, 'local'))
        row_sel = BODY + ' ' + activity_segment(A1, 'local')
        # Exact stream_close flow: disclosure is OPEN first, then the close click lands.
        await page.locator(row_sel + ' > summary').click()
        await page.wait_for_timeout(700)
        state = await page.evaluate('''async sel=>{
          const row=document.querySelector(sel),content=row.querySelector(':scope>.activity-body');
          row.querySelector(':scope>summary').click();
          await new Promise(r=>setTimeout(r,40));
          const mid={open:row.open,hold:row.hasAttribute('data-disclosure-hold'),
            target:content.hasAttribute('data-disclosure-target'),
            running:row.getAnimations({subtree:true}).filter(a=>a.playState==='running').length,
            connected:row.isConnected};
          return mid;
        }''', row_sel)
        print('mid-close (40ms after click):', state)
        await event(page, 'message.part.updated', part=(message(A1, 'assistant', [await part('\nSTREAM_DELTA')])['parts'][0]))
        await page.wait_for_timeout(700)
        print('post-stream (before close_work):', await page.evaluate('''sel=>{
          const row=document.querySelector(sel),content=row.querySelector(':scope>.activity-body');
          return {connected:row.isConnected,open:row.open,logical:row.querySelector(':scope>summary').getAttribute('aria-expanded'),
            hold:row.hasAttribute('data-disclosure-hold'),target:content.hasAttribute('data-disclosure-target'),
            running:row.getAnimations({subtree:true}).filter(a=>a.playState==='running').length,
            borders:getComputedStyle(row).borderColor};
        }''', row_sel))
        await close_work(page)
        await open_work(page, work_id(A1, 'local'))
        await page.wait_for_timeout(700)
        print('after reopen:', await page.evaluate('''sel=>{
          const row=document.querySelector(sel),content=row.querySelector(':scope>.activity-body');
          return {open:row.open,logical:row.querySelector(':scope>summary').getAttribute('aria-expanded'),
            hold:row.hasAttribute('data-disclosure-hold'),target:content.hasAttribute('data-disclosure-target'),
            running:row.getAnimations({subtree:true}).filter(a=>a.playState==='running').length,
            styleAttr:row.getAttribute('style'),contentStyle:content.getAttribute('style'),
            closedAttr:row.hasAttribute('data-closed'),autoAttr:row.hasAttribute('data-auto')};
        }''', row_sel))
        await browser.close()

asyncio.run(main())
