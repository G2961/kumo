"""Offline Stage 2 Work/Changes/Settings proof against a snapshot of the real asset.

Run with ../.local/api-test-venv/Scripts/python.exe and a literal installed Chrome
in PLAYWRIGHT_BROWSER_EXECUTABLE. No downloads, provider, device, or app edits.
Default matrix: light/dark x 320/412 x 100/140% x 800/400 px height.
Only data is stubbed: messages/SSE, clipboard, and read-only Changes history.
Renderers, click/disclosure handlers, timing and layout remain production code.
Work dialog motion and Auto mode belong to the primary agent; Work is smoke only.
Disclosure rows animate ~200ms (think: grid-template-rows transition; activity
and Changes cards: WAAPI height with data-disclosure-hold); reduced motion
collapses them synchronously.
The old WORK 80 and UI218 suites are deliberately not invoked by this runner.

Reports count only checks actually reached; an interrupted scenario is ERROR,
not a fabricated pass for its remaining assertions. --case narrows a rerun.
"""
import argparse
import asyncio
import hashlib
import json
import re
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, expect

from reasoning_activity_proof import (
    A1, HTML, PATCH, SID, U1, activity_segment, boot_page, event,
    launch_browser, message, reason, seed, thought, tool, user,
)
from work_log_proof import BODY, DIALOG, close_work, open_work, settle, summary, work_id

RAW = 'REASON_START\n' + '\n'.join(f'Observation {i}: <literal> & preserved.' for i in range(45)) + '\nREASON_END'
OUTPUT = 'OUTPUT_START\n' + '\n'.join(f'line {i}: <xml> & preserved stdout' for i in range(60)) + '\nOUTPUT_END'
SOURCE = '[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))'
KINDS = ('local', 'ssh', 'reason', 'changes')

# Playwright is_visible alone treats a 1px sr-only box as visible. Measure paint
# extent through clipping ancestors as well as opacity/display/visibility.
PAINTED = r'''e=>{
  if(!e||!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
  let r=e.getBoundingClientRect(),l=r.left,t=r.top,b=r.bottom,right=r.right;
  for(let p=e;p;p=p.parentElement){
    const s=getComputedStyle(p),q=p.getBoundingClientRect();
    if(+s.opacity<.05||s.visibility!=='visible'||s.display==='none')return false;
    if(/inset\(50%/.test(s.clipPath)||/rect\(0px,? 0px,? 0px,? 0px\)/.test(s.clip))return false;
    if(/hidden|clip|auto|scroll/.test(s.overflowX)){l=Math.max(l,q.left);right=Math.min(right,q.right)}
    if(/hidden|clip|auto|scroll/.test(s.overflowY)){t=Math.max(t,q.top);b=Math.min(b,q.bottom)}
  }
  return right-l>2&&b-t>2;
}'''

# No production animation helper names, animation mocks, or forced open state.
# Measure both the row's actual layout and its revealed content on real frames.
INSTALL_SAMPLER = r'''()=>{
  window.polishRead=spec=>{
    const row=document.querySelector(spec.row),button=document.querySelector(spec.button),body=document.querySelector(spec.body);
    if(!row||!button||!body)throw Error('Disclosure fixture disappeared');
    const r=row.getBoundingClientRect();let b=body.getBoundingClientRect(),top=b.top,bottom=b.bottom;
    for(let p=body;p;p=p.parentElement){
      const s=getComputedStyle(p),q=p.getBoundingClientRect();
      if(/hidden|clip|auto|scroll/.test(s.overflowY)){top=Math.max(top,q.top);bottom=Math.min(bottom,q.bottom)}
      // The scrolling viewport may contain only part of a long output; do not
      // confuse scrolling with its disclosure's clipping geometry.
      if(p===row)break;
    }
    const logical=button.hasAttribute('aria-expanded')?button.getAttribute('aria-expanded')==='true':
      row.matches('details')?row.open:row.hasAttribute('data-open')||row.hasAttribute('data-live');
    const rendered=body.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    return {height:r.height,reveal:rendered?Math.max(0,bottom-top):0,logical,
      physicalOpen:row.matches('details')?row.open:logical,focused:document.activeElement===button};
  };
  window.polishArm=(spec,duration=650)=>{
    const trace={before:polishRead(spec),frames:[],clickAt:null};window.polishTrace=trace;
    window.polishDone=new Promise(resolve=>{
      const click=e=>{
        if(!e.target.closest(spec.button))return;
        document.removeEventListener('click',click,true);trace.clickAt=performance.now();
        const frame=now=>{
          trace.frames.push({ms:now-trace.clickAt,...polishRead(spec)});
          if(now-trace.clickAt<duration)requestAnimationFrame(frame);else resolve(trace);
        };requestAnimationFrame(frame);
      };document.addEventListener('click',click,true);
    });
  };
}'''


class Proof:
    def __init__(self, out):
        self.out = out
        self.checks = []
        self.measurements = {}

    def check(self, name, condition, detail=None):
        self.checks.append({'name': name, 'status': 'PASS' if condition else 'FAIL', 'detail': detail})

    def save(self, name, value):
        self.measurements[name] = value


@dataclass(frozen=True)
class Disclosure:
    row: str
    button: str
    body: str

    def data(self):
        return vars(self)


def disclosure(kind):
    if kind == 'changes':
        row = '#sheet .change-card[data-file="0"]'
        return Disclosure(row, row + ' > summary', row + ' .change-preview')
    row = BODY + ' ' + (thought(A1, 'r1') if kind == 'reason' else activity_segment(A1, kind))
    return Disclosure(row, row + (' > .think-head' if kind == 'reason' else ' > summary'),
                      row + (' .think-body' if kind == 'reason' else ' .activity-body'))


def part_for(kind, live=False, suffix=''):
    if kind == 'reason':
        return reason('r1', RAW + suffix, end=None if live else 7000)
    if kind == 'ssh':
        return tool('ssh', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': 'ls -la'},
                    json.dumps({'stdout': OUTPUT + suffix, 'stderr': '', **({} if live else {'exitCode': 0})}),
                    status='running' if live else 'completed')
    return tool('local', 'list', {'path': 'src'}, OUTPUT + suffix,
                status='running' if live else 'completed', metadata={'source': 'local', 'sentinel': '<raw>&'})


async def wait_rest(page):
    # Only setup/end-state settling. Motion assertions below use recorded frames.
    await page.wait_for_timeout(650)


async def read_disclosure(page, spec):
    return await page.evaluate('spec=>polishRead(spec)', spec.data())


async def click_state(page, spec, opened):
    if (await read_disclosure(page, spec))['logical'] != opened:
        await page.locator(spec.button).click()
    await wait_rest(page)
    current = await read_disclosure(page, spec)
    assert current['logical'] == opened and current['physicalOpen'] == opened, current


async def prepare_changes(page):
    await seed(page, [user()])
    # seed() refreshes the feed only; refresh the real header for its fixture sid.
    await page.evaluate('updateCommitButton()')
    patch = '--- a/src/fixture.js\n+++ b/src/fixture.js\n@@ -1,8 +1,8 @@\n' + ''.join(
        f'-before_{i}\n+after_{i}\n' for i in range(8))
    file = {'key': 'fixture-change', 'source': 'ssh', 'connectionID': 'conn_fixture',
            'file': 'src/fixture.js', 'patch': patch, 'additions': 8, 'deletions': 8}

    async def reads(route):
        path = urlsplit(route.request.url).path
        if route.request.method == 'GET' and path == '/session/status':
            await route.fulfill(json={SID: {'type': 'idle'}})
        elif route.request.method == 'GET' and path == '/session/' + SID + '/message':
            await route.fulfill(json=[user()])
        else:
            await route.fallback()

    await page.route('**/*', reads)
    # This bridge is data-only and read-only, not Android/native execution.
    await page.evaluate('''file=>{
      window.polishHistoryCalls=[];window.polishHistoryUnexpected=[];
      window.CodexNative={request(raw){
        const q=JSON.parse(raw);polishHistoryCalls.push(q.action);
        if(q.action==='historyList')codexNativeResult(q.id,{turns:[{commit:'fixture-only',title:'Fixture edits',files:[file]}]});
        else if(q.action==='historyShow')codexNativeResult(q.id,{files:[file]});
        else {polishHistoryUnexpected.push(q.action);codexNativeResult(q.id,{error:'Unexpected fixture bridge action'})}
      }};
    }''', file)
    await page.locator('#diffPanel').click()
    spec = disclosure('changes')
    await expect(page.locator(spec.button)).to_be_visible()
    # Load through the actual lazy history handler; measure cached disclosure,
    # rather than accidentally measuring replacement of a loading skeleton.
    await page.locator(spec.button).click()
    await expect(page.locator(spec.row)).to_have_attribute('data-loaded', 'true')
    await wait_rest(page)
    await click_state(page, spec, False)
    return spec


async def prepare(page, kind, live=False):
    if kind == 'changes':
        return await prepare_changes(page)
    part = part_for(kind, live)
    await seed(page, [user(), message(A1, 'assistant', [part], U1, None if live else 9000)], running=live)
    await open_work(page, work_id(A1, part['id']))
    await wait_rest(page)
    return disclosure(kind)


async def trace_click(page, spec):
    await page.locator(spec.button).scroll_into_view_if_needed()
    await page.evaluate('spec=>polishArm(spec)', spec.data())
    await page.locator(spec.button).click()
    return await page.evaluate('()=>polishDone')


def check_final(proof, name, frame, opened):
    proof.check(name + '-logical', frame['logical'] == opened, frame)
    proof.check(name + '-physical', frame['physicalOpen'] == opened and
                (frame['reveal'] > 10 if opened else frame['reveal'] <= 1), frame)


async def settings(page, proof, **_):
    await seed(page, [user()])
    await page.evaluate('''()=>{
      window.polishClipboard=[];
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>polishClipboard.push(text)}});
      S.health={version:'fixture-1.0'};S.errors=['FIXTURE_DIAGNOSTIC'];
    }''')
    await page.locator('#menu').click()
    await page.locator('#drawer [data-a="settings"]').click()
    copy = page.locator('#settingsScreen [data-sa="copydiag"]')
    await expect(copy).to_be_attached()
    card = copy.locator('xpath=..')
    geometry = await card.evaluate('''card=>{
      const rect=e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height}};
      return {card:rect(card),rows:[...card.querySelectorAll(':scope > .settings-row')].map(e=>({
        name:e.dataset.sa||e.innerText,rect:rect(e),minimum:getComputedStyle(e).minHeight}))};
    }''')
    proof.save('diagnostic-card', geometry)
    proof.check('diagnostics-row-inventory', len(geometry['rows']) >= 6, geometry)
    bounds = geometry['card']
    proof.check('card-fits-all-minimum-rows', bounds['height'] >= len(geometry['rows']) * 52 - 1, geometry)
    for row in geometry['rows']:
        r = row['rect']
        # 52 CSS px is the minimum at BOTH font scales; do not invent 52*1.4.
        proof.check('row-min52-' + row['name'], r['height'] >= 51.5, row)
        proof.check('row-inside-card-' + row['name'], r['top'] >= bounds['top'] - .5 and
                    r['bottom'] <= bounds['bottom'] + .5 and r['left'] >= bounds['left'] - .5 and
                    r['right'] <= bounds['right'] + .5, row)
    sections = await page.locator('#settingsScreen').evaluate('''root=>{
      const children=[...root.children].map(e=>{const r=e.getBoundingClientRect();return {name:e.className||e.tagName,top:r.top,bottom:r.bottom,height:r.height}});
      return {client:root.clientHeight,scroll:root.scrollHeight,children};
    }''')
    proof.check('settings-sections-do-not-overlap', all(b['top'] >= a['bottom'] - .5 for a, b in
                zip(sections['children'], sections['children'][1:])), sections)
    await copy.scroll_into_view_if_needed()
    hit = await copy.evaluate('''e=>{
      const r=e.getBoundingClientRect(),x=(r.left+r.right)/2,y=(r.top+r.bottom)/2;
      return {height:r.height,inViewport:r.top>=0&&r.bottom<=innerHeight,
        hit:e.contains(document.elementFromPoint(x,y)),scroll:e.closest('#settingsScreen').scrollTop};
    }''')
    proof.check('copy-scrolled-unclipped-and-hittable', hit['inViewport'] and hit['hit'], hit)
    await page.screenshot(path=str(proof.out / 'copy-diagnostics.png'))
    # No force=True, evaluate(click), or replacement copyText/diagnosticsJSON.
    try:
        await copy.click(timeout=1500)
    except PlaywrightTimeoutError as error:
        proof.check('copy-real-click', False, str(error))
    else:
        proof.check('copy-real-click', True)
    copied = await page.evaluate('polishClipboard')
    expected = await page.evaluate('diagnosticsJSON()')
    proof.check('clipboard-actual-handler-exact-payload', copied == [expected], copied)
    proof.check('settings-no-native-bridge', await page.evaluate('typeof window.CodexNative === "undefined"'))


async def badges(page, proof, **_):
    saved = [user(), message(A1, 'assistant', [part_for('local'), part_for('ssh'),
              tool('edit', 'edit', {'filePath': 'src/login.js'}, metadata={'diff': PATCH, 'source': 'local'})], U1, 9000)]
    await seed(page, saved)
    await open_work(page, work_id(A1, 'local'))
    await wait_rest(page)
    for pid in ('local', 'ssh', 'edit'):
        row = page.locator(BODY + ' ' + activity_segment(A1, pid))
        await row.scroll_into_view_if_needed()
        environments = row.locator('.tool-environment')
        visible = [await el.inner_text() for el in await environments.all() if await el.evaluate(PAINTED)]
        proof.check(pid + '-environment-at-rest', any('SSH' in value for value in visible) if pid == 'ssh'
                    else not any(value.strip() == 'Local' for value in visible), visible)
        marks = row.locator('.tool-status svg')
        proof.check(pid + '-success-check-not-painted', not any([await el.evaluate(PAINTED) for el in await marks.all()]))
        ax = await row.aria_snapshot()
        proof.check(pid + '-completed-accessible', bool(re.search(r'completed|succeeded|заверш|выполн', ax, re.IGNORECASE)), ax)
    proof.check('metadata-and-source-unchanged', await page.evaluate(SOURCE) == saved)


async def visible_states(page, proof, **_):
    parts = [tool('running', 'read', {'filePath': 'running.txt'}, 'LIVE', status='running'),
             tool('error', 'read', {'filePath': 'missing.txt'}, status='error', error='FIXTURE_READ_ERROR'),
             tool('pending', 'read', {'filePath': 'pending.txt'}, status='pending'),
             tool('waiting', 'bash', {'command': 'fixture-command'}, status='running')]
    saved = [user(), message(A1, 'assistant', parts, U1)]
    await seed(page, saved, running=True)
    await event(page, 'permission.asked', id='polish-approval', sessionID=SID, permission='bash',
                patterns=['fixture-command'], tool={'messageID': A1, 'callID': 'call_waiting'})
    await open_work(page, work_id(A1, 'running'))
    await wait_rest(page)
    for state, label in [('running', 'Running'), ('error', 'Failed'), ('pending', 'Pending'), ('waiting', 'Awaiting approval')]:
        row = page.locator(BODY + ' ' + activity_segment(A1, state))
        status = row.locator('.tool-status')
        await status.scroll_into_view_if_needed()
        proof.check(state + '-state', await row.get_attribute('data-state') == state)
        proof.check(state + '-painted', await status.evaluate(PAINTED))
        ax = await row.aria_snapshot()
        proof.check(state + '-accessible', label in ax, ax)
        if state == 'error':
            proof.check('error-message-painted', await row.locator('.tool-error').evaluate(PAINTED))
    proof.check('live-source-unchanged', await page.evaluate(SOURCE) == saved)


async def raw_diff(page, proof, **_):
    parts = [part_for('reason'), part_for('local'), part_for('ssh'),
             tool('edit', 'edit', {'filePath': 'src/login.js'}, metadata={'diff': PATCH}),
             tool('remote-edit', 'mobile_ssh_ssh_write_file', {'connection_id': 'conn_fixture', 'path': '/srv/login.js', 'content': 'fixedLogin\n'},
                  json.dumps({'path': '/srv/login.js', 'patch': PATCH, 'additions': 1, 'deletions': 1}))]
    saved = [user(), message(A1, 'assistant', parts, U1, 9000)]
    await seed(page, saved)
    await open_work(page, work_id(A1, 'r1'))
    await wait_rest(page)
    for kind in ('reason', 'local', 'ssh'):
        spec = disclosure(kind)
        await click_state(page, spec, True)
        raw = await page.locator(spec.row + (' .rtext' if kind == 'reason' else ' .tool-output')).text_content()
        proof.check(kind + '-raw-exact', raw == (RAW if kind == 'reason' else OUTPUT), {'length': len(raw)})
    for pid in ('edit', 'remote-edit'):
        row = page.locator(BODY + ' ' + activity_segment(A1, pid))
        await row.locator('summary').click()
        proof.check(pid + '-addition', await row.locator('.diff-line.a .diff-text').text_content() == 'fixedLogin')
        proof.check(pid + '-deletion', await row.locator('.diff-line.d .diff-text').text_content() == 'oldLogin')
        proof.check(pid + '-path-preserved', ('/srv/login.js' if pid == 'remote-edit' else 'src/login.js') in await row.text_content())
    await close_work(page)
    await open_work(page, work_id(A1, 'r1'))
    proof.check('raw-diff-source-and-metadata-unchanged', await page.evaluate(SOURCE) == saved)


def rgb(value):
    if value.startswith('#'):
        value = value[1:]
        if len(value) == 3:
            value = ''.join(c * 2 for c in value)
        return [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return [float(n) / 255 for n in re.findall(r'[\d.]+', value)[:3]]


def contrast(a, b):
    def luminance(color):
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb(color)]
        return sum(c * w for c, w in zip(linear, (.2126, .7152, .0722)))
    a, b = sorted([luminance(a), luminance(b)])
    return (b + .05) / (a + .05)


async def output_color(page, proof, **_):
    await seed(page, [user(), message(A1, 'assistant', [part_for('reason'), part_for('local')], U1, 9000)])
    await open_work(page, work_id(A1, 'r1'))
    await wait_rest(page)
    for kind, content in [('reason', '.rtext'), ('local', '.tool-output')]:
        await click_state(page, disclosure(kind), True)
        colors = await page.locator(disclosure(kind).row + ' ' + content).evaluate('''e=>{
          let bg=null,opacity=1;
          for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);opacity*=+s.opacity;
            if(!bg&&s.backgroundColor!=='rgba(0, 0, 0, 0)'&&s.backgroundColor!=='transparent')bg=s.backgroundColor}
          return {color:getComputedStyle(e).color,muted:getComputedStyle(e).getPropertyValue('--text-2').trim(),bg,opacity};
        }''')
        ratio, secondary = contrast(colors['color'], colors['bg']), contrast(colors['muted'], colors['bg'])
        proof.check(kind + '-output-stronger-than-muted', ratio > secondary + .1 and colors['opacity'] >= .99,
                    {**colors, 'contrast': ratio, 'mutedContrast': secondary})
        proof.check(kind + '-output-contrast-min45', ratio >= 4.5, ratio)


async def work_smoke(page, proof, **_):
    await seed(page, [user(), message(A1, 'assistant', [part_for('local')], U1, 9000)])
    await page.fill('#inp', 'Unsent polish smoke draft')
    await open_work(page, work_id(A1, 'local'))
    await expect(page.locator(DIALOG)).to_have_accessible_name(re.compile(r'\S'))
    proof.check('work-modal-focus', await page.locator(DIALOG).evaluate('e=>e.contains(document.activeElement)'))
    await close_work(page)
    await expect(page.locator(summary(work_id(A1, 'local')))).to_be_focused()
    proof.check('work-close-restores-trigger', True)
    proof.check('work-draft-preserved', await page.locator('#inp').input_value() == 'Unsent polish smoke draft')


async def motion(page, proof, kind, reduced=False, **_):
    await page.emulate_media(reduced_motion='reduce' if reduced else 'no-preference')
    spec = await prepare(page, kind)
    await click_state(page, spec, False)
    for opened in (True, False):
        name = 'open' if opened else 'close'
        trace = await trace_click(page, spec)
        proof.save(name, trace)
        before, frames = trace['before'], trace['frames']
        final = frames[-1]
        check_final(proof, name, final, opened)
        proof.check(name + '-real-layout-delta', abs(final['height'] - before['height']) > 20, [before, final])
        if reduced:
            # Reduced motion must collapse synchronously: the first post-click
            # frame is already settled and no frame shows a partial reveal.
            proof.check(name + '-first-frame-final', abs(frames[0]['height'] - final['height']) <= 1 and
                        abs(frames[0]['reveal'] - final['reveal']) <= 1 and frames[0]['logical'] == opened, frames[0])
            proof.check(name + '-no-partial-frames', all(abs(f['height'] - final['height']) <= 1 and
                        abs(f['reveal'] - final['reveal']) <= 1 for f in frames), frames)
            continue
        # Animated disclosure contract: ~200ms of real intermediate geometry
        # (no snap, no bounce beyond 2px), settled within ~300ms, trigger focus
        # retained on every frame, and no stuck hold/partial state afterwards.
        for field in ('height', 'reveal'):
            lo, hi = sorted((before[field], final[field]))
            middle = [f[field] for f in frames if lo + 1 < f[field] < hi - 1]
            proof.check(name + '-intermediate-' + field, len({round(v, 1) for v in middle}) >= 2,
                        {'before': before[field], 'final': final[field], 'intermediate': middle})
        growing = final['height'] >= before['height']
        proof.check(name + '-monotonic-progress', all((b['height'] >= a['height'] - 2) if growing
                    else (b['height'] <= a['height'] + 2) for a, b in zip(frames, frames[1:])), frames)
        settled = next((f for f in frames if abs(f['height'] - final['height']) <= 1 and
                        abs(f['reveal'] - final['reveal']) <= 1), None)
        proof.check(name + '-settled-promptly', settled is not None and settled['ms'] <= 300,
                    {'settledAtMs': None if settled is None else settled['ms'], 'final': final})
        proof.check(name + '-trigger-focus-all-frames', all(f['focused'] for f in frames), frames)
        # Stuck-state scan is scoped to the disclosure body: live rows legitimately
        # keep an infinite spinner animation in their summary/head.
        proof.check(name + '-end-state-clean', await page.locator(spec.row).evaluate(
            '''row=>{const content=row.querySelector(':scope>.activity-body,:scope>.change-preview,:scope>.think-body');
               return !row.hasAttribute('data-disclosure-hold')&&!(content&&content.hasAttribute('data-disclosure-target'))
                  &&!(content&&content.getAnimations().some(a=>a.playState==='running'))}'''))
    if kind == 'changes':
        calls = await page.evaluate('polishHistoryCalls')
        proof.check('changes-real-read-handlers', calls.count('historyList') == 1 and calls.count('historyShow') == 1, calls)
        proof.check('changes-no-write-native-actions', await page.evaluate('polishHistoryUnexpected') == [])
        proof.check('changes-ssh-source-retained', await page.locator(spec.row + ' .change-source').inner_text() == 'SSH')
        proof.check('changes-patch-retained', await page.locator(spec.row + ' .diff-line.a').count() == 8 and
                    await page.locator(spec.row + ' .diff-line.d').count() == 8)
    else:
        await click_state(page, spec, True)
        raw = await page.locator(spec.row + (' .rtext' if kind == 'reason' else ' .tool-output')).text_content()
        proof.check('motion-content-raw-intact', raw == (RAW if kind == 'reason' else OUTPUT), {'length': len(raw)})


async def rapid(page, proof, kind, **_):
    spec = await prepare(page, kind)
    await click_state(page, spec, False)
    await page.locator(spec.button).scroll_into_view_if_needed()
    # Raw pointer clicks intentionally bypass Playwright's animation-stability
    # wait, but not hit testing or the app's actual click handler. The bottom-
    # anchored Changes sheet can move the summary during its own expansion.
    for opened in (True, False):
        for _ in range(3):
            box = await page.locator(spec.button).bounding_box()
            assert box and box['height'] > 0
            await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            await page.wait_for_timeout(30)
        await wait_rest(page)
        check_final(proof, 'rapid-' + ('open' if opened else 'close'), await read_disclosure(page, spec), opened)


async def stream_close(page, proof, kind, **_):
    spec = await prepare(page, kind, live=True)
    await click_state(page, spec, True)
    await page.locator(spec.button).scroll_into_view_if_needed()
    await page.evaluate('spec=>polishArm(spec)', spec.data())
    await page.locator(spec.button).click()
    # Inject via the existing SSE helper during the close, without calling a
    # renderer or an animation helper. This also tests the real rAF scheduler.
    injected = await page.evaluate('performance.now()-polishTrace.clickAt')
    if kind == 'reason':
        await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='r1', field='text', delta='\nSTREAM_DELTA')
    else:
        await event(page, 'message.part.updated', part=message(A1, 'assistant', [part_for(kind, True, '\nSTREAM_DELTA')])['parts'][0])
    trace = await page.evaluate('()=>polishDone')
    proof.save('close-with-stream', {'injectedAfterMs': injected, **trace})
    frames = trace['frames']
    proof.check('stream-injected-promptly', injected < 100, injected)
    # The close animation must run to completion despite the mid-close SSE
    # delta: no reopen, no stuck hold, and the closed state stays clean.
    settled = next((f for f in frames if f['reveal'] <= 1 and not f['logical'] and not f['physicalOpen']), None)
    proof.check('stream-close-settled-promptly', settled is not None and settled['ms'] <= 300,
                {'settledAtMs': None if settled is None else settled['ms']})
    check_final(proof, 'stream-close', frames[-1], False)
    proof.check('stream-no-reopen-geometry', all(b['height'] <= a['height'] + 2 for a, b in zip(frames, frames[1:])), frames)
    proof.check('stream-keeps-trigger-focus-all-frames', all(f['focused'] for f in frames), frames)
    # Stuck-state scan is scoped to the disclosure body: a live row legitimately
    # keeps an infinite spinner animation in its summary/head.
    proof.check('stream-close-end-state-clean', await page.locator(spec.row).evaluate(
        '''row=>{const content=row.querySelector(':scope>.activity-body,:scope>.change-preview,:scope>.think-body');
           return !row.hasAttribute('data-disclosure-hold')&&!(content&&content.hasAttribute('data-disclosure-target'))
              &&!(content&&content.getAnimations().some(a=>a.playState==='running'))}'''))
    raw = await page.locator(spec.row + (' .rtext' if kind == 'reason' else ' .tool-output')).text_content()
    proof.check('stream-content-updated-while-closed', raw == (RAW if kind == 'reason' else OUTPUT) + '\nSTREAM_DELTA')
    await close_work(page)
    await open_work(page, work_id(A1, 'r1' if kind == 'reason' else kind))
    await wait_rest(page)
    check_final(proof, 'reopen-work-retains-manual-close', await read_disclosure(page, spec), False)


async def show_more(page, proof, kind, **_):
    spec = await prepare(page, kind, live=True)
    await click_state(page, spec, True)
    button = page.locator(spec.row + ' [data-feed-expand]')
    await expect(button).to_be_visible()
    await button.click()
    proof.check('show-more-manual-expanded', await button.get_attribute('aria-expanded') == 'true')
    before = await page.locator(spec.row + ' .feed-text-window').evaluate('e=>e.clientHeight')
    if kind == 'reason':
        await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='r1', field='text', delta='\nSTREAM_DELTA')
    else:
        await event(page, 'message.part.updated', part=message(A1, 'assistant', [part_for(kind, True, '\nSTREAM_DELTA')])['parts'][0])
    await wait_rest(page)
    proof.check('show-more-persists-stream', await button.get_attribute('aria-expanded') == 'true')
    proof.check('show-more-preserves-focus-stream', await button.evaluate('e=>document.activeElement===e'))
    await click_state(page, spec, False)
    await click_state(page, spec, True)
    proof.check('show-more-persists-toggle', await button.get_attribute('aria-expanded') == 'true')
    await close_work(page)
    await open_work(page, work_id(A1, 'r1' if kind == 'reason' else kind))
    await wait_rest(page)
    proof.check('show-more-persists-work-reopen', await button.get_attribute('aria-expanded') == 'true')
    after = await page.locator(spec.row + ' .feed-text-window').evaluate('e=>({height:e.clientHeight,scroll:e.scrollHeight})')
    proof.check('show-more-not-just-an-aria-flag', after['height'] >= before and after['height'] >= after['scroll'] - 1, after)


CASES = {fn.__name__: (fn, {}) for fn in (settings, badges, visible_states, raw_diff, output_color, work_smoke)}
for _kind in KINDS:
    for _reduced in (False, True):
        CASES[('reduced_' if _reduced else 'motion_') + _kind] = (motion, {'kind': _kind, 'reduced': _reduced})
    CASES['rapid_' + _kind] = (rapid, {'kind': _kind})
    if _kind != 'changes':
        CASES['stream_close_' + _kind] = (stream_close, {'kind': _kind})
        CASES['show_more_' + _kind] = (show_more, {'kind': _kind})


async def main(args):
    out = Path(tempfile.mkdtemp(prefix='work-polish-proof-'))
    source = args.html.read_text(encoding='utf-8')
    # Preserve the exact tested input outside the repository while other agents
    # edit the asset. A later run must take a fresh production snapshot.
    (out / 'asset.html').write_text(source, encoding='utf-8')
    report = {'asset_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(), 'scenarios': [],
              'excluded': ['primary Work dialog motion proof', 'Auto mode', 'WORK 80', 'UI218']}
    selected = [name for name in CASES if not args.case or name in args.case]
    variants = [(t, w, f, h) for t in dict.fromkeys(args.theme or ['light', 'dark'])
                for w in dict.fromkeys(args.width or [320, 412]) for f in dict.fromkeys(args.font or [100, 140])
                for h in dict.fromkeys(args.height or [800, 400])]
    report['planned'] = {'scenarios': len(selected), 'configurations': len(variants), 'runs': len(selected) * len(variants)}
    print(f'Evidence: {out}\nAsset SHA256: {report["asset_sha256"]}', flush=True)
    async with async_playwright() as pw:
        browser = await launch_browser(pw, args.browser_executable)
        try:
            for theme, width, font, height in variants:
                for name in selected:
                    case_out = out / f'{theme}-{width}-{font}-{height}' / name
                    case_out.mkdir(parents=True)
                    proof = Proof(case_out)
                    result = {'case': name, 'theme': theme, 'width': width, 'font': font, 'height': height}
                    context, page, errors, unexpected = await boot_page(browser, theme=theme, source=source)
                    try:
                        await page.set_viewport_size({'width': width, 'height': height})
                        await page.emulate_media(reduced_motion='no-preference')
                        await page.evaluate('scale=>{lset("oc_font_scale",scale);appearance()}', font / 100)
                        await page.evaluate(INSTALL_SAMPLER)
                        await settle(page)
                        fn, kwargs = CASES[name]
                        await fn(page, proof, **kwargs)
                    except Exception:
                        result['error'] = traceback.format_exc()
                    finally:
                        proof.check('no-browser-errors', not errors, errors)
                        proof.check('no-unexpected-network', not unexpected, unexpected)
                        if await page.evaluate('typeof polishHistoryUnexpected !== "undefined"'):
                            proof.check('no-unexpected-fixture-bridge-actions', await page.evaluate('polishHistoryUnexpected') == [])
                        result['checks'] = proof.checks
                        result['status'] = 'ERROR' if 'error' in result else 'FAIL' if any(c['status'] == 'FAIL' for c in proof.checks) else 'PASS'
                        if result['status'] != 'PASS':
                            await page.screenshot(path=str(case_out / 'failure.png'))
                            (case_out / 'failure.html').write_text(await page.content(), encoding='utf-8')
                        (case_out / 'measurements.json').write_text(json.dumps(proof.measurements, indent=2), encoding='utf-8')
                        await context.close()
                    report['scenarios'].append(result)
                    # Incremental evidence survives a bounded external timeout.
                    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                    failed = [c['name'] for c in proof.checks if c['status'] == 'FAIL']
                    print(f'{result["status"]} {theme}/{width}/{font}/{height} {name}' + (': ' + ', '.join(failed) if failed else ''), flush=True)
                    if 'error' in result:
                        print(result['error'], flush=True)
        finally:
            await browser.close()
    scenarios = report['scenarios']
    checks = [check for result in scenarios for check in result['checks']]
    report['totals'] = {'scenarios_executed': len(scenarios),
                        **{f'scenarios_{status.lower()}': sum(r['status'] == status for r in scenarios) for status in ('PASS', 'FAIL', 'ERROR')},
                        'checks_executed': len(checks), 'checks_passed': sum(c['status'] == 'PASS' for c in checks),
                        'checks_failed': sum(c['status'] == 'FAIL' for c in checks)}
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report['totals']) + f'\nReport: {out / "report.json"}', flush=True)
    return int(any(result['status'] != 'PASS' for result in scenarios))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, default=HTML, help='Defaults to actual asset; accepts a captured baseline')
    parser.add_argument('--case', action='append', choices=list(CASES))
    parser.add_argument('--browser-executable')
    parser.add_argument('--theme', action='append', choices=['light', 'dark'])
    parser.add_argument('--width', action='append', type=int, choices=[320, 412])
    parser.add_argument('--font', action='append', type=int, choices=[100, 140])
    parser.add_argument('--height', action='append', type=int, choices=[800, 400])
    raise SystemExit(asyncio.run(main(parser.parse_args())))
