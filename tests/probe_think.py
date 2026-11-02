"""Visual probe: think block with reasoning text + tool entries (regression for
'thought text invisible' and 'tool output rendered side-by-side with the chip')."""
import asyncio
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ui_round3 as R  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = R.ROOT / 'tests' / 'evidence' / 'ui_round3'


async def main():
    R.TASK.mkdir(exist_ok=True)
    port = R.free_port()
    origin = f'http://127.0.0.1:{port}'
    out = open(R.TASK / 'opencode.log', 'w', encoding='utf-8')
    server = subprocess.Popen([R.OPENCODE, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                              cwd=R.TASK, stdout=out, stderr=out)
    try:
        await R.wait_server(origin)
        async with async_playwright() as pw:
            browser, context, page, errors = await R.boot_page(pw, origin, 'dark')
            read_out = '\n'.join(f'{i}: line {i} of index.html {{' for i in range(1, 60))
            await page.evaluate(f"""()=>{{
              S.sid='x';
              S.msgs.set('u1',{{info:{{id:'u1',sessionID:'x',role:'user'}},parts:new Map([['pu',{{id:'pu',type:'text',text:'задай мне вопрос'}}]])}});
              const a={{info:{{id:'a1',sessionID:'x',role:'assistant',time:{{created:1000,completed:9000}}}},parts:new Map()}};
              a.parts.set('r1',{{id:'r1',type:'reasoning',text:'Пользователь просит вопрос. Посмотрю структуру проекта, потом прочитаю index.html и сформулирую.',time:{{start:1000,end:2500}}}});
              a.parts.set('t1',{{id:'t1',type:'tool',tool:'glob',callID:'c1',state:{{status:'completed',input:{{pattern:'**/*'}},output:'src/index.html\\nsrc/app.js'}}}});
              a.parts.set('r2',{{id:'r2',type:'reasoning',text:'Ищу **/* — вижу index.html, читаю его.',time:{{start:2500,end:4000}}}});
              a.parts.set('t2',{{id:'t2',type:'tool',tool:'read',callID:'c2',state:{{status:'completed',input:{{filePath:'index.html'}},output:{json.dumps(read_out)}}}}});
              a.parts.set('x1',{{id:'x1',type:'text',text:'Вот структура проекта.'}});
              S.msgs.set('a1',a);
              renderMsgs(true);
            }}""")
            await page.wait_for_timeout(300)
            await page.click('.think-head')
            await page.wait_for_timeout(400)
            await page.screenshot(path=str(OUT / 'probe-think-open.png'))
            # open the read tool entry: its output must render BELOW the chip
            await page.click('.think .topic:nth-of-type(2) .tsum')
            await page.wait_for_timeout(400)
            layout = await page.evaluate("""()=>{const t=document.querySelectorAll('.think .topic')[1];const s=t.querySelector('.tsum').getBoundingClientRect(),o=t.querySelector('.tout').getBoundingClientRect();return {gap:o.top-s.bottom}}""")
            print('tout gap below summary:', layout)
            await page.screenshot(path=str(OUT / 'probe-think-tool-open.png'))
            print('pageerrors:', errors)
            await browser.close()
    finally:
        server.terminate()
        out.close()


if __name__ == '__main__':
    asyncio.run(main())
