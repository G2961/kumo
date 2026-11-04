"""One-off visual probe: state stickers in work-summary, question sheet, error card."""
import asyncio
from playwright.async_api import async_playwright

ASSETS = r'D:\smokecoding\codex-mobile\opencode-android-new\app\src\main\assets'
URL = 'file:///' + ASSETS.replace('\\', '/') + '/codex.html'

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome')
        page = await browser.new_page(viewport={'width': 424, 'height': 900}, color_scheme='dark')
        await page.goto(URL)
        await page.wait_for_timeout(800)
        await page.evaluate('''() => {
          document.querySelector('#main').innerHTML =
            workSummaryHtml({id:'a',status:'running',label:'Fixing the swap storm',detail:'reading LocalRuntime.kt · 12s'}) +
            workSummaryHtml({id:'b',status:'waiting',label:'Waiting for your answer',detail:'question 1 of 2'}) +
            workSummaryHtml({id:'c',status:'error',label:'Could not reach provider',detail:'timeout · 401'}) +
            workSummaryHtml({id:'d',status:'stopped',label:'Aborted by user',detail:'1m 02s'}) +
            workSummaryHtml({id:'e',status:'done',label:'Edited 3 files',detail:'2m 14s'}) +
            errorCardHTML({name:'Error',data:{message:'provider rate limited: 429'}},'m1');
        }''')
        await page.wait_for_timeout(300)
        await page.screenshot(path='tests/evidence/stickers_feed.png')
        await page.evaluate('''() => {
          S.questions.set('q1',{id:'q1',questions:[{question:'Какой порт оставить для сервера?',multiple:false,options:[{label:'14100',description:'по умолчанию'},{label:'8080',description:'альтернатива'}]}]});
          renderQuestionSheet();
        }''')
        await page.wait_for_timeout(400)
        await page.screenshot(path='tests/evidence/stickers_qsheet.png')
        await browser.close()
        print('done')

asyncio.run(main())
