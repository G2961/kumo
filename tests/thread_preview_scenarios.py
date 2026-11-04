"""Offline contract tests for the isolated thread-preview scenarios.

Run: ../.local/api-test-venv/Scripts/python.exe -B tests/thread_preview_scenarios.py
Reuses the real Settings/iframe/renderer integration proof, not a standalone page.
Only final app boot is disabled. No asset rewriting, native calls, server, storage,
model, build or device. Evidence: ../.local/thread-preview-scenarios/report.json.
A missing scenario is a failure, never a skip/pass. The clock advances real UI
handlers; neither fixture state nor rendered DOM is injected by these tests.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright, expect

sys.dont_write_bytecode = True
import thread_preview_proof as integration

OUT = integration.ROOT.parent / '.local/thread-preview-scenarios'
BUDGET_MS = 15000
TICK_MS = 100
SHORT_IDS = ['thought-local', 'command-local', 'thought-remote', 'command-remote']
SHORT_COPY = {
    'h1': 'допиши бэкенд',
    '#thought-local .event-label': 'Проверка структуры проекта',
    '#command-local .event-label': 'Просмотр файлов проекта',
    '#thought-remote .event-label': 'Проверка серверного каталога',
    '#command-remote .event-label': 'Просмотр каталога развёртывания',
    '.user-message': 'допиши бэкенд',
    '#thought-local .thought-text': 'Проверю файлы проекта и найду точки подключения API.',
    '#thought-remote .thought-text': 'Локально вижу фронтенд. Проверю известный каталог развёртывания на сервере.',
    '.skipped': 'Вопрос пропущен',
    '#answerText': 'В проверенных каталогах есть фронтенд, но готовый бэкенд не найден. Нужно уточнить API и хранение данных перед реализацией.',
    '.model span': 'GLM-5.3-Flash',
}

# Passive instrumentation: calls through the installed Playwright clock and only
# observes future timers. It never calls a simulation callback or mutates the UI.
TIMER_MONITOR = r"""() => {
 const p=window.__scenarioProof={pending:new Map(),scheduled:[],fired:[],cleared:[]};
 const later=window.setTimeout, cancel=window.clearTimeout;
 window.setTimeout=function(fn,delay,...args){
  const id=later.call(window,(...values)=>{
   p.pending.delete(id);p.fired.push({id,at:Date.now()});fn(...values);
  },delay,...args);
  const entry={id,delay:Number(delay)||0};p.pending.set(id,entry);p.scheduled.push(entry);return id;
 };
 window.clearTimeout=function(id){p.pending.delete(id);p.cleared.push(id);return cancel.call(window,id)};
 const repeat=window.setInterval, stop=window.clearInterval;
 window.setInterval=function(fn,delay,...args){
  const id=repeat.call(window,fn,delay,...args);
  const entry={id,delay:Number(delay)||0,interval:true};p.pending.set(id,entry);p.scheduled.push(entry);return id;
 };
 window.clearInterval=function(id){p.pending.delete(id);p.cleared.push(id);return stop.call(window,id)};
}"""

SNAPSHOT = r"""() => {
 const q=s=>document.querySelector(s), visible=e=>!!e&&e.checkVisibility({checkVisibilityCSS:true});
 const feed=q('#feed');
 return {
  scenario:q('#preview').dataset.scenario,
  state:q('#simulationState')?.textContent.trim()||'',
  stateVisible:visible(q('#simulationState')),
  approval:visible(q('#approval-panel')),
  answer:q('#answerText')?.textContent||'',
  events:[...document.querySelectorAll('.event')].map(e=>{
   const status=e.querySelector('.command-status');
   return {id:e.id,kind:e.matches('.thought')?'thought':'command',open:e.open,
    state:[e.dataset.state,e.dataset.status,e.className,
     status?.getAttribute('aria-label'),status?.textContent].filter(Boolean).join(' ').toLowerCase(),
    label:e.querySelector('.event-label')?.textContent||'',
    summary:e.querySelector('summary')?.innerText||'',
    output:e.querySelector('.command-output')?.textContent||'',
    outputToggleVisible:visible(e.querySelector('.output-toggle')),
    visibleText:e.innerText,
    environment:e.querySelector('.environment')?.textContent||''};
  }),
  feed:{top:feed.scrollTop,height:feed.clientHeight,total:feed.scrollHeight,
   distance:feed.scrollHeight-feed.clientHeight-feed.scrollTop},
  pending:window.__scenarioProof?[...window.__scenarioProof.pending.values()]:[],
  now:Date.now()
 };
}"""

STYLE = r"""() => {
 const q=s=>document.querySelector(s), box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};
 const styles=s=>[...document.querySelectorAll(s)].map(e=>{
  const c=getComputedStyle(e);return {font:c.fontSize,line:c.lineHeight,family:c.fontFamily,
   padding:[c.paddingTop,c.paddingRight,c.paddingBottom,c.paddingLeft].map(parseFloat),
   minHeight:parseFloat(c.minHeight),...box(e)};
 });
 return {composer:styles('.composer')[0],draft:styles('#draft')[0],model:styles('.model'),options:styles('.option'),
  fixed:Object.fromEntries(['.composer-footer','.composer','#draft','#demoSend'].map(s=>[s,box(q(s))])),
  phases:[...document.querySelectorAll('.phase')].map(e=>{
   const c=getComputedStyle(e),events=[...e.children].filter(child=>child.matches('.event'));
   const previous=e.previousElementSibling;
   return {id:e.id,rowGap:c.rowGap,marginTop:parseFloat(c.marginTop),
    intraGaps:events.slice(1).map((event,i)=>event.getBoundingClientRect().top-events[i].getBoundingClientRect().bottom),
    interGap:previous?.matches('.phase')?e.getBoundingClientRect().top-previous.getBoundingClientRect().bottom:null};
  }),
  eventGeometry:(()=>{
   const events=[...document.querySelectorAll('.phase > .event')].map(e=>({id:e.id,...box(e)}));
   return {events,span:events.length?events.at(-1).y+events.at(-1).height-events[0].y:0};
  })(),
  summaries:[...document.querySelectorAll('.command > summary')].map(e=>{
   const c=getComputedStyle(e),label=e.querySelector('.event-label'),l=getComputedStyle(label);
   const caret=e.querySelector('.caret'),env=e.querySelector('.environment'),status=e.querySelector('.command-status');
   const r=label.getBoundingClientRect(),cr=caret.getBoundingClientRect();
   return {id:e.parentElement.id,open:e.parentElement.open,label:label.textContent,
    padding:[c.paddingTop,c.paddingRight,c.paddingBottom,c.paddingLeft].map(parseFloat),
    height:r.height,line:parseFloat(l.lineHeight),clamp:l.webkitLineClamp,overflow:l.overflow,
    env:!!env&&env.getBoundingClientRect().width>0&&env.getBoundingClientRect().right<=cr.left,
    status:!!status&&status.getBoundingClientRect().width>0&&status.getBoundingClientRect().right<=cr.left,
    separate:!!env&&!!status&&!env.contains(status)&&!status.contains(env)&&!caret.contains(env)&&!caret.contains(status),
    right:r.right<=cr.left,summary:box(e),
    x:{title:r.x,terminal:e.querySelector('.terminal')?.getBoundingClientRect().x??null,caret:cr.x}};
  }),
  overflow:document.documentElement.scrollWidth>innerWidth+1||document.documentElement.scrollHeight>innerHeight+1,
  viewport:{width:innerWidth,height:innerHeight},
  fonts:[...document.querySelectorAll('.event-label,.thought-text,.command-line,.command-output,.environment,#draft,.model,.option')]
   .map(e=>{const c=getComputedStyle(e);return [c.fontFamily,c.fontSize,c.lineHeight,c.fontWeight]}),
  reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,
  motion:[...document.querySelectorAll('.caret,.command-status')].map(e=>{
   const c=getComputedStyle(e);return {duration:c.transitionDuration,animation:c.animationName,animationDuration:c.animationDuration};
  })};
}"""


def has_state(event, state):
    patterns = {
        'running': r'\brunning\b|\bвыполняется\b|\bдумает\b|\bразмышляет\b',
        'error': r'\berror\b|\bfailed\b|ошибка|не удалось',
        'completed': r'\bcompleted\b|\bsuccess\b|выполнено|завершено',
        'waiting': r'\bwaiting\b|\bapproval\b|ожидает|подтверждени',
        'cancelled': r'\bcancelled\b|\bcanceled\b|отмен',
    }
    return bool(re.search(patterns[state], event['state'], re.I))


def command(snapshot, environment):
    return next((e for e in snapshot['events'] if e['kind'] == 'command'
                 and environment.lower() in e['environment'].lower()), {})


def active(snapshot, kind):
    return next((e for e in snapshot['events'] if e['kind'] == kind and has_state(e, 'running')), {})


async def press(frame, selector, key='Enter'):
    target = frame.locator(selector)
    await expect(target).to_be_visible()
    await expect(target).to_be_enabled()
    # Actual keyboard activation avoids using force-clicks or DOM .click(). It
    # also works with a paused clock, without waiting for animation-frame stability.
    await target.press(key)


async def choose(frame, name):
    if not await frame.locator('#settings').is_visible():
        await press(frame, '#previewSettings')
    await press(frame, f'#settings button[data-scenario="{name}"]')
    await expect(frame.locator('#preview')).to_have_attribute('data-scenario', name)
    await expect(frame.locator(f'#settings [data-scenario="{name}"]')).to_have_attribute('aria-pressed', 'true')
    if await frame.locator('#settings').is_visible():
        await press(frame, '#settings [data-close-dialog]')


async def targets(proof, frame, stage):
    await proof.targets_at(frame, stage)
    bad = [v for v in proof.targets.values() if v['height'] < 44 or v['width'] < 44]
    proof.check(stage + '-44px-controls-both-axes', not bad, bad)


async def structure(proof, frame, stage):
    data = await frame.evaluate(r"""() => ({
     nested:document.querySelectorAll('.thought .command,.thought .command-line,.thought .command-output,.thought .answer,.command .thought,.event .event').length,
     ids:[...document.querySelectorAll('[id]')].map(e=>e.id),
     phases:[...document.querySelectorAll('.phase')].map(e=>[...e.children].filter(c=>c.matches('.event')).map(c=>c.matches('.thought')?'thought':'command')),
     events:document.querySelectorAll('.event').length,
     userOutside:!!document.querySelector('.user-message')&&!document.querySelector('.event .user-message'),
     answerOutside:!document.querySelector('.event #answerText')
    })""")
    proof.check(stage + '-flat-message-and-tool-separation', data['nested'] == 0
                and data['userOutside'] and data['answerOutside']
                and len(data['ids']) == len(set(data['ids']))
                and sum(map(len, data['phases'])) == data['events'], data)


async def style_contract(proof, frame, stage):
    value = await frame.evaluate(STYLE)
    proof.metrics[stage + '-style'] = value
    proof.check(stage + '-model-at-least13-options-at-least12-no-small-fallback',
                len(value['model']) == 1 and float(value['model'][0]['font'].removesuffix('px')) >= 13
                and len(value['options']) == 2 and all(float(v['font'].removesuffix('px')) >= 12 for v in value['options']),
                {'model': value['model'], 'options': value['options']})
    proof.check(stage + '-compact-composer-not-small-textarea',
                value['draft']['minHeight'] >= 44 and value['draft']['height'] >= 44
                and sum(value['composer']['padding'][::2]) < 18,
                {'composer': value['composer'], 'draft': value['draft']})
    proof.check(stage + '-intra-phase-gap0', bool(value['phases']) and all(
        phase['rowGap'] == '0px' and all(abs(gap) <= 1 for gap in phase['intraGaps'])
        for phase in value['phases']), value['phases'])
    adjacent = [phase for phase in value['phases'] if phase['interGap'] is not None]
    proof.check(stage + '-inter-phase-margin-and-gap12', len(adjacent) == len(value['phases']) - 1
                and all(phase['marginTop'] == 12 and abs(phase['interGap'] - 12) <= 1 for phase in adjacent), adjacent)
    proof.check(stage + '-command-summary-padding8-and-two-line-clamp', bool(value['summaries']) and all(
        v['padding'] == [8, 8, 8, 8] and v['clamp'] == '2' and v['overflow'] == 'hidden'
        and v['height'] <= 2 * v['line'] + 1 for v in value['summaries']), value['summaries'])
    proof.check(stage + '-env-status-and-right-caret-separate', all(
        v['env'] and v['status'] and v['separate'] and v['right'] for v in value['summaries']), value['summaries'])
    footer = value['fixed']['.composer-footer']
    proof.check(stage + '-no-overflow-composer-in-viewport', not value['overflow'] and footer['y'] >= 0
                and footer['y'] + footer['height'] <= value['viewport']['height'] + 1, value['fixed'])
    proof.check(stage + '-reduced-motion', value['reduced'] and all(
        all(float(t.strip().removesuffix('s')) <= .001 for t in v['duration'].split(','))
        and (v['animation'] == 'none' or all(float(t.strip().removesuffix('s')) <= .001
                                          for t in v['animationDuration'].split(',')))
        for v in value['motion']), value['motion'])
    await targets(proof, frame, stage)
    return value


async def short_checks(proof, page, frame):
    await integration.collapse(frame)
    proof.check('short-default-scenario', await frame.locator('#preview').get_attribute('data-scenario') == 'short')
    actual = {selector: (await frame.locator(selector).text_content()).strip() for selector in SHORT_COPY}
    proof.check('short-original-copy-and-ids', actual == SHORT_COPY and await frame.locator('.event').evaluate_all(
        'es=>es.map(e=>e.id)') == SHORT_IDS and await frame.locator('#local-output').count() == 1, actual)
    await structure(proof, frame, 'short')
    before = await style_contract(proof, frame, 'short-closed')
    await press(frame, '#command-local > summary', 'Space')
    await integration.settle(frame)
    first_open = await frame.evaluate(STYLE)
    first_positions = {stage: next(s['x'] for s in metrics['summaries'] if s['id'] == 'command-local')
                       for stage, metrics in [('closed', before), ('open', first_open)]}
    proof.check('short-first-command-title-terminal-caret-x-stable', all(
        first_positions['closed'][part] is not None and first_positions['open'][part] is not None
        and abs(first_positions['closed'][part] - first_positions['open'][part]) <= 1
        for part in ('title', 'terminal', 'caret')), first_positions)
    for selector in ('#thought-local', '#thought-remote', '#command-remote'):
        await press(frame, selector + ' > summary', 'Space')
    await integration.settle(frame)
    after = await style_contract(proof, frame, 'short-open')
    proof.metrics['short-density'] = {
        stage: {'eventSpan': metrics['eventGeometry']['span'],
                'eventHeights': [e['height'] for e in metrics['eventGeometry']['events']],
                'composerHeight': metrics['composer']['height'],
                'composerFooterHeight': metrics['fixed']['.composer-footer']['height']}
        for stage, metrics in [('closed', before), ('first-command-open', first_open), ('all-open', after)]}
    for stage, density in proof.metrics['short-density'].items():
        proof.check('short-' + stage + '-event-span-no-extra-spacing', len(density['eventHeights']) == 4
                    and abs(density['eventSpan'] - sum(density['eventHeights']) - 12) <= 1, density)
    proof.check('short-expansion-keeps-composer-height', all(
        abs(metrics['composer']['height'] - before['composer']['height']) <= 1
        and abs(metrics['fixed']['.composer-footer']['height'] - before['fixed']['.composer-footer']['height']) <= 1
        for metrics in (first_open, after)), proof.metrics['short-density'])
    proof.check('short-labels-and-fonts-stay-identical-open-closed', before['fonts'] == after['fonts']
                and [v['label'] for v in before['summaries']] == [v['label'] for v in after['summaries']])
    proof.check('short-summary-geometry-no-open-jump', all(
        a['padding'] == b['padding'] and abs(a['summary']['height'] - b['summary']['height']) <= 1
        for a, b in zip(before['summaries'], after['summaries'])))
    await proof.shot(frame, 'short-contract-open')
    # A keyboard-like resize is not an Android IME test; it specifically catches
    # font fallback and footer squeezing when width AND remaining height are small.
    original = page.viewport_size
    try:
        await frame.locator('#draft').fill('Synthetic keyboard draft')
        await frame.locator('#draft').focus()
        await page.set_viewport_size({'width': 320, 'height': 412})
        await integration.settle(frame)
        await style_contract(proof, frame, 'short-narrow-keyboard')
        proof.check('keyboard-draft-keeps-focus', await frame.locator('#draft').evaluate('e=>e===document.activeElement'))
        await proof.shot(frame, 'short-narrow-keyboard')
    finally:
        await page.set_viewport_size(original)
        await frame.locator('#draft').fill('')
        await integration.collapse(frame)


async def expanders(proof, frame, selector, stage):
    buttons = frame.locator(selector + ':visible')
    proof.check(stage + '-has-expand-controls', await buttons.count() > 0)
    for index, button in enumerate(await buttons.all()):
        control = await button.get_attribute('aria-controls')
        assert control, f'{selector} #{index}: missing aria-controls'
        body = frame.locator('[id=' + json.dumps(control) + ']')
        await expect(body).to_have_count(1)
        await expect(button).to_have_attribute('aria-expanded', 'false')
        owner = await button.evaluate('e=>e.closest(".event").id')
        proof.check(f'{stage}-{index}-body-owned-by-event', await body.evaluate(
            '(e,id)=>e.closest(".event")?.id===id', owner))
        raw = await body.text_content()
        small = await body.evaluate('e=>({height:e.clientHeight,scroll:e.scrollHeight})')
        proof.check(f'{stage}-{index}-only-on-overflow', small['scroll'] > small['height'] + 1, small)
        await button.press('Enter')
        await expect(button).to_have_attribute('aria-expanded', 'true')
        large = await body.evaluate('e=>({height:e.clientHeight,scroll:e.scrollHeight})')
        proof.check(f'{stage}-{index}-full-text-readable', large['height'] > small['height']
                    and large['scroll'] <= large['height'] + 1 and await body.text_content() == raw, large)
        proof.check(f'{stage}-{index}-descriptive-collapse-label', bool(re.search('свернуть', await button.inner_text(), re.I)))
        await button.press('Space')
        await expect(button).to_have_attribute('aria-expanded', 'false')
        proof.check(f'{stage}-{index}-collapse-preserves-all-text', await body.text_content() == raw
                    and abs(await body.evaluate('e=>e.clientHeight') - small['height']) <= 1)


async def long_checks(proof, page, frame):
    await choose(frame, 'long')
    await structure(proof, frame, 'long')
    phases = await frame.locator('.phase').count()
    proof.check('long-at-least-four-thought-tool-phases', phases >= 4
                and await frame.locator('.thought').count() >= 4 and await frame.locator('.command').count() >= 4, phases)
    closed = await style_contract(proof, frame, 'long-closed')
    for summary in await frame.locator('.event > summary').all():
        await summary.press('Space')
    opened = await style_contract(proof, frame, 'long-open')
    proof.check('long-labels-fonts-and-composer-stable', closed['fonts'] == opened['fonts']
                and [v['label'] for v in closed['summaries']] == [v['label'] for v in opened['summaries']]
                and integration.rects_equal(closed['fixed'], opened['fixed']))
    await integration.carets(proof, frame, 'long-open')
    commands = await frame.locator('.command-line').all_text_contents()
    proof.check('long-realistic-paths-and-shell-chains', any(len(c) >= 180 and re.search(r'(?:/[^\s/]+){5}', c)
                and ('&&' in c or ' | ' in c) for c in commands), commands)
    thoughts = await frame.locator('.thought').evaluate_all(r"""es=>es.map(e=>({
     label:e.querySelector('.event-label').textContent.trim(),
     raw:[...e.querySelectorAll('.thought-text')].map(t=>t.textContent).join('\n'),
     buttons:e.querySelectorAll('.reason-toggle').length,nested:e.querySelectorAll('details').length
    }))""")
    for i, thought in enumerate(thoughts):
        headings = re.findall(r'^\s*#{1,6}\s+(.+?)\s*#*\s*$', thought['raw'], re.M)
        headings += re.findall(r'^\s*\*\*(.+?)\*\*\s*$', thought['raw'], re.M)
        proof.check(f'long-thought-{i}-title-from-raw-or-fallback',
                    thought['label'] in headings if headings else thought['label'] == 'Рассуждение', thought)
        proof.check(f'long-thought-{i}-one-toggle-no-nested-details', thought['nested'] == 0
                    and thought['buttons'] <= 1 and (len(thought['raw']) < 600 or thought['buttons'] == 1),
                    {'rawLength': len(thought['raw']), 'buttons': thought['buttons'], 'nested': thought['nested']})
    proof.check('long-contains-genuinely-long-raw-reasoning', any(len(t['raw']) >= 600 for t in thoughts))
    await expanders(proof, frame, '.reason-toggle', 'long-reasoning')
    await expanders(proof, frame, '.output-toggle', 'long-output')
    windows = await frame.locator('.command-output').evaluate_all(r"""es=>es.map(e=>{
     const window=e.closest('.output-window')||e;
     return {overflow:window.scrollHeight>window.clientHeight+1,
      buttons:[...e.closest('.command').querySelectorAll('.output-toggle')].filter(b=>b.checkVisibility()).length};
    })""")
    proof.check('long-no-output-toggle-for-short-output', all(v['buttons'] == int(v['overflow']) for v in windows), windows)
    # Long fixture must include literal hostile-looking source, otherwise a
    # zero-script-elements assertion would be vacuous rather than an escape test.
    raw = '\n'.join(await frame.locator('.thought-text,.command-output').all_text_contents())
    proof.check('long-raw-escape-adversarial-fixture-present', bool(re.search(r'<(?:script|img|svg)\b', raw, re.I)))
    unsafe = await frame.locator('.thought-text,.command-output').evaluate_all(r"""es=>es.flatMap(e=>
     [...e.querySelectorAll('*')].filter(c=>c.matches('script,img,svg,iframe,object,embed')||
      [...c.attributes].some(a=>/^on/i.test(a.name)||/javascript:/i.test(a.value))).map(c=>c.outerHTML))""")
    proof.check('long-raw-is-text-not-executable-markup', not unsafe, unsafe)
    await targets(proof, frame, 'long-expanded-controls')
    await proof.shot(frame, 'long-expanded')


async def advance_until(page, frame, predicate, description, budget=BUDGET_MS):
    elapsed = 0
    trace = []
    while True:
        snap = await frame.evaluate(SNAPSHOT)
        signature = (snap['state'], [(e['id'], e['state'], e['output']) for e in snap['events']])
        if not trace or signature != trace[-1][0]:
            trace.append((signature, elapsed))
        if predicate(snap):
            return snap, elapsed
        if elapsed >= budget:
            raise AssertionError(f'{description} not observed within {budget}ms; trace={trace!r}')
        await page.clock.run_for(TICK_MS)
        elapsed += TICK_MS


async def start(frame):
    await press(frame, '#simulationStart')
    return await frame.evaluate(SNAPSHOT)


async def local_failure(proof, page, frame, prefix):
    await choose(frame, 'execution')
    initial = await frame.evaluate(SNAPSHOT)
    proof.check(prefix + '-initially-idle', not initial['approval'] and not initial['pending']
                and bool(re.search('idle|готов|не запущ|ожида.*запуск|запустите', initial['state'], re.I))
                and not any(has_state(e, 'running') for e in initial['events']), initial)
    await page.clock.run_for(BUDGET_MS * 2)
    idle = await frame.evaluate(SNAPSHOT)
    proof.check(prefix + '-no-autoplay', all(initial[k] == idle[k] for k in ('events', 'answer', 'state', 'approval')))
    snap = await start(frame)
    thought = active(snap, 'thought')
    proof.check(prefix + '-start-thought-running-auto-open', bool(thought and thought['open']), snap)
    assert thought, 'Start must synchronously render the running thought'
    local, elapsed = await advance_until(page, frame, lambda s: bool(command(s, 'Local') and
        has_state(command(s, 'Local'), 'running') and command(s, 'Local')['output'].strip()), 'local partial output')
    current = command(local, 'Local')
    running_style = await style_contract(proof, frame, prefix + '-local-running')
    proof.check(prefix + '-local-running-state-readable', local['stateVisible'] and len(local['state']) >= 5, local['state'])
    proof.check(prefix + '-local-running-output-auto-open', current['open'] and bool(current['output'].strip()), current)
    old = next(e for e in local['events'] if e['id'] == thought['id'])
    proof.check(prefix + '-completed-auto-thought-collapses', not old['open'] and not has_state(old, 'running'), old)
    failed, used = await advance_until(page, frame, lambda s: bool(command(s, 'Local') and
        has_state(command(s, 'Local'), 'error')), 'local error', BUDGET_MS - elapsed)
    elapsed += used
    error = command(failed, 'Local')
    explanation = error['visibleText'].replace(error['label'], '').replace(error['environment'], '').strip()
    explanation = re.sub(r'ошибка|error|failed', '', explanation, flags=re.IGNORECASE).strip()
    proof.check(prefix + '-local-error-visible-and-concise', bool(re.search('ошиб|не удалось|error|failed', error['visibleText'], re.I))
                and 12 <= len(explanation) <= 300, {'event': error, 'explanation': explanation})
    proof.check(prefix + '-partial-output-preserved-after-error', current['output'].strip() in error['output'], error['output'])
    proof.check(prefix + '-finished-auto-command-collapses', not error['open'], error)
    await targets(proof, frame, prefix + '-failure')
    next_thought, used = await advance_until(page, frame, lambda s: bool(active(s, 'thought') and
        active(s, 'thought')['id'] != thought['id']), 'new thought after local error', BUDGET_MS - elapsed)
    elapsed += used
    proof.check(prefix + '-new-reasoning-after-failure', active(next_thought, 'thought')['open'], next_thought)
    waiting, used = await advance_until(page, frame, lambda s: s['approval'], 'remote approval', BUDGET_MS - elapsed)
    elapsed += used
    remote = command(waiting, 'SSH')
    proof.check(prefix + '-remote-waits-without-running', bool(remote and has_state(remote, 'waiting'))
                and not has_state(remote, 'running') and not waiting['pending'], waiting)
    await expect(frame.locator('#approveDemo')).to_be_visible()
    await expect(frame.locator('#denyDemo')).to_be_visible()
    proof.check(prefix + '-state-readable', waiting['stateVisible'] and len(waiting['state']) >= 5, waiting['state'])
    await structure(proof, frame, prefix + '-waiting')
    await targets(proof, frame, prefix + '-waiting')
    waiting_style = await style_contract(proof, frame, prefix + '-approval')
    proof.check(prefix + '-streaming-does-not-squeeze-composer',
                integration.rects_equal(running_style['fixed'], waiting_style['fixed']))
    await page.clock.run_for(BUDGET_MS * 2)
    still = await frame.evaluate(SNAPSHOT)
    proof.check(prefix + '-approval-never-auto-accepts', waiting['events'] == still['events']
                and still['approval'] and waiting['state'] == still['state'])
    proof.metrics[prefix + '-elapsed-to-approval-ms'] = elapsed
    replay_signature = [{k: e[k] for k in ('id', 'kind', 'state', 'label', 'output', 'environment')} for e in waiting['events']]
    if prefix == 'approve':
        proof.metrics['deterministic-approval'] = {'events': replay_signature, 'elapsed': elapsed}
    else:
        proof.check('replay-identical-output-states-and-timing', proof.metrics.get('deterministic-approval') ==
                    {'events': replay_signature, 'elapsed': elapsed})
    await proof.shot(frame, prefix + '-approval')
    return waiting, elapsed


async def approve_checks(proof, page, frame):
    _, elapsed = await local_failure(proof, page, frame, 'approve')
    await press(frame, '#approveDemo')
    running = await frame.evaluate(SNAPSHOT)
    remote = command(running, 'SSH')
    proof.check('approve-ssh-running-auto-open-no-panel', bool(remote and has_state(remote, 'running') and remote['open'])
                and not running['approval'], running)
    done, used = await advance_until(page, frame, lambda s: bool(command(s, 'SSH') and
        has_state(command(s, 'SSH'), 'completed')), 'SSH completed', BUDGET_MS - elapsed)
    result = command(done, 'SSH')
    proof.check('approve-completed-output-and-readable-state', bool(result['output'].strip())
                and done['stateVisible'] and bool(re.search('заверш|выполн|готов|completed', done['state'], re.I))
                and not result['open'] and not done['approval'] and not done['pending'], done)
    proof.check('approve-total-timed-phases-within-15s', elapsed + used <= BUDGET_MS, elapsed + used)
    await press(frame, '[id=' + json.dumps(result['id']) + '] > summary', 'Space')
    await expect(frame.locator('[id=' + json.dumps(result['id']) + '] .command-output')).to_be_visible()
    await proof.shot(frame, 'execution-completed-output')
    await page.clock.run_for(BUDGET_MS * 2)
    proof.check('approve-terminal-state-stable', (await frame.evaluate(SNAPSHOT))['state'] == done['state'])
    # Replay is the Start button, not a private reset or direct fixture write.
    replay = await start(frame)
    proof.check('replay-resets-error-output-and-approval', bool(active(replay, 'thought')) and not replay['approval']
                and not any(has_state(e, 'error') or has_state(e, 'completed') for e in replay['events'])
                and all(not e['output'] for e in replay['events'] if e['kind'] == 'command'), replay)
    await press(frame, '#simulationStop')


async def deny_checks(proof, page, frame):
    waiting, _ = await local_failure(proof, page, frame, 'deny')
    errors = [e['id'] for e in waiting['events'] if has_state(e, 'error')]
    await press(frame, '#denyDemo')
    denied = await frame.evaluate(SNAPSHOT)
    remote = command(denied, 'SSH')
    proof.check('deny-cancelled-neutral-no-new-error', bool(remote and has_state(remote, 'cancelled'))
                and not has_state(remote, 'error') and not remote['output'].strip()
                and errors == [e['id'] for e in denied['events'] if has_state(e, 'error')]
                and not denied['approval'] and not denied['pending']
                and bool(re.search('отмен|cancel', denied['state'], re.I)), denied)
    await page.clock.run_for(BUDGET_MS * 2)
    later = await frame.evaluate(SNAPSHOT)
    proof.check('deny-no-late-ssh-or-state-change', all(denied[k] == later[k]
                for k in ('events', 'state', 'answer', 'approval')))
    await proof.shot(frame, 'execution-denied')


async def stop_checks(proof, page, frame):
    for stage in ('thought', 'local', 'waiting', 'ssh'):
        await choose(frame, 'execution')
        await start(frame)
        if stage == 'local':
            await advance_until(page, frame, lambda s: bool(command(s, 'Local') and has_state(command(s, 'Local'), 'running')), 'stop local')
        if stage in ('waiting', 'ssh'):
            await advance_until(page, frame, lambda s: s['approval'], 'stop approval')
        if stage == 'ssh':
            await press(frame, '#approveDemo')
        await press(frame, '#simulationStop')
        stopped = await frame.evaluate(SNAPSHOT)
        proof.check('stop-' + stage + '-cancels-every-timer-and-pending-approval', not stopped['pending']
                    and not stopped['approval'] and not any(has_state(e, 'running') for e in stopped['events'])
                    and bool(re.search('останов|отмен|cancel|stopp', stopped['state'], re.I)), stopped)
        await page.clock.run_for(BUDGET_MS * 2)
        later = await frame.evaluate(SNAPSHOT)
        proof.check('stop-' + stage + '-no-late-ticks', all(stopped[k] == later[k] for k in ('events', 'state', 'answer', 'approval')))
    replay = await start(frame)
    proof.check('stop-can-replay', bool(active(replay, 'thought')), replay)
    await press(frame, '#simulationStop')


async def manual_checks(proof, page, frame):
    for keep_open in (False, True):
        await choose(frame, 'short')
        await choose(frame, 'execution')
        snap = await start(frame)
        thought = active(snap, 'thought')
        assert thought, 'Missing running thought'
        manual_ids = [thought['id']]

        async def take_control(event_id, keep_open=keep_open):
            selector = '[id=' + json.dumps(event_id) + ']'
            assert await frame.locator(selector).evaluate('e=>e.open'), 'Current unfinished event must auto-open'
            await press(frame, selector + ' > summary', 'Space')
            if keep_open:
                await press(frame, selector + ' > summary', 'Space')

        await take_control(thought['id'])
        violations = []
        local_seen = False
        waiting = False
        for _ in range(BUDGET_MS // TICK_MS):
            snap = await frame.evaluate(SNAPSHOT)
            for event in snap['events']:
                if event['id'] in manual_ids and event['open'] != keep_open:
                    violations.append({'id': event['id'], 'state': event['state'], 'open': event['open']})
            local = command(snap, 'Local')
            if local and has_state(local, 'running') and not local_seen:
                local_seen = True
                manual_ids.append(local['id'])
                await take_control(local['id'])
            if snap['approval']:
                waiting = True
                break
            await page.clock.run_for(TICK_MS)
        mode = 'open' if keep_open else 'closed'
        proof.check('manual-' + mode + '-survives-all-thought-and-command-ticks',
                    waiting and local_seen and not violations, violations)
        assert waiting, 'Manual-state test never reached approval'
        await press(frame, '#approveDemo')
        remote = command(await frame.evaluate(SNAPSHOT), 'SSH')
        assert remote and has_state(remote, 'running'), 'Missing running SSH command'
        await take_control(remote['id'])
        for _ in range(BUDGET_MS // TICK_MS):
            remote = command(await frame.evaluate(SNAPSHOT), 'SSH')
            if remote['open'] != keep_open:
                violations.append(remote)
            if has_state(remote, 'completed'):
                break
            await page.clock.run_for(TICK_MS)
        proof.check('manual-' + mode + '-survives-ssh-completion',
                    has_state(remote, 'completed') and not violations, violations)


async def internal_output_focus_checks(proof, page, frame):
    for activation in ('pointer', 'keyboard'):
        await choose(frame, 'execution')
        await start(frame)
        running, _ = await advance_until(page, frame, lambda s: bool(command(s, 'Local') and
            has_state(command(s, 'Local'), 'running') and command(s, 'Local')['outputToggleVisible']),
            'running command with overflowing output')
        local = command(running, 'Local')
        owner = frame.locator('[id=' + json.dumps(local['id']) + ']')
        button = owner.locator('.output-toggle')
        body = frame.locator('[id=' + json.dumps(await button.get_attribute('aria-controls')) + ']')
        await expect(owner).to_have_attribute('open', '')
        await expect(button).to_have_attribute('aria-expanded', 'false')
        proof.check(activation + '-output-starts-auto-open-not-manual',
                    await owner.get_attribute('data-manual') is None)
        small = await body.evaluate('e=>({height:e.clientHeight,scroll:e.scrollHeight})')
        proof.check(activation + '-output-really-overflows', small['scroll'] > small['height'] + 1, small)
        if activation == 'pointer':
            # Real input while the simulation clock is paused. Only scrolling is
            # evaluated; no synthetic click or details/manual state is injected.
            await button.evaluate('e=>e.scrollIntoView({block:"center"})')
            box = await button.bounding_box()
            assert box, 'Output toggle has no hit target'
            await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        else:
            await button.press('Enter')
        await expect(button).to_have_attribute('aria-expanded', 'true')
        await expect(button).to_be_focused()
        handle = await button.element_handle()
        proof.check(activation + '-output-toggle-marks-owner-manual-open',
                    await owner.get_attribute('data-manual') == 'open')
        violations = []
        error_seen = False
        for _ in range(BUDGET_MS // TICK_MS):
            snap = await frame.evaluate(SNAPSHOT)
            current = command(snap, 'Local')
            retained = await handle.evaluate('e=>e.isConnected&&e===document.activeElement')
            if not current['open'] or not retained or await button.get_attribute('aria-expanded') != 'true':
                violations.append({'state': current['state'], 'open': current['open'], 'sameFocusedButton': retained})
            if has_state(current, 'error'):
                error_seen = True
                break
            await page.clock.run_for(TICK_MS)
        proof.check(activation + '-expand-output-error-keeps-details-and-keyboard-focus',
                    error_seen and not violations, violations)
        assert error_seen, 'Expanded-output test never reached local error'
        await expect(button).to_be_visible()
        await expect(button).to_be_focused()
        large = await body.evaluate('e=>({height:e.clientHeight,scroll:e.scrollHeight})')
        proof.check(activation + '-error-full-output-readable-and-preserved',
                    large['height'] > small['height'] and large['scroll'] <= large['height'] + 1
                    and local['output'] in (await body.text_content())
                    and 'ECONNREFUSED' in (await body.text_content()), large)
        await targets(proof, frame, activation + '-expanded-output-error')
        await proof.shot(frame, activation + '-expanded-output-error-focus')
        # A further phase must not steal focus or collapse this manually-read output.
        await advance_until(page, frame, lambda s: bool(active(s, 'thought')), 'reasoning after expanded local error')
        proof.check(activation + '-expanded-output-focus-survives-next-phase',
                    await owner.evaluate('e=>e.open') and await handle.evaluate('e=>e.isConnected&&e===document.activeElement'))
        await handle.dispose()
        await press(frame, '#simulationStop')


async def replay_checks(proof, page, frame):
    for stage in ('local', 'waiting'):
        await choose(frame, 'short')
        await choose(frame, 'execution')
        await start(frame)
        if stage == 'local':
            await advance_until(page, frame, lambda s: bool(command(s, 'Local') and has_state(command(s, 'Local'), 'running')), 'replay local')
        else:
            await advance_until(page, frame, lambda s: s['approval'], 'replay approval')
        before = await frame.evaluate(SNAPSHOT)
        restarted = await start(frame)
        old_ids = {t['id'] for t in before['pending']}
        proof.check('replay-' + stage + '-clears-old-timers-and-approval',
                    not old_ids.intersection(t['id'] for t in restarted['pending']) and not restarted['approval']
                    and bool(active(restarted, 'thought'))
                    and all(not e['output'] for e in restarted['events'] if e['kind'] == 'command'), restarted)
        waiting, elapsed = await advance_until(page, frame, lambda s: s['approval'], 'replayed approval')
        signature = [{k: e[k] for k in ('id', 'kind', 'state', 'label', 'output', 'environment')} for e in waiting['events']]
        proof.check('replay-' + stage + '-same-deterministic-sequence', proof.metrics.get('deterministic-approval') ==
                    {'events': signature, 'elapsed': elapsed}, {'events': signature, 'elapsed': elapsed})
        await press(frame, '#simulationStop')


async def scroll_checks(proof, page, frame):
    for distance in (0, 47, 48, 96):
        await choose(frame, 'execution')
        await start(frame)
        # Only scrolling is written; content height must come from the fixture,
        # not spacer injection. Use the short viewport to exercise actual growth.
        await advance_until(page, frame, lambda s, distance=distance: bool(command(s, 'Local') and has_state(command(s, 'Local'), 'running'))
                            and s['feed']['total'] - s['feed']['height'] > distance + 8, 'scrollable running local command')
        await frame.locator('#feed').evaluate('(e,d)=>{e.scrollTop=e.scrollHeight-e.clientHeight-d}', distance)
        before = await frame.evaluate(SNAPSHOT)
        style = await frame.evaluate(STYLE)
        later, _ = await advance_until(page, frame, lambda s, before=before: bool(command(s, 'Local') and
            command(s, 'Local')['output'] != command(before, 'Local')['output']), 'streamed output growth')
        if distance < 48:
            proof.check(f'scroll-distance-{distance}-follows-bottom', later['feed']['distance'] < 2,
                        {'before': before['feed'], 'after': later['feed']})
        else:
            proof.check(f'scroll-distance-{distance}-preserves-reading-position', abs(before['feed']['top'] - later['feed']['top']) <= 1,
                        {'before': before['feed'], 'after': later['feed']})
        proof.check(f'scroll-distance-{distance}-composer-fixed', integration.rects_equal(style['fixed'], (await frame.evaluate(STYLE))['fixed']))
        await press(frame, '#simulationStop')


async def switch_checks(proof, page, frame):
    for stage in ('thought', 'local', 'waiting', 'ssh'):
        for scenario in ('long', 'short'):
            await choose(frame, 'execution')
            await start(frame)
            if stage == 'local':
                await advance_until(page, frame, lambda s: bool(command(s, 'Local') and has_state(command(s, 'Local'), 'running')), 'switch local')
            if stage in ('waiting', 'ssh'):
                await advance_until(page, frame, lambda s: s['approval'], 'switch approval')
            if stage == 'ssh':
                await press(frame, '#approveDemo')
            await choose(frame, scenario)
            before = await frame.locator('.thread').inner_html()
            state = await frame.evaluate(SNAPSHOT)
            proof.check(f'switch-{stage}-{scenario}-cancels-timers', not state['pending'] and not state['approval'], state['pending'])
            await page.clock.run_for(BUDGET_MS * 2)
            proof.check(f'switch-{stage}-{scenario}-no-stale-output', await frame.locator('.thread').inner_html() == before)
    await choose(frame, 'execution')
    proof.check('switch-back-execution-is-idle', not (await frame.evaluate(SNAPSHOT))['pending']
                and not any(has_state(e, 'running') for e in (await frame.evaluate(SNAPSHOT))['events']))


async def reachable_controls(proof, frame, scope, stage):
    controls = frame.locator(scope).locator('button,summary,textarea,input,select,a[href],[role="button"]')
    reached = []
    for control in await controls.all():
        if not await control.is_visible():
            continue
        await control.scroll_into_view_if_needed()
        # Center within each real scroll ancestor: nearest-edge alignment can
        # leave a fractional pixel clipped by the nested feed's boundary.
        await control.evaluate('e=>e.scrollIntoView({block:"center",inline:"nearest",behavior:"instant"})')
        await expect(control).to_be_in_viewport(ratio=1)
        info = await control.evaluate(r"""e=>{
         const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
         return {name:e.id||e.getAttribute('aria-label')||e.textContent.trim(),width:r.width,height:r.height,
          hit:!!hit&&(hit===e||e.contains(hit)),x:r.x,y:r.y};
        }""")
        reached.append(info)
        proof.check(stage + '-reachable-' + info['name'], info['width'] >= 44 and info['height'] >= 44 and info['hit'], info)
    proof.check(stage + '-has-reachable-controls', bool(reached))
    proof.metrics[stage + '-controls'] = reached


async def low_height_checks(proof, page, frame, baseline):
    original = page.viewport_size
    draft = 'First synthetic line\nSecond synthetic line\nThird synthetic line\nFourth synthetic line\nFifth synthetic line'
    try:
        for width, height in ((412, 360), (320, 300)):
            await page.set_viewport_size({'width': width, 'height': height})
            for scenario in ('short', 'execution'):
                stage = f'low-{width}x{height}-{scenario}'
                await choose(frame, scenario)
                await frame.locator('#draft').fill(draft)
                await integration.settle(frame)
                metrics = await frame.evaluate(r"""() => {
                 const q=s=>document.querySelector(s),root=q('#preview'),feed=q('#feed'),draft=q('#draft');
                 const r=getComputedStyle(root),f=getComputedStyle(feed),d=getComputedStyle(draft);
                 return {root:{display:r.display,overflow:r.overflowY,client:root.clientHeight,scroll:root.scrollHeight},
                  feed:{height:feed.getBoundingClientRect().height,min:parseFloat(f.minHeight),max:parseFloat(f.maxHeight),
                   padding:[f.paddingTop,f.paddingBottom]},footerPosition:getComputedStyle(q('.composer-footer')).position,
                  draft:{height:draft.getBoundingClientRect().height,font:parseFloat(d.fontSize),min:parseFloat(d.minHeight),
                   scroll:draft.scrollHeight,client:draft.clientHeight},
                  model:[...document.querySelectorAll('.model')].map(e=>parseFloat(getComputedStyle(e).fontSize)),
                  options:[...document.querySelectorAll('.option')].map(e=>parseFloat(getComputedStyle(e).fontSize)),
                  horizontal:root.scrollWidth>root.clientWidth+1||document.documentElement.scrollWidth>innerWidth+1,
                  documentScroll:scrollY};
                }""")
                proof.metrics[stage] = metrics
                proof.check(stage + '-deliberate-outer-scroll-not-shrink',
                            metrics['root']['display'] == 'block' and metrics['root']['overflow'] == 'auto'
                            and metrics['root']['client'] == height and metrics['root']['scroll'] > height
                            and metrics['feed']['min'] == 120 and metrics['feed']['max'] == 240
                            and 120 <= metrics['feed']['height'] <= 240
                            and metrics['feed']['padding'] == ['8px', '8px']
                            and metrics['footerPosition'] == 'relative'
                            and not metrics['horizontal'] and metrics['documentScroll'] == 0, metrics)
                proof.check(stage + '-five-line-draft-fonts-and-minimum-sizes',
                            await frame.locator('#draft').input_value() == draft
                            and metrics['draft']['min'] >= 44 and metrics['draft']['height'] == 96
                            and metrics['draft']['scroll'] > metrics['draft']['client']
                            and metrics['draft']['font'] >= 15
                            and len(metrics['model']) == 1 and min(metrics['model']) >= 13
                            and len(metrics['options']) == 2 and min(metrics['options']) >= 12, metrics)
                # Wheel over the header, not the nested feed/textarea: the actual
                # outer scroller must bring the composer into reach.
                header = frame.locator('.preview-header')
                await header.scroll_into_view_if_needed()
                before = await frame.locator('#preview').evaluate('e=>e.scrollTop')
                box = await header.bounding_box()
                assert box, 'Preview header has no wheel target'
                await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                await page.mouse.wheel(0, 2000)
                for _ in range(60):
                    after = await frame.locator('#preview').evaluate('e=>e.scrollTop')
                    if after > before + 1:
                        break
                    await integration.settle(frame)
                proof.check(stage + '-real-wheel-scrolls-outer-preview', after > before + 1,
                            {'before': before, 'after': after})
                await proof.shot(frame, stage + '-outer-scrolled')
                await reachable_controls(proof, frame, '#preview', stage)
                await targets(proof, frame, stage)
                await frame.locator('#demoSend').scroll_into_view_if_needed()
                await expect(frame.locator('#demoSend')).to_be_in_viewport(ratio=1)
                await proof.shot(frame, stage + '-composer-reachable')
                await frame.locator('#previewSettings').click()
                await reachable_controls(proof, frame, '#settings', stage + '-settings')
                await frame.locator('#settings [data-close-dialog]').click()
                proof.check(stage + '-parent-unchanged-and-draft-preserved',
                            await page.evaluate(integration.PARENT_SNAPSHOT) == baseline
                            and await frame.locator('#draft').input_value() == draft)
                probe = await frame.evaluate('window.__threadProof')
                proof.check(stage + '-no-network-native-storage', not any(probe[k] for k in ('network', 'native', 'storage')), probe)
    finally:
        await page.set_viewport_size(original)
        await frame.locator('#draft').fill('')
        await choose(frame, 'short')
        await integration.settle(frame)
    await style_contract(proof, frame, 'normal-layout-restored-after-low-height')


async def scenario_checks(proof, page, frame, theme, baseline):
    await proof.step('short-contract', lambda: short_checks(proof, page, frame), frame)
    await press(frame, '#previewSettings')
    selectors = [f'#settings button[data-scenario="{name}"]' for name in ('short', 'long', 'execution')]
    missing = [selector for selector in selectors if await frame.locator(selector).count() != 1]
    for selector in selectors:
        proof.check('scenario-control-' + selector, selector not in missing)
    await targets(proof, frame, 'scenario-settings')
    await press(frame, '#settings [data-close-dialog]')
    if missing:
        for group in ('long-content', 'execution-approve', 'execution-deny', 'execution-stop', 'execution-manual',
                      'execution-internal-output-focus', 'low-height-multiline-reachability',
                      'execution-replay', 'execution-scroll', 'execution-switch', 'execution-close'):
            proof.check(group, False, {'blockedByMissingContract': missing})
        return frame
    await proof.step('long-content', lambda: long_checks(proof, page, frame), frame)
    await proof.step('low-height-multiline-reachability', lambda: low_height_checks(proof, page, frame, baseline), frame)
    await choose(frame, 'execution')
    missing = [selector for selector in ('#simulationStart', '#simulationStop', '#simulationState')
               if await frame.locator(selector).count() != 1]
    for selector in ('#simulationStart', '#simulationStop', '#simulationState'):
        proof.check('execution-control-' + selector, selector not in missing)
    if missing:
        proof.check('execution-contract', False, {'blockedByMissingContract': missing})
        await choose(frame, 'short')
        return frame
    await choose(frame, 'short')
    proof.check('scenarios-preserve-parent-before-clock', await page.evaluate(integration.PARENT_SNAPSHOT) == baseline)
    # Install only after legacy checks using real RAF have finished. Explicitly
    # resume before returning to their close/reopen/live-renderer checks.
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await page.clock.install(time=epoch)
    await page.clock.pause_at(epoch + timedelta(seconds=1))
    await frame.evaluate(TIMER_MONITOR)
    original_viewport = page.viewport_size
    try:
        for name, operation in [('approve', approve_checks), ('deny', deny_checks), ('stop', stop_checks),
                                ('manual', manual_checks), ('internal-output-focus', internal_output_focus_checks),
                                ('replay', replay_checks), ('switch', switch_checks)]:
            await proof.step('execution-' + name, lambda op=operation: op(proof, page, frame), frame)
            await choose(frame, 'short')
            proof.check('parent-unchanged-after-' + name, await page.evaluate(integration.PARENT_SNAPSHOT) == baseline)
            monitor = await frame.evaluate('window.__threadProof')
            proof.check('scenario-no-network-native-storage-' + name, not any(monitor[k] for k in ('network', 'native', 'storage')), monitor)
        await page.set_viewport_size({'width': 320, 'height': 412})
        await proof.step('execution-scroll', lambda: scroll_checks(proof, page, frame), frame)
        proof.check('scroll-preserves-parent', await page.evaluate(integration.PARENT_SNAPSHOT) == baseline)
        monitor = await frame.evaluate('window.__threadProof')
        proof.check('scroll-no-network-native-storage', not any(monitor[k] for k in ('network', 'native', 'storage')), monitor)
        await page.set_viewport_size(original_viewport)
        await choose(frame, 'short')
        await choose(frame, 'execution')
        await start(frame)
        await advance_until(page, frame, lambda s: bool(command(s, 'Local') and has_state(command(s, 'Local'), 'running')), 'close during local run')
        timers = await frame.evaluate('({scheduled:window.__scenarioProof.scheduled,pending:[...window.__scenarioProof.pending.values()]})')
        proof.check('simulation-uses-bounded-timeouts-not-intervals', bool(timers['scheduled']) and all(
            not t.get('interval') and 0 <= t['delay'] <= BUDGET_MS for t in timers['scheduled']), timers)
        # closePreview is public UI code. Calling it here lets us inspect timer
        # cleanup synchronously before its postMessage removes the real iframe.
        closed = await frame.evaluate('''() => {
         closePreview();return {pending:[...window.__scenarioProof.pending.values()],probe:window.__threadProof};
        }''')
        proof.check('close-clears-timers-before-iframe-removal', not closed['pending'], closed)
        proof.check('closing-fixture-no-network-native-storage', not any(closed['probe'][k]
                    for k in ('network', 'native', 'storage')), closed['probe'])
        await expect(page.locator('#threadPreview')).to_have_count(0)
        await page.clock.run_for(BUDGET_MS * 2)
        proof.check('close-no-late-parent-mutations', await page.evaluate(integration.PARENT_SNAPSHOT) == baseline)
        await page.clock.resume()
        frame = await integration.open_preview(page, theme)
        proof.check('reopen-fresh-short-after-running-close', await frame.locator('#preview').get_attribute('data-scenario') == 'short'
                    and await frame.locator('.event[open]').count() == 0 and await frame.locator('#draft').input_value() == '')
    except Exception as error:
        proof.check('execution-close-or-harness-error', False, f'{type(error).__name__}: {error}')
        await page.clock.resume()
        if frame.is_detached():
            frame = await integration.open_preview(page, theme)
    finally:
        await page.clock.resume()
        await page.set_viewport_size(original_viewport)
        if not frame.is_detached():
            await choose(frame, 'short')
            await integration.collapse(frame)
    return frame


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true', help='One dark narrow viewport; default runs both themes and all six viewport cases')
    parser.add_argument('--browser-executable', help='Explicit existing Chromium/Chrome binary; uses a fresh temporary profile')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    integration.OUT = OUT
    main_source = (integration.ASSETS / 'codex.html').read_text(encoding='utf-8')
    preview_source = (integration.ASSETS / 'thread-preview.html').read_text(encoding='utf-8')
    hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in
              [('codex.html', main_source), ('thread-preview.html', preview_source)]}
    results = [integration.source_checks(main_source, preview_source)]
    source_proof = integration.Proof('scenario-source', OUT)
    source_proof.check('public-runSimulation-exists', bool(re.search(r'function\s+runSimulation\s*\(', preview_source)))
    results.append(source_proof.report())
    main_source, count = re.subn(r'S\.bootP\s*=\s*boot\(\);(?=\s*</script>\s*</body>)',
                                 'S.bootP=Promise.resolve();', main_source)
    assert count == 1, 'Only the final app boot may be disabled'
    cases = [('dark', 320, 800)] if args.quick else [(theme, w, h) for theme in ('light', 'dark') for w, h in integration.VIEWPORTS]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=args.browser_executable)
        browser_info = {'version': browser.version, 'executable': args.browser_executable or 'Playwright default'}
        try:
            for theme, width, height in cases:
                results.append(await integration.run_case(browser, main_source, preview_source, theme, width, height, scenario_checks))
        finally:
            await browser.close()
    report = {'assetSha256': hashes, 'browser': browser_info, 'cases': cases, 'results': results,
              'notes': ['Actual Settings entry and sandboxed iframe; all legacy integration assertions retained.',
                        'Synthetic parent data only. No network/native/storage writes allowed.',
                        'Playwright clock advances actual timeout callbacks; UI state/DOM never injected.',
                        '320x412, 412x360 and 320x300 are keyboard-like viewports, not Android IME/device tests.',
                        'At heights <=400px outer preview scrolling is intentional; controls must remain fully reachable and at least 44px.',
                        'Missing scenarios fail explicitly; dependent checks are reported blocked, not passed.']}
    path = OUT / ('report-quick.json' if args.quick else 'report.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    checks = [check for result in results for check in result['checks']]
    failures = [check for check in checks if not check['ok']]
    print(f'{len(checks)-len(failures)}/{len(checks)} checks passed; {len(failures)} failed; report: {path}', flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
