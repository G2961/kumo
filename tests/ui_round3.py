"""UI round 3: permissions modes, effort chip value, env sheet, context menu, error cards.

Runs against a real opencode 1.19.3 server (no model calls: UI mechanics +
permission ruleset round-trip + fork endpoint presence). Playwright 390x844,
both themes. Screenshots go to tests/evidence/ui_round3/.
"""
import asyncio, json, re, socket, subprocess, sys, urllib.request, urllib.parse
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT.parent / '.local'
TASK = LOCAL / ('ui-round3-' + uuid4().hex[:8])
TASK.mkdir()
OPENCODE = 'D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe'
EVIDENCE = ROOT / 'tests' / 'evidence' / 'ui_round3'

VIEWPORT = {'width': 390, 'height': 844}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def wait_server(origin):
    for _ in range(80):
        try:
            with urllib.request.urlopen(origin + '/global/health', timeout=2) as r:
                health = json.load(r)
            if health.get('healthy'):
                return health
        except Exception:
            await asyncio.sleep(.25)
    raise RuntimeError('opencode did not start')


async def boot_page(pw, origin, theme):
    browser = await pw.chromium.launch()
    context = await browser.new_context(viewport=VIEWPORT)
    await context.grant_permissions(['local-network-access'], origin=origin)
    await context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=origin)
    page = await context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    await page.route('**/_ui/index.html', lambda route: route.fulfill(
        path=str(ROOT / 'app/src/main/assets/codex.html'), content_type='text/html'))
    await page.add_init_script(f"localStorage.setItem('oc_theme','\"{theme}\"');localStorage.removeItem('oc_lp_hint')")
    await page.goto(origin + '/_ui/index.html')
    await page.wait_for_timeout(600)
    return browser, context, page, errors


async def open_sheet(page, selector):
    await page.click(selector)
    await page.wait_for_timeout(350)


async def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    port = free_port()
    origin = f'http://127.0.0.1:{port}'
    out = open(TASK / 'opencode.log', 'w', encoding='utf-8')
    server = subprocess.Popen([OPENCODE, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                              cwd=TASK, stdout=out, stderr=out)
    result = {'checks': [], 'failures': []}

    def check(name, ok, detail=''):
        result['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})
        print(('PASS ' if ok else 'FAIL ') + name + (' :: ' + detail if detail and not ok else ''), flush=True)
        if not ok:
            result['failures'].append(name)

    try:
        health = await wait_server(origin)
        check('server_health', health['version'] == '1.19.3', str(health))

        real_project = TASK / 'perm-project'
        real_project.mkdir()
        with urllib.request.urlopen(origin + '/path', timeout=10) as r:
            default_dir = json.load(r)['directory']

        # ---- real-server permission round-trip (Block B plumbing) ----
        def api(path, body=None, method='GET', directory=None):
            url = origin + path
            if directory:
                url += ('&' if '?' in url else '?') + urllib.parse.urlencode({'directory': directory})
            req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'}, method=method)
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        sess = api('/session', {'title': 'perm roundtrip'}, 'POST', str(real_project))
        rules = [{'permission': 'bash', 'pattern': '*', 'action': 'ask'},
                 {'permission': 'write', 'pattern': '*', 'action': 'ask'},
                 {'permission': 'edit', 'pattern': '*', 'action': 'ask'},
                 {'permission': 'mobile_ssh_ssh_exec', 'pattern': '*', 'action': 'ask'},
                 {'permission': 'mobile_ssh_ssh_write_file', 'pattern': '*', 'action': 'ask'}]
        api('/session/' + sess['id'], {'permission': rules}, 'PATCH', str(real_project))
        back = api('/session/' + sess['id'], directory=str(real_project))
        got = {(r['permission'], r['pattern']): r['action'] for r in (back.get('permission') or [])}
        ok_rules = all(got.get((r['permission'], r['pattern'])) == r['action'] for r in rules)
        check('permission_ruleset_roundtrip', ok_rules, str(got))

        async with async_playwright() as pw:
            for theme in ['dark', 'light']:
                browser, context, page, errors = await boot_page(pw, origin, theme)
                shot = lambda name: page.screenshot(path=str(EVIDENCE / f'{theme}-{name}.png'))

                # boot integrity
                check(f'{theme}_boot_no_js_errors', not errors, '; '.join(errors[:3]))

                # tokens (Block A3/A5, preferences 5)
                bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
                if theme == 'dark':
                    comp = await page.evaluate("getComputedStyle(document.querySelector('.comp')).backgroundColor")
                    sheet_bg = await page.evaluate("getComputedStyle(document.querySelector('#sheet')).backgroundColor")
                    check(f'{theme}_page_color', bg == 'rgb(33, 33, 33)', bg)
                    check(f'{theme}_composer_color', comp == 'rgb(48, 48, 48)', comp)
                    check(f'{theme}_sheet_color', sheet_bg == 'rgb(24, 24, 24)', sheet_bg)
                    ph = await page.evaluate("""getComputedStyle(document.querySelector('#inp'), '::placeholder').color""")
                    check(f'{theme}_placeholder_contrast', ph in ('rgb(175, 175, 175)', 'rgb(170, 170, 170)'), ph)
                else:
                    check(f'{theme}_page_color', bg == 'rgb(255, 255, 255)', bg)
                await shot('home')

                # effort chip shows a value, not the word Reasoning (Block A1)
                eff_text = await page.eval_on_selector('#cEff span', 'e=>e.textContent')
                check(f'{theme}_effort_chip_default', eff_text == 'Default', eff_text)
                await open_sheet(page, '#cEff')
                await shot('effort-sheet')
                opts = await page.eval_on_selector_all('#sheet .opt', "els=>els.map(e=>e.textContent.trim())")
                check(f'{theme}_effort_sheet_rows', any('High' in o for o in opts), str(opts))
                await page.click('#sheet .opt:nth-child(4)')
                await page.wait_for_timeout(250)
                eff2 = await page.eval_on_selector('#cEff span', 'e=>e.textContent')
                check(f'{theme}_effort_chip_value', eff2 not in ('', 'Reasoning'), eff2)

                # environment sheet (Block A2)
                await open_sheet(page, '#cEnv')
                await shot('env-sheet')
                title = await page.eval_on_selector('#sheet h3', 'e=>e.textContent')
                check(f'{theme}_env_title', title == 'Environment', title)
                body_text = await page.eval_on_selector('#sheet', 'e=>e.innerText')
                check(f'{theme}_env_no_websearch', 'Web search' not in body_text, body_text[:120])
                check(f'{theme}_env_empty_ssh_note', 'No SSH connections' in body_text)
                check(f'{theme}_env_add_ssh_button', 'Add SSH connection' in body_text)
                agent_rows = await page.eval_on_selector_all('#sheet .opt', "els=>els.map(e=>e.className)")
                check(f'{theme}_env_agent_opt_style', all('btn' not in c for c in agent_rows), str(agent_rows))
                await page.click('#dim')

                # permissions sheet + auto-approve warning (Block B4/B5)
                await page.evaluate("document.querySelector('#cPerm').scrollIntoView()")
                await open_sheet(page, '#cPerm')
                labels = await page.eval_on_selector_all('#sheet .opt', "els=>els.map(e=>e.textContent.trim())")
                check(f'{theme}_perm_rows', len(labels) == 3 and any('Auto-approve' in l for l in labels), str(labels))
                auto_idx = next(i for i, l in enumerate(labels) if 'Auto-approve' in l)
                await page.click(f'#sheet .opt:nth-child({auto_idx + 1})')
                await page.wait_for_timeout(250)
                warn = await page.eval_on_selector('#sheet h3', 'e=>e.textContent')
                check(f'{theme}_auto_warning', warn == 'Auto-approve', warn)
                await page.click('#apYes')
                await page.wait_for_timeout(250)
                chip = await page.eval_on_selector('#cPerm span', 'e=>e.textContent')
                danger = await page.eval_on_selector('#cPerm', "e=>e.classList.contains('danger')")
                check(f'{theme}_auto_chip', chip == 'Auto-approve' and danger, chip)
                await open_sheet(page, '#cPerm')
                ro_idx = next(i for i, l in enumerate(
                    await page.eval_on_selector_all('#sheet .opt', "els=>els.map(e=>e.textContent)")) if 'Read-only' in l)
                await page.click(f'#sheet .opt:nth-child({ro_idx + 1})')
                await page.wait_for_timeout(250)
                chip2 = await page.eval_on_selector('#cPerm span', 'e=>e.textContent')
                check(f'{theme}_readonly_chip', chip2 == 'Read-only', chip2)

                # commit pill without chevron, inactive on empty thread (Block A6)
                commit_html = await page.eval_on_selector('#commit', 'e=>e.innerHTML')
                check(f'{theme}_commit_no_chevron', 'm6 9 6 6 6-6' not in commit_html, commit_html[:150])

                # synthetic thread: cards + error card + context menu (Block C/F)
                await page.evaluate("""() => {
                  const uid='msg_u1',aid='msg_a1';
                  const u={info:{id:uid,sessionID:'x',role:'user'},parts:new Map([['p1',{id:'p1',type:'text',text:'Fix the login bug'}]])};
                  const a={info:{id:aid,sessionID:'x',role:'assistant'},parts:new Map([
                    ['p2',{id:'p2',type:'tool',tool:'bash',callID:'c1',state:{status:'completed',input:{command:'df -h'},output:'total 9.8G',metadata:{},time:{start:1,end:2}}}],
                    ['p3',{id:'p3',type:'tool',tool:'edit',callID:'c2',state:{status:'completed',input:{filePath:'src/login.js'},output:'',metadata:{},time:{start:1,end:2}}}],
                    ['p4',{id:'p4',type:'text',text:'Here is the **fix** with `code`.'}]
                  ])};
                  const err={info:{id:'msg_a2',sessionID:'x',role:'assistant',error:{name:'APIError',data:{message:'Rate limit exceeded',statusCode:429,isRetryable:true}}},parts:new Map()};
                  const filler=[...Array(20)].map((_,i)=>['mf'+i,{info:{id:'msg_f'+String(i).padStart(2,'0'),sessionID:'x',role:'user'},parts:new Map([['pf'+i,{id:'pf'+i,type:'text',text:'Filler message '+i}]])}]);
                  S.msgs=new Map([['m1',u],['m2',a],['m3',err],...filler]);
                  S.sid='x';S.historyFiles=new Map();
                  renderMsgs(true);
                }""")
                await page.wait_for_timeout(250)
                await shot('thread')
                user_attr = await page.eval_on_selector('.msg.user[data-mid="msg_u1"]', "e=>e.dataset.utext")
                check(f'{theme}_user_msg_attrs', user_attr == 'Fix the login bug', str(user_attr))
                ai_attr = await page.eval_on_selector('.msg.ai[data-mid="msg_a1"]', "e=>e.dataset.raw")
                check(f'{theme}_ai_msg_attrs', ai_attr is not None)
                err_card = await page.eval_on_selector('.error-card', 'e=>e.innerText')
                check(f'{theme}_error_card_retry', 'Retry' in err_card and 'Switch model' in err_card, err_card[:150])
                check(f'{theme}_error_card_code', '429' in err_card)
                check(f'{theme}_error_no_working_spinner', 'Working...' not in await page.eval_on_selector('#main', 'e=>e.innerText'))
                # ghost-card fix: completed edit w/o recorded diff renders compact, no empty details body
                change_cards = await page.eval_on_selector_all('.change-card', "els=>els.map(e=>e.innerText)")
                check(f'{theme}_no_empty_diff_details', all('No diff recorded' not in c for c in change_cards), str(change_cards))
                compact = await page.eval_on_selector_all('#main .card', "els=>els.filter(e=>!e.classList.contains('change-card')&&!e.classList.contains('error-card')).map(e=>e.innerText)")
                check(f'{theme}_compact_change_row', any('src/login.js' in c and '+' not in c for c in compact), str(compact))

                # long-press context menu on user message (Block F)
                box = await page.evaluate("()=>{const e=document.querySelector('.msg.user[data-mid=\"msg_u1\"]');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:Math.max(60,r.y+10)}}")
                await page.mouse.move(box['x'], box['y'])
                await page.mouse.down()
                await page.wait_for_timeout(600)
                await page.mouse.up()
                await page.wait_for_timeout(350)
                menu = await page.eval_on_selector('#sheet', "e=>e.classList.contains('on')?e.innerText:'OFF'")
                check(f'{theme}_lp_menu_user', all(k in menu for k in ['Copy', 'Edit & resend', 'Resend', 'Select text']), menu[:150])
                await shot('context-menu')
                await page.click('#sheet .opt:nth-child(1)')
                await page.wait_for_timeout(300)
                clip = await page.evaluate("navigator.clipboard.readText()")
                check(f'{theme}_copy_works', clip == 'Fix the login bug', clip)

                # long-press does not block scrolling (Block F tests)
                scroll_info = await page.evaluate("()=>{const m=document.querySelector('#main');const can=m.scrollHeight>m.clientHeight;m.scrollTop=50;return {can,v:m.scrollTop}}")
                check(f'{theme}_scroll_after_lp', scroll_info['can'] and scroll_info['v'] == 50, str(scroll_info))

                # ai message menu + fork from here (Block F)
                box2 = await page.evaluate("()=>{const e=document.querySelector('.msg.ai[data-mid=\"msg_a1\"]');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:Math.max(60,r.y+10)}}")
                await page.mouse.move(box2['x'], box2['y'])
                await page.mouse.down()
                await page.wait_for_timeout(600)
                await page.mouse.up()
                await page.wait_for_timeout(350)
                menu2 = await page.eval_on_selector('#sheet', "e=>e.classList.contains('on')?e.innerText:'OFF'")
                check(f'{theme}_lp_menu_ai', all(k in menu2 for k in ['Copy', 'Retry', 'Fork from here']), menu2[:150])
                fork_calls = []
                async def fork_route(route):
                    fork_calls.append(route.request.post_data_json)
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps({'id': 'ses_forked', 'title': 'Forked thread'}))
                await page.route('**/session/x/fork*', fork_route)
                await page.click('#sheet .opt:nth-child(4)')
                await page.wait_for_timeout(600)
                check(f'{theme}_fork_request', len(fork_calls) == 1 and fork_calls[0] == {'messageID': 'msg_a1'}, str(fork_calls))

                await browser.close()

            # edit & resend fork-on-send flow (Block F): single theme pass
            browser, context, page, errors = await boot_page(pw, origin, 'dark')
            await page.evaluate("""() => {
              const u={info:{id:'msg_u1',sessionID:'x',role:'user'},parts:new Map([['p1',{id:'p1',type:'text',text:'Original ask'}]])};
              S.msgs=new Map([['m1',u]]);S.sid='x';renderMsgs(true);
            }""")
            forks = []
            async def fork2_route(route):
                forks.append(route.request.post_data_json)
                await route.fulfill(status=200, content_type='application/json', body=json.dumps({'id': 'ses_fork2', 'title': 'Forked thread'}))
            await page.route('**/session/x/fork*', fork2_route)
            async def fork2_follow(route):
                if route.request.method == 'GET':
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps({'id': 'ses_fork2', 'title': 'Forked thread', 'permission': []}))
                else:
                    await route.fulfill(status=200, content_type='application/json', body='{}')
            await page.route('**/session/ses_fork2?**', fork2_follow)
            prompts = []
            async def prompt_route(route):
                prompts.append(route.request.post_data_json)
                await route.fulfill(status=204)
            await page.route('**/session/ses_fork2/prompt_async*', prompt_route)
            await page.evaluate("startEditResend('msg_u1','Edited text')")
            banner = await page.eval_on_selector('#editBanner', "e=>e.hidden?'HIDDEN':e.innerText")
            check('edit_banner_visible', 'Editing · will fork from here' in banner, banner)
            await page.click('#editCancel')
            banner2 = await page.eval_on_selector('#editBanner', "e=>e.hidden")
            check('edit_banner_cancel', banner2 is True)
            await page.evaluate("startEditResend('msg_u1','Edited text')")
            await page.click('#send')
            await page.wait_for_timeout(700)
            check('edit_resend_fork_body', len(forks) == 1 and forks[0] == {'messageID': 'msg_u1'}, str(forks))
            check('edit_resend_prompt_to_fork', len(prompts) == 1 and any('Edited text' == p.get('parts', [{}])[0].get('text') for p in prompts if isinstance(p, dict)), str(prompts)[:200])
            await browser.close()

        # spec check: fork endpoint really exists (Block F: API not invented)
        with urllib.request.urlopen(origin + '/doc', timeout=15) as r:
            spec = json.load(r)
        check('fork_endpoint_in_doc', '/session/{sessionID}/fork' in spec['paths'])

    finally:
        server.terminate()
        out.close()
        (TASK / 'result.json').write_text(json.dumps(result, indent=2), 'utf-8')
        (LOCAL / 'ui-round3-last.txt').write_text(str(TASK), 'utf-8')
    if result['failures']:
        print('FAILURES: ' + ', '.join(result['failures']), flush=True)
        sys.exit(1)
    print('ALL PASS', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
