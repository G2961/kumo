"""One-off visual probe: kumo wait mascot v9 wired to synthetic real-event flow."""
import asyncio
from playwright.async_api import async_playwright

URL = 'file:///D:/smokecoding/codex-mobile/opencode-android-new/app/src/main/assets/codex.html'
EV = 'tests/evidence'

async def shot(page, name):
    await page.screenshot(path=f'{EV}/kw_{name}.png')

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome')
        page = await browser.new_page(viewport={'width': 424, 'height': 900}, color_scheme='dark')
        await page.goto(URL)
        await page.wait_for_timeout(700)
        await shot(page, 'home')
        # open a thread and start a turn
        await page.evaluate('''() => {S.sid='t1';renderMsgs();kwGo();S.running=true}''')
        await page.wait_for_timeout(3200)
        await shot(page, 'wait_stars')
        # agent reads files (tool running + ok)
        await page.evaluate('''() => {onEvent({type:'message.part.updated',properties:{part:{id:'p1',messageID:'m1',sessionID:'t1',type:'tool',tool:'read',state:{status:'running',input:{filePath:'/storage/emulated/0/cc/wptube/player.js'}}}}})}''')
        await page.wait_for_timeout(500)
        await shot(page, 'tool_run')
        await page.evaluate('''() => {onEvent({type:'message.part.updated',properties:{part:{id:'p1',messageID:'m1',sessionID:'t1',type:'tool',tool:'read',state:{status:'completed',input:{filePath:'/x'},output:'ok'}}}});onEvent({type:'message.part.updated',properties:{part:{id:'p2',messageID:'m1',sessionID:'t1',type:'tool',tool:'bash',state:{status:'running',input:{command:'npm test'}}}}})}''')
        await page.wait_for_timeout(400)
        await shot(page, 'tool_ok_next')
        # text starts streaming
        await page.evaluate('''() => {onEvent({type:'message.part.delta',properties:{sessionID:'t1',messageID:'m1',partID:'p3',field:'text',delta:'Готово. Таймер теперь гасится.\\n'}});onEvent({type:'message.part.delta',properties:{sessionID:'t1',messageID:'m1',partID:'p3',field:'text',delta:'**Что поменялось**\\n• player.js: очистка таймера в close()'}})}''')
        await page.wait_for_timeout(900)
        await shot(page, 'saying')
        # finish the turn
        await page.evaluate('''() => {onEvent({type:'session.idle',properties:{sessionID:'t1'}})}''')
        await page.wait_for_timeout(900)
        await shot(page, 'done')
        # second turn: silence -> tool fail -> session error
        # second turn: silence -> tool fail -> session error
        await page.evaluate('''() => {kwGo();S.running=true}''')
        await page.evaluate('''() => {onEvent({type:'message.part.updated',properties:{part:{id:'p4',messageID:'m2',sessionID:'t1',type:'tool',tool:'edit',state:{status:'error',input:{filePath:'player.js'},error:'boom'}}}})}''')
        await page.wait_for_timeout(500)
        await shot(page, 'fail')
        await page.evaluate('''() => {onEvent({type:'session.error',properties:{sessionID:'t1',error:{name:'Error',data:{message:'provider rate limited'}}}})}''')
        await page.wait_for_timeout(1600)
        await shot(page, 'error_done')
        # stop mid-run
        await page.evaluate('''() => {kwGo();S.running=true}''')
        await page.wait_for_timeout(800)
        await page.evaluate('''() => {kwStop()}''')
        await page.wait_for_timeout(1100)
        await shot(page, 'stopped')
        # pill: start again, scroll up
        await page.evaluate('''() => {kwGo();S.running=true;$('#main').scrollTop=0}''')
        await page.wait_for_timeout(700)
        await shot(page, 'pill')
        # peek: 30s of silence in wait phase
        await page.evaluate('''() => {onEvent({type:'session.idle',properties:{sessionID:'t1'}});kwGo();S.running=true}''')
        await page.wait_for_timeout(31000)
        await shot(page, 'peek')
        await browser.close()
        print('done')

asyncio.run(main())
