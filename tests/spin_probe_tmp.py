"""One-off: spinner phase continuity across stream-like repaints."""
import asyncio
from playwright.async_api import async_playwright

URL = 'file:///D:/smokecoding/codex-mobile/opencode-android-new/app/src/main/assets/codex.html'

JS = '''async () => {
 const sleep = ms => new Promise(r => setTimeout(r, ms));
 S.sid = 't1'; S.running = true; renderMsgs(); kwGo(); S.running = true;
 onEvent({type:'message.part.updated',properties:{part:{id:'p1',messageID:'m1',sessionID:'t1',type:'tool',tool:'bash',state:{status:'running',input:{command:'npm run dev'},title:'Run server.js'}}}});
 await sleep(400);
 const ct = () => { const el = document.querySelector('#feed .spin'); const a = el && el.getAnimations()[0]; return a ? Math.round(a.currentTime || 0) : -1; };
 const c1 = ct();
 const trace = [];
 const prog = () => { const el = document.querySelector('#feed .spin'); const a = el && el.getAnimations()[0]; if (!a) return -1; const t = a.effect.getComputedTiming(); return (t.progress != null ? t.progress : (a.currentTime + t.delay) / 800 % 1); };
 for (let i = 0; i < 5; i++) { renderMsgs(); const pr = prog(); const rp = ((performance.now() - kwSince) / 800) % 1; trace.push({i, pr: Math.round(pr * 100), rp: Math.round(rp * 100), err: Math.round((pr - rp) * 360)}); await sleep(120); }
 const c2 = ct();
 const realPhase = (performance.now() - kwSince) % 800;
 const maxErr = Math.max(...trace.map(t => Math.abs(t.err)));
 return {c1, c2, realPhase: Math.round(realPhase), maxErrDeg: maxErr, trace};
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
