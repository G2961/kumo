"""One-off: real touch-tap on the home mascot vs programmatic click; hit-testing probe."""
import asyncio
from playwright.async_api import async_playwright

URL = 'file:///D:/smokecoding/codex-mobile/opencode-android-new/app/src/main/assets/codex.html'

JS_SETUP = '''() => {
 S.sid=null;render();
 const k = document.getElementById('kwKumo');
 const r = k.getBoundingClientRect();
 return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2), w: Math.round(r.width), h: Math.round(r.height), cur: kwCur};
}'''

JS_HIT = '''([x, y]) => {
 const el = document.elementFromPoint(x, y);
 const chain = [];
 let n = el;
 while (n && n.id !== 'app') { chain.push(n.tagName + (n.id ? '#' + n.id : '') + (n.className && typeof n.className === 'string' ? '.' + n.className.split(' ').join('.') : '')); n = n.parentElement; }
 return {hit: chain.join(' < '), hasOnclick: !!document.getElementById('kwKumo').onclick, cur: kwCur};
}'''

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel='chrome')
        iphone = await b.new_context(viewport={'width': 424, 'height': 900}, color_scheme='dark', has_touch=True, is_mobile=True)
        p = await iphone.new_page()
        await p.goto(URL)
        await p.wait_for_timeout(600)
        pos = await p.evaluate(JS_SETUP)
        print('mascot box:', pos)
        await p.wait_for_timeout(1500)  # let all FLIPs settle
        diag = await p.evaluate('''() => {
          const n = document.getElementById('kwKumo');
          const r = n.getBoundingClientRect();
          return {parent: n.parentElement && (n.parentElement.id || n.parentElement.tagName),
                  rect: {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)},
                  anims: n.getAnimations().length,
                  display: getComputedStyle(n).display};
        }''')
        print('settled diag:', diag)
        pos = await p.evaluate(JS_SETUP)
        hit = await p.evaluate(JS_HIT, [pos['x'], pos['y']])
        print('hit test:', hit)
        print('hit test:', hit)
        # real touch tap (like a finger), NOT el.click()
        await p.touchscreen.tap(pos['x'], pos['y'])
        await p.wait_for_timeout(200)
        print('after touch tap, cur:', await p.evaluate('kwCur'))
        await p.touchscreen.tap(pos['x'], pos['y'])
        await p.wait_for_timeout(200)
        print('after 2nd touch tap, cur:', await p.evaluate('kwCur'))
        await p.screenshot(path='tests/evidence/tap_emotions.png')
        await b.close()

asyncio.run(main())
