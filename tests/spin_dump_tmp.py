"""One-off: dump animations on a .spin node right after renderMsgs."""
import asyncio
from playwright.async_api import async_playwright

URL = 'file:///D:/smokecoding/codex-mobile/opencode-android-new/app/src/main/assets/codex.html'

JS = '''async () => {
 const sleep = ms => new Promise(r => setTimeout(r, ms));
 S.sid = 't1'; S.running = true; renderMsgs(); kwGo(); S.running = true;
 onEvent({type:'message.part.updated',properties:{part:{id:'p1',messageID:'m1',sessionID:'t1',type:'tool',tool:'bash',state:{status:'running',input:{command:'npm run dev'},title:'Run server.js'}}}});
 await sleep(500);
 renderMsgs();
 const el = document.querySelector('#feed .spin');
 const dump = tag => el.getAnimations().map(a => ({tag, cls: a.constructor.name, ct: Math.round(a.currentTime || 0), delay: a.effect ? a.effect.getComputedTiming().delay : null, iter: a.effect ? a.effect.getComputedTiming().iterations : null, styleAnim: getComputedStyle(el).animationName}));
 const before = dump('before');
 el.style.animation = 'none';
 el.animate([{transform:'rotate(0deg)'},{transform:'rotate(360deg)'}], {duration:800, iterations:Infinity, delay:-400, easing:'linear'});
 const after = dump('after');
 await sleep(100);
 const later = dump('later100');
 return {before, after, later};
}'''

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel='chrome')
        p = await b.new_page(viewport={'width': 424, 'height': 900}, color_scheme='dark')
        await p.goto(URL)
        await p.wait_for_timeout(600)
        print(await p.evaluate(JS))
        await b.close()

asyncio.run(main())
