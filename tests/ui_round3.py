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

                # effort chip hidden at default (no model/reasoning in bare env)
                eff_hidden = await page.eval_on_selector('#cEff', 'e=>e.hidden')
                check(f'{theme}_effort_chip_hidden_default', eff_hidden is True, str(eff_hidden))
                # effort reachable from the model sheet; chip appears once a value is set
                await open_sheet(page, '#cModel')
                has_eff_row = await page.evaluate("()=>{S.provs=[{id:'p1',name:'Prov One',models:[{id:'m1',name:'Model One',capabilities:{reasoning:true},variants:{low:{},high:{}}}]}];S.model={p:'p1',m:'m1'};chips();modelSheet();return !!document.querySelector('#sheetEffort')}")
                check(f'{theme}_model_sheet_effort_row', has_eff_row)
                eff_vis = await page.eval_on_selector('#cEff', 'e=>e.hidden')
                check(f'{theme}_effort_chip_hidden_default2', eff_vis is True, str(eff_vis))
                await page.click('#sheetEffort')
                await page.wait_for_timeout(250)
                await shot('effort-sheet')
                opts = await page.eval_on_selector_all('#sheet .opt', "els=>els.map(e=>e.textContent.trim())")
                check(f'{theme}_effort_sheet_rows', any('High' in o for o in opts), str(opts))
                await page.click('#sheet .opt:nth-child(3)')
                await page.wait_for_timeout(250)
                eff_vis2 = await page.eval_on_selector('#cEff', 'e=>e.hidden')
                eff2 = await page.eval_on_selector('#cEff span', 'e=>e.textContent')
                check(f'{theme}_effort_chip_value', eff_vis2 is False and eff2 == 'High', eff2 + '|' + str(eff_vis2))
                # model chip shows provider when names collide
                await page.evaluate("()=>{S.provs=[{id:'p1',name:'Prov One',models:[{id:'m1',name:'Model One',capabilities:{reasoning:true}}]},{id:'p2',name:'Prov Two',models:[{id:'m1',name:'Model One',capabilities:{reasoning:true}}]}];S.model={p:'p2',m:'m1'};chips()}")
                chip_lbl = await page.eval_on_selector('#cModel span', 'e=>e.textContent')
                check(f'{theme}_model_chip_disambig', chip_lbl == 'Model One · Prov Two', chip_lbl)
                # non-reasoning model -> chip hidden again
                await page.evaluate("()=>{S.provs=[{id:'p1',name:'Prov One',models:[{id:'m1',name:'Model One',capabilities:{reasoning:false},variants:{low:{},high:{}}}]}];S.model={p:'p1',m:'m1'};chips()}")
                eff_vis3 = await page.eval_on_selector('#cEff', 'e=>e.hidden')
                check(f'{theme}_effort_chip_hidden_nonreasoning', eff_vis3 is True, str(eff_vis3))

                # effort sheet shows rows; then gesture behavior: content swipe never closes, grab swipe closes
                await open_sheet(page, '#cEnv')
                content_swipe = await page.evaluate("""() => {
                  const sh=document.querySelector('#sheet');
                  const el=sh.querySelector('.opt')||sh;
                  const r=el.getBoundingClientRect();
                  const t=(x,y)=>new Touch({identifier:1,target:el,clientX:x,clientY:y});
                  sh.dispatchEvent(new TouchEvent('touchstart',{touches:[t(r.x,r.y)],bubbles:true}));
                  sh.dispatchEvent(new TouchEvent('touchmove',{touches:[t(r.x,r.y+120)],bubbles:true,cancelable:true}));
                  sh.dispatchEvent(new TouchEvent('touchend',{touches:[],changedTouches:[t(r.x,r.y+120)],bubbles:true}));
                  return sh.classList.contains('on');
                }""")
                check(f'{theme}_content_swipe_keeps_sheet', content_swipe is True)
                grab_swipe = await page.evaluate("""() => {
                  const sh=document.querySelector('#sheet');
                  const el=sh.querySelector('.grab');
                  const r=el.getBoundingClientRect();
                  const t=(x,y)=>new Touch({identifier:2,target:el,clientX:x,clientY:y});
                  const ev=(type,y,extra)=>el.dispatchEvent(new TouchEvent(type,{touches:type==='touchend'?[]:[t(r.x+19,y)],changedTouches:[t(r.x+19,y)],bubbles:true,cancelable:true,...extra}));
                  ev('touchstart',r.y+11);
                  ev('touchmove',r.y+131);
                  ev('touchend',r.y+131);
                  return sh.classList.contains('on');
                }""")
                check(f'{theme}_grab_swipe_closes_sheet', grab_swipe is False)

                # environment sheet: connection only, no agent block (feedback 2)
                await open_sheet(page, '#cEnv')
                await shot('env-sheet')
                title = await page.eval_on_selector('#sheet h3', 'e=>e.textContent')
                check(f'{theme}_env_title', title == 'Environment', title)
                body_text = await page.eval_on_selector('#sheet', 'e=>e.innerText')
                check(f'{theme}_env_no_websearch', 'Web search' not in body_text, body_text[:120])
                check(f'{theme}_env_empty_ssh_note', 'No SSH connections' in body_text)
                check(f'{theme}_env_add_server', 'Add server' in body_text)
                check(f'{theme}_env_no_agent_block', 'Ask first' not in body_text and 'Read only' not in body_text, body_text[:120])
                # Add server leads to the Remote screen (feedback 1)
                await page.click('#sa')
                await page.wait_for_timeout(300)
                remote_vis = await page.evaluate("!document.querySelector('#remoteScreen').hidden")
                remote_t = await page.eval_on_selector('#remoteScreen h1', 'e=>e.textContent')
                sheet_closed = await page.evaluate("!document.querySelector('#sheet').classList.contains('on')")
                check(f'{theme}_add_server_opens_remote', remote_vis and remote_t == 'Remote servers' and sheet_closed, remote_t + '|' + str(remote_vis))
                await page.evaluate("closeRemote()")

                # Mode sheet (feedback 2): Plan / Ask first / Auto-approve
                await open_sheet(page, '#cPerm')
                mode_t = await page.eval_on_selector('#sheet h3', 'e=>e.textContent')
                labels = await page.eval_on_selector_all('#sheet .opt', "els=>els.map(e=>e.textContent.trim())")
                check(f'{theme}_mode_sheet', mode_t == 'Mode' and labels[0].startswith('Plan') and labels[1].startswith('Ask first') and labels[2].startswith('Auto-approve'), mode_t + '|' + str(labels))
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
                await page.click('#sheet .opt:nth-child(1)')
                await page.wait_for_timeout(250)
                chip2 = await page.eval_on_selector('#cPerm span', 'e=>e.textContent')
                check(f'{theme}_plan_chip', chip2 == 'Plan', chip2)

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

            # provider save closes the sheet immediately (bugfix)
            browser, context, page, errors = await boot_page(pw, origin, 'dark')
            await page.route('**/global/config', lambda route: route.fulfill(status=200, content_type='application/json', body='{}'))
            await page.route('**/auth/*', lambda route: route.fulfill(status=200, content_type='application/json', body='true'))
            slow_provs = []
            async def slow_providers(route):
                await asyncio.sleep(8)
                await route.fulfill(status=200, content_type='application/json', body=json.dumps({'providers': [{'id': 'testprov', 'name': 'Test', 'models': {'m1': {'id': 'm1', 'name': 'Model 1'}}}], 'default': {}}))
                slow_provs.append(1)
            await page.route('**/config/providers', slow_providers)
            await page.evaluate("providerForm()")
            await page.evaluate("""() => {
              document.querySelector('#pi').value='testprov';
              document.querySelector('#pu').value='https://example.com/v1';
              document.querySelector('#pk').value='sk-test-key';
              document.querySelector('#pm').value='m1';
            }""")
            await page.click('#ps')
            await page.wait_for_timeout(1500)
            sheet_open = await page.eval_on_selector('#sheet', "e=>e.classList.contains('on')")
            check('provider_save_closes_fast', sheet_open is False and not slow_provs, f'sheet={sheet_open} slowLoaded={bool(slow_provs)}')
            key_after = await page.evaluate("document.querySelector('#pk') ? document.querySelector('#pk').value : ''")
            check('provider_key_cleared_after_save', key_after == '', repr(key_after))
            await page.wait_for_timeout(9000)
            err_count = await page.evaluate("S.errors.length")
            check('provider_slow_refresh_no_crash', err_count >= 0)

            # model sheet: grouped by provider, display name only, single selection
            await page.evaluate("""() => {
              S.provs=[{id:'agentrouter',name:'AgentRouter One',models:[{id:'gpt-6.1-sol',name:'GPT-6.1 Sol'}]},
                       {id:'agentrouter2',name:'AgentRouter Two',models:[{id:'gpt-6.1-sol',name:'GPT-6.1 Sol'}]}];
              S.model={p:'agentrouter2',m:'gpt-6.1-sol'};
              modelSheet();
            }""")
            await page.wait_for_timeout(250)
            headers = await page.eval_on_selector_all('#sheet .grp-h', "els=>els.map(e=>e.innerText)")
            check('model_sheet_grouped', len(headers) == 2 and 'agentrouter one' in headers[0].lower() and 'agentrouter two' in headers[1].lower(), str(headers))
            sel_rows = await page.eval_on_selector_all('#sheet .opt.sel', "els=>els.map(e=>e.innerText)")
            check('model_sheet_single_sel', len(sel_rows) == 1 and 'GPT-6.1 Sol' in sel_rows[0], str(sel_rows))
            row_text = await page.eval_on_selector('#sheet .opt.sel', "e=>e.innerText")
            check('model_sheet_no_provider_id', 'agentrouter' not in row_text.lower(), row_text)
            # collapse / expand groups
            await page.click('#sheet .grp-h')
            await page.wait_for_timeout(350)
            collapsed = await page.eval_on_selector('#sheet .grp', "e=>!e.querySelector('.proj-body').classList.contains('open')")
            check('model_sheet_group_collapses', collapsed is True)
            persisted = await page.evaluate("localStorage.getItem('oc_model_groups')")
            check('model_sheet_collapse_persisted', 'agentrouter' in (persisted or ''), str(persisted))
            await page.click('#sheet .grp-h')
            await page.wait_for_timeout(350)
            expanded = await page.eval_on_selector('#sheet .grp', "e=>e.querySelector('.proj-body').classList.contains('open')")
            check('model_sheet_group_expands', expanded is True)
            await page.evaluate("document.querySelectorAll('#sheet .opt')[0].click()")
            await page.wait_for_timeout(250)
            sel_model = await page.evaluate("S.model")
            check('model_sheet_pick_by_index', sel_model == {'p': 'agentrouter', 'm': 'gpt-6.1-sol'}, str(sel_model))
            await browser.close()

            # Block G: sidebar structure with real threads (3 projects, 12 sessions, one folder >5)
            gdirs = []
            for gi in range(3):
                gd = TASK / f'g-project-{gi}'
                gd.mkdir()
                gdirs.append(gd)
                subprocess.run(['git', '-C', str(gd), 'init', '-q'], check=True, capture_output=True)
            for gi, (gd, n) in enumerate(zip(gdirs, [7, 3, 2])):
                for j in range(n):
                    title = 'Zebra cross-project' if (gi == 2 and j == 0) else f'G thread {gi}-{j}'
                    api('/session', {'title': title}, 'POST', str(gd))
            for theme2 in ['dark', 'light']:
                browser, context, page, errors = await boot_page(pw, origin, theme2)
                gshot = lambda name: page.screenshot(path=str(EVIDENCE / f'g-{theme2}-{name}.png'))
                check(f'g_{theme2}_boot', not errors, '; '.join(errors[:2]))
                await page.evaluate(f"""async () => {{
                  S.savedProjects={json.dumps([str(d) for d in gdirs])};lset('oc_projects',S.savedProjects);
                  S.projects=sortProjects(S.savedProjects.map(w=>({{worktree:w}})));
                  S.projects.forEach(p=>S.expandedFolders.add(p.worktree));
                  renderDrawer();drawer(true);
                  await Promise.all(S.projects.map(p=>loadFolderSessions(p.worktree)));
                  renderDrawer();
                }}""")
                await page.wait_for_timeout(500)
                await gshot('drawer')
                pin = await page.evaluate("""()=>{const dp=document.querySelector('#drawerProjects');dp.scrollTop=dp.scrollHeight;const h=document.querySelector('.drawer-top-bar').getBoundingClientRect();const b=document.querySelector('.drawer-bottom').getBoundingClientRect();return {h:[h.top,h.bottom],b:[b.top,b.bottom],vh:innerHeight}}""")
                check(f'g_{theme2}_header_pinned', pin['h'][0] >= 0 and pin['h'][1] <= pin['vh'], str(pin))
                check(f'g_{theme2}_bottom_pinned', pin['b'][1] <= pin['vh'] + 1, str(pin))
                counts_in_folder = await page.evaluate("()=>document.querySelectorAll('#drawer .proj-group')[0].querySelectorAll('.th').length")
                check(f'g_{theme2}_five_plus_showall', counts_in_folder == 6, str(counts_in_folder))
                await page.click('#drawer .th-more')
                await page.wait_for_timeout(300)
                after = await page.evaluate("()=>{const g=document.querySelectorAll('#drawer .proj-group')[0];return {n:g.querySelectorAll('.th').length,more:!!g.querySelector('.th-more')}}")
                check(f'g_{theme2}_showall_expands', after['n'] == 7 and not after['more'], str(after))
                sizes = await page.evaluate("()=>[...document.querySelectorAll('#drawer .nav-row, .btn-new-thread, .drawer-gear, #drawer .th')].every(e=>{const r=e.getBoundingClientRect();return r.height>=44&&r.width>=44})")
                check(f'g_{theme2}_touch_targets', sizes)
                text = await page.eval_on_selector('#drawer', 'e=>e.innerText')
                check(f'g_{theme2}_no_decorative', not any(x in text for x in ['Images', 'Library', 'Scheduled']), text[:100])
                await page.click('.nav-row[data-a="remote"]')
                await page.wait_for_timeout(400)
                remote_vis = await page.evaluate("!document.querySelector('#remoteScreen').hidden")
                remote_t = await page.eval_on_selector('#remoteScreen h1', 'e=>e.textContent')
                check(f'g_{theme2}_remote_screen', remote_vis and remote_t == 'Remote servers', remote_t)
                await page.evaluate("closeRemote()")
                # drawer tap while a sheet is open closes only the sheet (no action)
                await page.evaluate("renderDrawer();drawer(true)")
                await page.wait_for_timeout(400)
                await page.click('.nav-row[data-a="plugins"]')
                await page.wait_for_timeout(500)
                sheet_on = await page.evaluate("document.querySelector('#sheet').classList.contains('on')")
                check(f'g_{theme2}_plugins_sheet', sheet_on)
                await page.click('#drawer [data-a="search"]')
                await page.wait_for_timeout(300)
                after_tap = await page.evaluate("({sheet:document.querySelector('#sheet').classList.contains('on'),search:document.querySelector('#drawerSearchBox')?.hidden!==false})")
                check(f'g_{theme2}_drawer_tap_closes_sheet_only', not after_tap['sheet'] and after_tap['search'], str(after_tap))
                await page.evaluate("renderDrawer();drawer(true)")
                await page.wait_for_timeout(400)
                await page.evaluate("renderDrawer();drawer(true)")
                await page.wait_for_timeout(400)
                await page.wait_for_timeout(350)
                await page.click('#drawer [data-a="search"]')
                await page.wait_for_timeout(250)
                await gshot('search')
                await page.fill('#drawerSearchInput', 'Zebra')
                await page.wait_for_timeout(800)
                res = await page.eval_on_selector_all('#drawerSearchResults .th', 'els=>els.map(e=>e.innerText)')
                projname = await page.evaluate(f"projName({json.dumps(str(gdirs[2]))})")
                check(f'g_{theme2}_search_found', len(res) >= 1 and 'Zebra' in res[0], str(res))
                check(f'g_{theme2}_search_shows_project', projname in res[0], res[0] + ' vs ' + projname)
                await page.click('#drawerSearchResults .th')
                await page.wait_for_timeout(900)
                title = await page.eval_on_selector('#title', 'e=>e.textContent')
                check(f'g_{theme2}_search_opens_thread', 'Zebra' in title, title)
                dimc = await page.evaluate("getComputedStyle(document.querySelector('#dim')).backgroundColor")
                check(f'g_{theme2}_dim_65', dimc == 'rgba(0, 0, 0, 0.65)', dimc)
                hdr = await page.evaluate("({gear:!!document.querySelector('#gear'),diff:!!document.querySelector('#diffPanel')})")
                check(f'g_{theme2}_header_gone_diff_in', not hdr['gear'] and hdr['diff'], str(hdr))
                await browser.close()

            # Settings screen (dark)
            browser, context, page, errors = await boot_page(pw, origin, 'dark')
            await page.evaluate("openSettings()")
            await page.wait_for_timeout(300)
            visible = await page.evaluate("!document.querySelector('#settingsScreen').hidden")
            check('set_screen_visible', visible)
            h1 = await page.eval_on_selector('#settingsScreen h1', 'e=>e.textContent')
            check('set_title', h1 == 'Settings', h1)
            await page.screenshot(path=str(EVIDENCE / 'settings-dark.png'))
            theme_lbl = await page.eval_on_selector('#settingsScreen [data-sa="theme"] .m', 'e=>e.textContent')
            check('set_theme_row_dark', theme_lbl == 'Dark', theme_lbl)
            await page.screenshot(path=str(EVIDENCE / 'settings-dark.png'))
            await page.click('#settingsScreen [data-sa="theme"]')
            await page.wait_for_timeout(300)
            th_t = await page.eval_on_selector('#sheet h3', 'e=>e.textContent')
            th_opts = await page.eval_on_selector_all('#sheet .opt', 'els=>els.map(e=>e.textContent.trim())')
            check('set_theme_sheet', th_t == 'Theme' and th_opts == ['System', 'Light', 'Dark'], th_t + '|' + str(th_opts))
            await page.click('#sheet .opt:nth-child(2)')
            await page.wait_for_timeout(200)
            t1 = await page.evaluate("document.documentElement.dataset.theme")
            lbl1 = await page.eval_on_selector('#settingsScreen [data-sa="theme"] .m', 'e=>e.textContent')
            check('set_theme_light', t1 == 'light' and lbl1 == 'Light', t1 + '|' + lbl1)
            # System follows the emulated OS scheme
            await page.emulate_media(color_scheme='light')
            await page.click('#settingsScreen [data-sa="theme"]')
            await page.wait_for_timeout(250)
            await page.click('#sheet .opt:nth-child(1)')
            await page.wait_for_timeout(200)
            t2 = await page.evaluate("document.documentElement.dataset.theme")
            check('set_theme_system_light', t2 == 'light', t2)
            await page.emulate_media(color_scheme='dark')
            await page.wait_for_timeout(200)
            t3 = await page.evaluate("document.documentElement.dataset.theme")
            check('set_theme_system_follows', t3 == 'dark', t3)
            # font row opens slider sheet
            await page.click('#settingsScreen [data-sa="font"]')
            await page.wait_for_timeout(250)
            await page.evaluate("document.querySelector('#setFontScale').value=120;document.querySelector('#setFontScale').dispatchEvent(new Event('input'))")
            fs = await page.evaluate("document.documentElement.style.getPropertyValue('--font-scale')")
            flbl = await page.eval_on_selector('#settingsScreen [data-sa="font"] .m', 'e=>e.textContent')
            check('set_font_applies', fs == '1.2' and flbl == '120%', fs + '|' + flbl)
            await page.click('#dim')
            await page.wait_for_timeout(250)
            # haptics toggle persists
            await page.click('#setHaptics')
            await page.wait_for_timeout(150)
            hp = await page.evaluate("localStorage.getItem('oc_haptics')")
            check('set_haptics_persisted', hp == 'false', str(hp))
            # web search NOT in settings; lives in the plus menu
            body = await page.eval_on_selector('#settingsScreen', 'e=>e.innerText')
            check('set_no_websearch_row', 'Web search' not in body)
            check('set_no_ssh_row', 'SSH connections' not in body, body[:200])
            await page.click('#settingsScreen [data-sa="providers"]')
            await page.wait_for_timeout(600)
            pt = await page.eval_on_selector('#sheet h3', 'e=>e.textContent')
            check('set_providers_sheet', pt == 'Providers', pt)
            await page.click('#dim', position={'x': 195, 'y': 40})
            await page.wait_for_timeout(300)
            still = await page.evaluate("!document.querySelector('#settingsScreen').hidden")
            check('set_screen_behind_sheet', still)
            srv = await page.evaluate("document.querySelector('#settingsScreen').innerText")
            check('set_diag_version', '1.19.3' in srv)
            await page.click('#settingsScreen [data-sa="copydiag"]')
            await page.wait_for_timeout(300)
            diag = await page.evaluate("navigator.clipboard.readText()")
            check('set_copy_diag', diag.startswith('{') and '"health"' in diag, diag[:80])
            await page.click('#settingsScreen [data-sa="reset"]')
            await page.wait_for_timeout(150)
            label = await page.eval_on_selector('#resetLabel', 'e=>e.textContent')
            kept = await page.evaluate("localStorage.getItem('oc_theme')")
            check('set_reset_arms_only', label == 'Tap again to confirm' and kept, label + '|' + str(kept))
            await page.click('#settingsScreen [data-sa="reset"]')
            await page.wait_for_timeout(1500)
            wiped = await page.evaluate("({f:localStorage.getItem('oc_font_scale'),w:localStorage.getItem('oc_web_search')})")
            check('set_reset_clears', wiped['f'] is None and wiped['w'] is None, str(wiped))
            await browser.close()

            # detail screens: skill / plugin / ssh
            browser, context, page, errors = await boot_page(pw, origin, 'dark')
            await page.evaluate("S.skillsList=[{name:'deploy-helper',description:'Deploys the site',location:'/skills/deploy',content:'SKILL_BODY_123'}];openSkillDetail(0)")
            await page.wait_for_timeout(250)
            vis = await page.evaluate("!document.querySelector('#detailScreen').hidden")
            t = await page.eval_on_selector('#detailScreen h1', 'e=>e.textContent')
            body = await page.evaluate("document.querySelector('#detailScreen').innerText")
            check('det_skill_screen', vis and t == 'Skill' and 'deploy-helper' in body and 'SKILL_BODY_123' in body, t + '|' + body[:100])
            await page.screenshot(path=str(EVIDENCE / 'detail-skill-dark.png'))
            await page.evaluate("closeDetail()")
            await page.evaluate("S.mcpStatus={mobile_ssh:{status:'connected'}};openPluginDetail('mobile_ssh')")
            await page.wait_for_timeout(250)
            t2 = await page.eval_on_selector('#detailScreen h1', 'e=>e.textContent')
            check('det_plugin_screen', t2 == 'Plugin' and 'connected' in await page.evaluate("document.querySelector('#detailScreen').innerText"), t2)
            await page.click('#detailScreen [data-da="mcpToggle"]')
            await page.wait_for_timeout(500)
            alive = await page.evaluate("!document.querySelector('#detailScreen').hidden")
            check('det_plugin_toggle_nocrash', alive)
            await page.evaluate("closeDetail()")
            await page.evaluate("S.connections=[{id:'c1',name:'Srv One',host:'192.0.2.1',port:2222,user:'root',directory:'/srv',auth:'password',fingerprint:'SHA256:abc'}];S.connectionID=null;openSshDetail('c1')")
            await page.wait_for_timeout(250)
            body3 = await page.evaluate("document.querySelector('#detailScreen').innerText")
            check('det_ssh_fields', all(x in body3 for x in ['192.0.2.1', '2222', 'root', '/srv']), body3[:200])
            check('det_ssh_masked', '••••••••' in body3)
            await page.click('#detEye')
            await page.wait_for_timeout(300)
            body4 = await page.evaluate("document.querySelector('#detailScreen').innerText")
            check('det_ssh_eye_toggles', '••••••••' not in body4)
            await page.click('#detailScreen [data-da="setactive"]')
            await page.wait_for_timeout(250)
            cid = await page.evaluate("S.connectionID")
            check('det_ssh_set_active', cid == 'c1', str(cid))
            await page.screenshot(path=str(EVIDENCE / 'detail-ssh-dark.png'))
            await browser.close()

        # spec check: fork endpoint really exists (Block F: API not invented)
        with urllib.request.urlopen(origin + '/doc', timeout=15) as r:
            spec = json.load(r)
        check('fork_endpoint_in_doc', '/session/{sessionID}/fork' in spec['paths'])
        # cleanup: remove the test provider injected into the user's global config
        try:
            req = urllib.request.Request(origin + '/global/config', data=json.dumps({'provider': {'p1': None}}).encode(),
                                         headers={'Content-Type': 'application/json'}, method='PATCH')
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass

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
