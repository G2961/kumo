import asyncio, json, subprocess, sys, urllib.request
sys.path.insert(0, 'tests')
import ui_round3 as u
from playwright.async_api import async_playwright


async def main():
    port = u.free_port()
    origin = f'http://127.0.0.1:{port}'
    out = open(u.TASK.parent / 'probe2.log', 'w', encoding='utf-8')
    server = subprocess.Popen([u.OPENCODE, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                              cwd=u.TASK, stdout=out, stderr=out)
    try:
        await u.wait_server(origin)
        gdirs = []
        for gi in range(3):
            gd = u.TASK / f'p{gi}'
            gd.mkdir()
            gdirs.append(gd)
            subprocess.run(['git', '-C', str(gd), 'init', '-q'], check=True, capture_output=True)
        for gi, (gd, n) in enumerate(zip(gdirs, [7, 3, 2])):
            for j in range(n):
                title = 'Zebra cross-project' if (gi == 2 and j == 0) else f'G thread {gi}-{j}'
                import urllib.parse
                url = origin + '/session?' + urllib.parse.urlencode({'directory': str(gd)})
                req = urllib.request.Request(url, data=json.dumps({'title': title}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=15):
                    pass
        async with async_playwright() as pw:
            browser, context, page, errors = await u.boot_page(pw, origin, 'dark')
            await page.evaluate(f"""async () => {{
              S.savedProjects={json.dumps([str(d) for d in gdirs])};lset('oc_projects',S.savedProjects);
              S.projects=sortProjects(S.savedProjects.map(w=>({{worktree:w}})));
              S.projects.forEach(p=>S.expandedFolders.add(p.worktree));
              renderDrawer();drawer(true);
              await Promise.all(S.projects.map(p=>loadFolderSessions(p.worktree)));
              renderDrawer();
            }}""")
            await page.wait_for_timeout(500)
            geo = await page.evaluate("""()=>{const g=(s)=>{const e=document.querySelector(s);if(!e)return null;const r=e.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]};
              return {drawer:g('#drawer'),header:g('.drawer-header'),topbar:g('.drawer-top-bar'),search:g('#drawer [data-a=\"search\"]'),projects:g('#drawerProjects'),bottom:g('.drawer-bottom'),scroll:document.querySelector('#drawerProjects').scrollTop,searchBtnTop:document.querySelector('#drawer [data-a=\"search\"]').outerHTML.slice(0,120)}}""")
            print(json.dumps(geo, indent=1))
            await page.screenshot(path=str(u.EVIDENCE / 'probe-drawer.png'))
            await browser.close()
    finally:
        server.terminate()
        out.close()

asyncio.run(main())
