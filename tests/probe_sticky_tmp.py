"""One-off probe verify: no scroll jump on reveal + sticky summary works. Not a proof."""
import asyncio, sys
from playwright.async_api import async_playwright
sys.path.insert(0, 'tests')
from reasoning_activity_proof import A1, U1, activity_segment, boot_page, launch_browser, message, reason, seed, text, tool, user
from work_log_proof import BODY, open_work, work_id

OUTPUT = '\n'.join(f'line {i}: stdout' for i in range(40))
PATCH = '@@ -1,2 +1,44 @@\n ctx\n' + '\n'.join(f'+added line {i} with some content here' for i in range(42))


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark')
        parts = [
            tool('write1', 'write', {'filePath': '.dnsgo-proxy.js'}, PATCH, metadata={'diff': PATCH}),
            tool('cmd1', 'bash', {'command': 'node .dnsgo-proxy.js & sleep 2; node -e "1"'}, OUTPUT, metadata={'exitCode': 0}),
            tool('cmd2', 'bash', {'command': 'curl -sL http://127.0.0.1:4873/health'}, OUTPUT, metadata={'exitCode': 0}),
        ]
        await seed(page, [user(), message(A1, 'assistant', [reason('r0', 'thinking'), *parts, text('a', 'done')], U1, 9000)])
        await open_work(page, work_id(A1, 'r0'))
        row_sel = BODY + ' ' + activity_segment(A1, 'write1')

        result = await page.evaluate('''async sel=>{
          const body=document.querySelector('#workLogBody');
          const row=document.querySelector(sel);
          const label=row.querySelector('.activity-label');
          const bodyTop=body.getBoundingClientRect().top;
          const out={};
          const geo=()=>({dialogTop:document.querySelector('#workLogDialog').getBoundingClientRect().top,
            bodyTop:body.getBoundingClientRect().top,bodyClientH:body.clientHeight,
            labelTop:label.getBoundingClientRect().top-body.getBoundingClientRect().top,
            scrollTop:body.scrollTop});
          body.scrollTop=label.getBoundingClientRect().top-body.getBoundingClientRect().top-44;
          out.before=geo();
          row.querySelector(':scope>summary').click();
          await new Promise(r=>setTimeout(r,500));
          out.afterReveal=geo();
          out.headlineVisible=label.getBoundingClientRect().top>=body.getBoundingClientRect().top;
          // Now scroll far down through the details: sticky must pin the headline.
          body.scrollTop=body.scrollHeight;
          await new Promise(r=>setTimeout(r,100));
          const sum=row.querySelector(':scope>summary').getBoundingClientRect();
          out.afterScroll={summaryPinnedTop:Math.round(sum.top-body.getBoundingClientRect().top),
            pinned:Math.abs(sum.top-body.getBoundingClientRect().top)<20};
          return out;
          return out;
        }''', row_sel)
        print('RESULT:', result)
        await page.screenshot(path='tests/evidence/probe_sticky_scroll.png')
        await browser.close()

asyncio.run(main())
