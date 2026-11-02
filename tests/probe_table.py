"""Debug probe: markdown table with backslash continuations + code block."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ui_round3 as R  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

MD = (
    'Запуск (в Termux: pkg install nodejs если нет):\n\n'
    '```js\nnpm install\nnpm start\n```\n\n'
    'API:\n\n'
    '| Метод | Endpoint | Описание |\n'
    '|---|---|---|\n'
    '| GET | /api/videos | Все видео (?category=popular\\\n'
    'recommended\\\nq=поиск) |\n'
    '| GET | /api/videos/:id | Одно видео |\n'
    '| GET | /api/videos/:id/related | Похожие видео |\n'
    '| POST | /api/videos/:id/like | Увеличить лайки |\n'
)


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
            browser, context, page, errors = await R.boot_page(pw, origin, 'light')
            js = """()=>{S.sid='x';
              S.msgs.set('u1',{info:{id:'m01',sessionID:'x',role:'user'},parts:new Map([['pu',{id:'pu',type:'text',text:'напиши бэкенд'}]])});
              S.msgs.set('a1',{info:{id:'m02',sessionID:'x',role:'assistant',time:{created:1,completed:2}},parts:new Map([['pa',{id:'pa',type:'text',text:__MD__}]])});
              renderMsgs(true)}""".replace('__MD__', json.dumps(MD))
            await page.evaluate(js)
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(R.ROOT / 'tests' / 'evidence' / 'ui_round3' / 'probe-table.png'))
            bar = await page.evaluate("document.querySelector('.msg-actions')?.dataset.amid")
            print('bar amid:', bar, '| pageerrors:', errors)
            await browser.close()
    finally:
        server.terminate()
        out.close()


if __name__ == '__main__':
    asyncio.run(main())
