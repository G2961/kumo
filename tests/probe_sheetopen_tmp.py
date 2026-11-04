"""One-off probe: what does the sheet look like right after tapping the summary? Not a proof."""
import asyncio, sys
from playwright.async_api import async_playwright
sys.path.insert(0, 'tests')
from reasoning_activity_proof import A1, U1, boot_page, launch_browser, message, reason, seed, text, tool, user
from work_log_proof import SUMMARY, work_id

OUTPUT = '\n'.join(f'line {i}: stdout' for i in range(40))
PATCH = '@@ -1,2 +1,12 @@\n ctx\n' + '\n'.join(f'+line {i}' for i in range(10))


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark')
        parts = [reason('r1', 'Think'), tool('read1', 'read', {'filePath': 'server.js'}, 'CODE'), text('a', 'done')]
        await seed(page, [user(), message(A1, 'assistant', parts, U1, 9000)])

        rows = await page.evaluate('''async sel=>{
          const btn=document.querySelector(sel);
          const dialog=document.querySelector('#workLogDialog');
          const body=document.querySelector('#workLogBody');
          const samples=[];
          btn.click();
          for(const delay of [0,60,140,260,500]){
            await new Promise(r=>setTimeout(r,delay));
            const dR=dialog.getBoundingClientRect();
            samples.push({t:delay,open:dialog.open,entries:body.children.length,
              dialogH:Math.round(dR.height),dialogBottom:Math.round(dR.bottom),
              firstEntryH:body.children[0]?Math.round(body.children[0].getBoundingClientRect().height):0});
          }
          return samples;
        }''', SUMMARY)
        for s in rows: print(s)
        await browser.close()

asyncio.run(main())
