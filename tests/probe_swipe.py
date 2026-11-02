import asyncio, json, socket, subprocess, sys, urllib.request
sys.path.insert(0, 'tests')
import ui_round3 as u
from playwright.async_api import async_playwright


async def main():
    port = u.free_port()
    origin = f'http://127.0.0.1:{port}'
    out = open(u.TASK.parent / 'probe.log', 'w', encoding='utf-8')
    server = subprocess.Popen([u.OPENCODE, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                              cwd=u.TASK, stdout=out, stderr=out)
    try:
        await u.wait_server(origin)
        async with async_playwright() as pw:
            browser, context, page, errors = await u.boot_page(pw, origin, 'dark')
            await page.click('#cEnv')
            await page.wait_for_timeout(300)
            diag = await page.evaluate("""() => {
              const micro={};
              const d=document.createElement('div');
              d.ontouchstart=()=>{micro.pp=(micro.pp||0)+1};
              d.addEventListener('touchstart',()=>{micro.al=(micro.al||0)+1});
              d.dispatchEvent(new TouchEvent('touchstart',{bubbles:true}));
              d.dispatchEvent(new MouseEvent('click',{bubbles:true}));
              d.onclick=()=>{micro.ppClick=(micro.ppClick||0)+1};
              d.dispatchEvent(new MouseEvent('click',{bubbles:true}));
              micro.propertyHandlerWorks=!!d.ontouchstart;
              return micro;
            }""")
            print(json.dumps(diag, indent=1))
            await browser.close()
    finally:
        server.terminate()
        out.close()

asyncio.run(main())
