"""One-off probe: reuse the real stream_close flow, break down end-state-clean. Not a proof."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, 'tests')
import work_polish_proof as wpp
from playwright.async_api import async_playwright
from reasoning_activity_proof import A1, HTML, boot_page, launch_browser, seed, user, message
from work_log_proof import close_work, open_work, settle, work_id


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark', source=HTML.read_text(encoding='utf-8'))
        await page.set_viewport_size({'width': 412, 'height': 800})
        await page.emulate_media(reduced_motion='no-preference')
        await page.evaluate('scale=>{lset("oc_font_scale",scale);appearance()}', 1.0)
        await page.evaluate(wpp.INSTALL_SAMPLER)
        await settle(page)
        spec = await wpp.prepare(page, 'local', live=True)
        await wpp.click_state(page, spec, True)
        await page.locator(spec.button).scroll_into_view_if_needed()
        await page.evaluate('spec=>polishArm(spec)', spec.data())
        await page.locator(spec.button).click()
        await page.evaluate('performance.now()-polishTrace.clickAt')
        await page.evaluate('ev=>onEvent(ev)', {'type': 'message.part.updated', 'properties': {
            'part': message(A1, 'assistant', [wpp.part_for('local', True, '\nSTREAM_DELTA')])['parts'][0]}})
        trace = await page.evaluate('()=>polishDone')
        frames = trace['frames']
        print('injected/close frames tail:', [(f['ms'], round(f['height'], 1), round(f['reveal'], 1)) for f in frames[-6:]])
        raw = await page.locator(spec.row + ' .tool-output').text_content()
        print('raw ok:', raw == wpp.OUTPUT + '\nSTREAM_DELTA')
        await close_work(page)
        await open_work(page, work_id(A1, 'local'))
        await wpp.wait_rest(page)
        final = await wpp.read_disclosure(page, spec)
        print('reopen disclosure:', {k: final[k] for k in ('height', 'reveal', 'logical', 'physicalOpen')})
        detail = await page.locator(spec.row).evaluate('''row=>{
          const content=row.querySelector(':scope>.activity-body,:scope>.change-preview');
          return {rowConnected:row.isConnected,hold:row.hasAttribute('data-disclosure-hold'),
            target:null!==content&&content.hasAttribute('data-disclosure-target'),
            contentExists:null!==content,running:row.getAnimations({subtree:true}).filter(a=>a.playState==='running').length,
            open:row.open,closed:row.hasAttribute('data-closed'),auto:row.hasAttribute('data-auto'),
            contentDisplay:null===content?null:getComputedStyle(content).display,
            outer:row.outerHTML.slice(0,220)};
        }''')
        print('end-state breakdown:', json.dumps(detail, indent=1))
        await browser.close()

asyncio.run(main())
