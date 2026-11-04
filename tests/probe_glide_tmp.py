"""One-off probe: group disclosure glide (tall body) vs note. Not a proof."""
import asyncio, sys
from playwright.async_api import async_playwright
sys.path.insert(0, 'tests')
from reasoning_activity_proof import A1, U1, boot_page, launch_browser, message, reason, seed, text, tool, user
from work_log_proof import BODY, open_work, work_id

OUTPUT = '\n'.join(f'line {i}: stdout' for i in range(60))


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark')
        parts = [
            reason('r1', 'Run the server and check endpoints.'),
            tool('c1', 'bash', {'command': 'node server.js & sleep 2; curl -s localhost:3000/api/videos | head -c 120'}, OUTPUT, metadata={'exitCode': 0}),
            tool('c2', 'bash', {'command': 'node server.js >/dev/null 2>&1 & sleep 1'}, OUTPUT, metadata={'exitCode': 0}),
            text('a', 'done'),
        ]
        await seed(page, [user(), message(A1, 'assistant', parts, U1, 9000)])
        await open_work(page, work_id(A1, 'r1'))

        data = await page.evaluate('''async ()=>{
          const row=document.querySelector('#workLogBody .work-group');
          const content=row.querySelector(':scope>.activity-body');
          const anims=[];
          const orig=Element.prototype.animate;
          Element.prototype.animate=function(kf,opts){anims.push({from:kf[0].height,to:kf[1].height,dur:opts.duration});return orig.call(this,kf,opts)};
          const heights=[];
          row.querySelector(':scope>summary').click();
          for(let i=0;i<8;i++){heights.push(Math.round(content.getBoundingClientRect().height));await new Promise(r=>setTimeout(r,90))}
          Element.prototype.animate=orig;
          return {anims,heights,finalH:Math.round(content.getBoundingClientRect().height)};
        }''')
        print('GROUP:', data)
        await browser.close()

asyncio.run(main())
