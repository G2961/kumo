import asyncio, json, subprocess, sys, urllib.request
sys.path.insert(0, 'tests')
import ui_round3 as u
from playwright.async_api import async_playwright


async def main():
    port = u.free_port()
    origin = f'http://127.0.0.1:{port}'
    out = open(u.TASK.parent / 'trace.log', 'w', encoding='utf-8')
    server = subprocess.Popen([u.OPENCODE, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                              cwd=u.TASK, stdout=out, stderr=out)
    try:
        await u.wait_server(origin)
        async with async_playwright() as pw:
            browser, context, page, errors = await u.boot_page(pw, origin, 'dark')
            captured = []
            async def cap(route):
                captured.append(route.request.post_data_json)
                await route.fulfill(status=204)
            await page.route('**/prompt_async*', cap)
            await page.evaluate("""()=>{nativeCall=async(a,d)=>({saved:true});window.__req=[];
              const _api=api;api=async(p,o)=>{window.__req.push((o&&o.method||'GET')+' '+p.slice(0,60));return _api(p,o)};
              S.connections=[{id:'c1',name:'aeza',host:'192.0.2.9',port:22,user:'root',directory:''}];S.connectionID='c1';chips();
              document.querySelector('#inp').value='дарова';}""")
            await page.click('#send')
            await page.wait_for_timeout(3000)
            state = await page.evaluate("({req:window.__req,sid:S.sid,lastErr:S.lastErr,errors:S.errors.slice(-2),title:document.querySelector('#title').textContent})")
            print('REQ:', json.dumps(state['req'], ensure_ascii=False))
            print('SID:', state['sid'], 'LASTERR:', state['lastErr'], 'ERRORS:', state['errors'], 'TITLE:', state['title'])
            print('PROMPT BODY:', json.dumps(captured, ensure_ascii=False, indent=1)[:1600])
            await browser.close()
    finally:
        server.terminate()
        out.close()

asyncio.run(main())
