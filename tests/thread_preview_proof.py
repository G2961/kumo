"""Offline mobile proof of the actual thread preview and its Settings integration.

Run: ../.local/api-test-venv/Scripts/python.exe -B tests/thread_preview_proof.py
Only the final main-app boot() call is disabled; renderers and handlers are real.
Both assets are served from memory, all other requests are blocked, and every
browser context is fresh. Screenshots contain only the static preview fixture.
No Android device, native implementation, model, SSH, or real user state is used.
"""
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

# Import helpers without creating an extra file outside this test's write scope.
sys.dont_write_bytecode = True
from reasoning_activity_proof import (  # noqa: E402
    A1, SID, U1, event, message, reason, seed, text, tool, user,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'app/src/main/assets'
OUT = ROOT.parent / '.local/thread-preview-scenarios/integration'
ORIGIN = 'http://thread-preview.test'
VIEWPORTS = [(320, 800), (412, 915), (412, 412)]

# Install before fixture scripts, including in the sandboxed frame. This bridge
# spy deliberately EXISTS in the iframe: Android bridges may be injected into
# every frame, so the safety requirement is no calls, not bridge inaccessibility.
MONITOR = r"""(() => {
 const p=window.__threadProof={network:[],native:[],storage:[],clipboard:[]};
 for(const method of ['setItem','removeItem','clear']){
  const original=Storage.prototype[method];
  Storage.prototype[method]=function(...args){
   p.storage.push({method,key:args[0]||null});return original.apply(this,args);
  };
 }
 const blocked=name=>function(){p.network.push(name);throw new Error('Offline proof blocked '+name)};
 window.fetch=blocked('fetch');window.XMLHttpRequest=blocked('XMLHttpRequest');
 window.WebSocket=blocked('WebSocket');window.EventSource=blocked('EventSource');
 window.Worker=blocked('Worker');window.SharedWorker=blocked('SharedWorker');
 navigator.sendBeacon=blocked('sendBeacon');
 if(window!==window.top){
  window.CodexNative=new Proxy({}, {get:(_,method)=>(...args)=>{
   p.native.push(String(method));throw new Error('Fixture called native bridge');
  }});
 }
 // Clipboard writes must not touch the developer's OS clipboard. Exercise the
 // actual copy handler/fallback while retaining only the synthetic copied text.
 const copy=document.execCommand.bind(document);
 document.execCommand=(command,...args)=>{
  if(command==='copy'){
   p.clipboard.push(document.activeElement?.value||window.getSelection()?.toString()||'');
   return true;
  }
  return copy(command,...args);
 };
 if(navigator.clipboard)Object.defineProperty(navigator.clipboard,'writeText',{
  configurable:true,value:async value=>{p.clipboard.push(value)}
 });
})()"""

PARENT_SNAPSHOT = r"""() => {
 const storage=s=>Object.fromEntries(Object.keys(s).sort().map(k=>[k,s.getItem(k)]));
 return {
  messages:[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]})),
  rendered:document.querySelector('#main').innerHTML,
  draft:document.querySelector('#inp').value,
  local:storage(localStorage),session:storage(sessionStorage),cookie:document.cookie,
  theme:document.documentElement.dataset.theme,
  fontScale:document.documentElement.style.getPropertyValue('--font-scale'),
  sid:S.sid,dir:S.dir,model:S.model,agent:S.agent,connection:S.connectionID,
  connections:S.connections,policies:S.localPolicies,efforts:S.efforts,
  remoteRuns:[...S.remoteRuns],attachments:S.attachments,reactions:S.reactions,
  running:S.running,status:S.status,revision:S.revision,
  sameMessages:S.msgs===window.__originalMessages,
  sameRenderer:renderMsgs===window.__originalRenderer&&onEvent===window.__originalOnEvent
 };
}"""

METRICS = r"""() => {
 const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};
 const q=s=>document.querySelector(s);
 const feed=q('#feed');
 return {
  composer:Object.fromEntries(['.composer-footer','.composer','#draft','#demoSend'].map(s=>[s,rect(q(s))])),
  fonts:[...document.querySelectorAll('h1,.user-message,.event-label,.thought-text,.command-line,.command-output,.environment,#answerText,#draft,.model,.option')].map(e=>{
   const c=getComputedStyle(e);return [c.fontFamily,c.fontSize,c.lineHeight,c.fontWeight];
  }),
  feed:{...rect(feed),clientHeight:feed.clientHeight,scrollHeight:feed.scrollHeight,scrollTop:feed.scrollTop},
  viewport:{width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,
   scrollHeight:document.documentElement.scrollHeight,scrollX,scrollY},
  bodyFont:getComputedStyle(document.body).fontSize
 };
}"""


class Proof:
    def __init__(self, name, directory):
        self.name, self.directory = name, directory
        self.checks, self.screenshots, self.metrics = [], [], {}
        self.targets = {}

    def check(self, name, ok, detail=None):
        result = {'name': name, 'ok': bool(ok)}
        if detail is not None:
            result['detail'] = detail
        self.checks.append(result)
        if not ok:
            print(f'FAIL {self.name}/{name}: {detail}', flush=True)

    async def step(self, name, operation, frame=None):
        try:
            await operation()
        except Exception as error:
            self.check(name, False, f'{type(error).__name__}: {error}')
            if frame is not None and not frame.is_detached():
                await self.shot(frame, name + '-failure')

    async def shot(self, frame, name):
        # Never screenshot the parent page, even when an integration check fails.
        await expect(frame.locator('html')).to_have_attribute('data-preview-ready', 'true')
        path = self.directory / (name + '.png')
        await frame.locator('#preview').screenshot(path=str(path), animations='disabled')
        self.screenshots.append(str(path.relative_to(ROOT.parent)))

    async def targets_at(self, frame, stage):
        values = await frame.evaluate(r"""() => [...document.querySelectorAll(
          'button,summary,textarea,input,select,a[href],[role="button"]')]
          .filter(e=>e.checkVisibility({checkVisibilityCSS:true}))
          .map(e=>({key:e.id||e.getAttribute('aria-label')||e.getAttribute('data-demo')||e.textContent.trim(),
           tag:e.tagName,width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}))""")
        for value in values:
            key = value['tag'] + ':' + value['key']
            if key not in self.targets:
                self.targets[key] = {**value, 'stage': stage}
            elif value['height'] < self.targets[key]['height'] or value['width'] < self.targets[key]['width']:
                self.targets[key] = {**value, 'stage': stage,
                                     'height': min(value['height'], self.targets[key]['height']),
                                     'width': min(value['width'], self.targets[key]['width'])}

    def report(self):
        return {'name': self.name, 'checks': self.checks, 'metrics': self.metrics,
                'controls': list(self.targets.values()), 'screenshots': self.screenshots,
                'ok': all(c['ok'] for c in self.checks)}


def rects_equal(left, right):
    return left.keys() == right.keys() and all(
        abs(left[key][axis] - right[key][axis]) <= 1
        for key in left for axis in ('x', 'y', 'width', 'height'))


async def settle(frame):
    await frame.evaluate('() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')


async def open_preview(page, theme):
    await page.locator('#settingsScreen [data-sa="threadpreview"]').click()
    await expect(page.locator('#threadPreview')).to_have_count(1)
    frame = await page.locator('#threadPreview').element_handle()
    frame = await frame.content_frame()
    await expect(frame.locator('html')).to_have_attribute('data-preview-ready', 'true')
    await expect(frame.locator('html')).to_have_attribute('data-theme', theme)
    return frame


async def collapse(frame):
    await frame.locator('#previewSettings').click()
    await frame.locator('#collapseAll').click()
    await settle(frame)


async def carets(proof, frame, stage):
    values = await frame.locator('.event').evaluate_all(r"""events=>events.map(e=>{
     const s=e.querySelector(':scope > summary'),c=s.querySelector('.caret');
     const r=c.getBoundingClientRect(),sr=s.getBoundingClientRect(),css=getComputedStyle(c);
     const label=s.querySelector('.event-label');
     return {id:e.id,open:e.open,count:s.querySelectorAll('.caret').length,
      last:s.lastElementChild===c,href:c.querySelector('use')?.getAttribute('href'),
      width:r.width,height:r.height,rightInset:sr.right-r.right,
      paddingRight:parseFloat(getComputedStyle(s).paddingRight),transform:css.transform,
      labelBefore:label.getBoundingClientRect().right<=r.left,
      nativeMarker:getComputedStyle(s).listStyleType};
    })""")
    ok = all(v['count'] == 1 and v['last'] and v['href'] == '#chevron'
             and v['width'] == 16 and v['height'] == 16 and v['labelBefore']
             and abs(v['rightInset'] - v['paddingRight']) <= 1
             and v['nativeMarker'] == 'none' for v in values)
    for opened in (True, False):
        transforms = {v['transform'] for v in values if v['open'] == opened}
        ok = ok and len(transforms) <= 1
    closed = [v['transform'] for v in values if not v['open']]
    opened = [v['transform'] for v in values if v['open']]
    ok = ok and all(v == 'none' for v in closed) and all(v != 'none' for v in opened)
    proof.check('consistent-right-carets-' + stage, ok, values)


async def initial_checks(proof, page, frame, theme, baseline):
    await proof.shot(frame, 'collapsed')
    await proof.targets_at(frame, 'collapsed')
    proof.metrics['collapsed'] = await frame.evaluate(METRICS)
    proof.check('all-thoughts-initially-collapsed',
                await frame.locator('.thought').count() == 2
                and await frame.locator('.thought[open]').count() == 0)
    proof.check('all-commands-initially-collapsed',
                await frame.locator('.command').count() == 2
                and await frame.locator('.command[open]').count() == 0)
    proof.check('chronological-flat-events', await frame.locator('.event').evaluate_all(
        "es=>es.map(e=>e.id)") == ['thought-local', 'command-local', 'thought-remote', 'command-remote'])
    commands = await frame.locator('.command-line').all_text_contents()
    proof.check('no-duplicate-command', len(commands) == len(set(commands)) == 2
                and await frame.locator('.thought .command,.thought .command-line').count() == 0,
                commands)
    environments = await frame.locator('.command .environment').all_text_contents()
    for environment in await frame.locator('.command .environment').all():
        await environment.scroll_into_view_if_needed()
        await expect(environment).to_be_in_viewport()
    proof.check('local-and-remote-environments-visible', environments == ['Local', 'SSH · aeza'], environments)
    indicators = await frame.locator('use[href="#check"]').evaluate_all(
        "es=>es.map(e=>!!e.closest('.command-status')&&!!e.closest('.command'))")
    proof.check('success-indicators-only-on-commands', indicators == [True, True]
                and await frame.locator('.command-status[aria-label="Выполнено"]').count() == 2)
    await carets(proof, frame, 'collapsed')
    proof.check('sandbox-exactly-allow-scripts',
                await page.locator('#threadPreview').get_attribute('sandbox') == 'allow-scripts')
    parsed = urlsplit(frame.url)
    proof.check('dedicated-fixture-url-with-current-theme',
                parsed.path == '/_ui/thread-preview.html' and parse_qs(parsed.query) == {'theme': [theme]})
    isolation = await frame.evaluate(r"""() => {
     const blocked=f=>{try{f();return false}catch(e){return e.name==='SecurityError'}};
     return {parentDocument:blocked(()=>parent.document.body),
      parentLocal:blocked(()=>parent.localStorage.getItem('proof-sentinel')),
      parentSession:blocked(()=>parent.sessionStorage.getItem('proof-sentinel')),
      ownLocal:blocked(()=>localStorage.getItem('proof-sentinel')),
      ownSession:blocked(()=>sessionStorage.getItem('proof-sentinel'))};
    }""")
    proof.check('sandbox-denies-parent-document-and-storage', all(isolation.values()), isolation)
    proof.check('parent-inert-and-state-preserved-while-open',
                await page.locator('#app').evaluate('e=>e.inert')
                and await page.evaluate(PARENT_SNAPSHOT) == baseline)
    # Exercise idempotence without replacing the real Settings entry path above.
    handle = await page.locator('#threadPreview').element_handle()
    await page.evaluate('openDesignPreview();openDesignPreview()')
    proof.check('duplicate-open-reuses-original-frame',
                await page.locator('#threadPreview').count() == 1
                and await handle.evaluate('e=>e===document.querySelector("#threadPreview")'))
    await page.evaluate("window.postMessage({type:'thread-preview-close'},'*')")
    await frame.evaluate("parent.postMessage({type:'unrelated-preview-message'},'*')")
    await settle(page)
    proof.check('unrelated-source-and-message-do-not-close',
                await page.locator('#threadPreview').count() == 1
                and await page.locator('#app').evaluate('e=>e.inert'))


async def layout_checks(proof, page, frame):
    await collapse(frame)
    before = await frame.evaluate(METRICS)
    await frame.locator('#thought-local > summary').click()
    await frame.locator('#command-local > summary').click()
    await settle(frame)
    await carets(proof, frame, 'mixed')
    await frame.evaluate("document.querySelector('#feed').scrollTop=0")
    await proof.shot(frame, 'expanded')
    for selector in ('#thought-remote > summary', '#command-remote > summary'):
        await frame.locator(selector).click()
    await settle(frame)
    after = await frame.evaluate(METRICS)
    proof.metrics['expanded'] = after
    proof.check('constant-fonts-on-expansion', before['fonts'] == after['fonts'])
    proof.check('constant-composer-rect-on-expansion', rects_equal(before['composer'], after['composer']),
                {'before': before['composer'], 'after': after['composer']})
    proof.check('feed-scrolls-instead-of-shrinking',
                abs(before['feed']['height'] - after['feed']['height']) <= 1
                and after['feed']['scrollHeight'] > before['feed']['scrollHeight']
                and after['feed']['scrollHeight'] > after['feed']['clientHeight'],
                {'before': before['feed'], 'after': after['feed']})
    viewport = after['viewport']
    proof.check('no-document-overflow-and-composer-on-screen',
                viewport['scrollWidth'] <= viewport['width'] + 1
                and viewport['scrollHeight'] <= viewport['height'] + 1
                and after['composer']['.composer-footer']['y'] >= 0
                and after['composer']['.composer-footer']['y'] + after['composer']['.composer-footer']['height'] <= viewport['height'] + 1,
                viewport)
    await carets(proof, frame, 'expanded')
    await proof.targets_at(frame, 'all-expanded')
    output = frame.locator('#local-output')
    small = await output.evaluate('e=>e.getBoundingClientRect().height')
    toggle = frame.locator('.output-toggle')
    await toggle.click()
    await expect(toggle).to_have_attribute('aria-expanded', 'true')
    await expect(toggle).to_contain_text('Свернуть вывод')
    large = await output.evaluate('e=>e.getBoundingClientRect().height')
    output_metrics = await frame.evaluate(METRICS)
    proof.check('output-expands-without-font-or-composer-shrink',
                large > small and before['fonts'] == output_metrics['fonts']
                and rects_equal(before['composer'], output_metrics['composer']),
                {'collapsedHeight': small, 'expandedHeight': large})
    await frame.evaluate("document.querySelector('#feed').scrollTop=0")
    # Real wheel input verifies that the feed, not the whole page, is scrollable.
    box = await frame.locator('#feed').bounding_box()
    await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    await page.mouse.wheel(0, 1000)
    # Playwright's wait_for_function uses eval inside its utility world, which
    # this fixture's CSP correctly forbids. Poll with direct evaluation instead.
    for _ in range(60):
        if await frame.locator('#feed').evaluate('e=>e.scrollTop>0'):
            break
        await settle(frame)
    else:
        raise AssertionError('Wheel input did not scroll the preview feed')
    scrolled = await frame.evaluate(METRICS)
    proof.check('scroll-keeps-composer-fixed', rects_equal(before['composer'], scrolled['composer'])
                and scrolled['viewport']['scrollY'] == 0, scrolled['feed'])
    await proof.shot(frame, 'expanded-scrolled')
    await toggle.click()
    await expect(toggle).to_have_attribute('aria-expanded', 'false')
    await expect(toggle).to_contain_text('Показать ещё')
    proof.check('output-collapses-again', abs(await output.evaluate('e=>e.getBoundingClientRect().height') - small) <= 1)
    for selector in ('.thought', '.command'):
        for summary in await frame.locator(selector + ' > summary').all():
            await summary.click()
    proof.check('thoughts-and-commands-toggle-closed', await frame.locator('.event[open]').count() == 0)


async def control_checks(proof, page, frame, theme, baseline):
    await collapse(frame)
    initial_feed = await frame.locator('.thread').inner_html()
    await frame.locator('#copyAnswer').click()
    await expect(frame.locator('#notice')).to_have_text('Текст примера скопирован')
    await frame.locator('#answerMore').click()
    await expect(frame.locator('#answerMenu')).to_be_visible()
    await proof.targets_at(frame, 'answer-menu')
    await proof.shot(frame, 'answer-menu')
    await frame.locator('#copyFromMenu').click()
    await expect(frame.locator('#answerMenu')).not_to_be_visible()
    copied = await frame.evaluate('window.__threadProof.clipboard')
    answer = await frame.locator('#answerText').text_content()
    proof.check('copy-button-and-menu-copy-synthetic-answer', copied == [answer, answer], copied)
    await frame.locator('#answerMore').click()
    await frame.locator('#answerMenu [data-close-dialog]').click()
    await expect(frame.locator('#answerMenu')).not_to_be_visible()
    await frame.locator('#previewSettings').click()
    await expect(frame.locator('#settings')).to_be_visible()
    await proof.targets_at(frame, 'settings')
    await proof.shot(frame, 'settings')
    opposite = 'dark' if theme == 'light' else 'light'
    original_background = await frame.locator('body').evaluate('e=>getComputedStyle(e).backgroundColor')
    await frame.locator(f'button[data-theme="{opposite}"]').click()
    await expect(frame.locator('html')).to_have_attribute('data-theme', opposite)
    await expect(frame.locator(f'button[data-theme="{opposite}"]')).to_have_attribute('aria-pressed', 'true')
    proof.check('theme-switch-changes-fixture-only',
                await frame.locator('body').evaluate('e=>getComputedStyle(e).backgroundColor') != original_background
                and await page.evaluate(PARENT_SNAPSHOT) == baseline)
    await frame.locator(f'button[data-theme="{theme}"]').click()
    await expect(frame.locator(f'button[data-theme="{opposite}"]')).to_have_attribute('aria-pressed', 'false')
    await frame.locator('#expandExample').click()
    proof.check('settings-expand-example', await frame.locator('.event[open]').evaluate_all(
        'es=>es.map(e=>e.id)') == ['thought-local', 'command-local'])
    await collapse(frame)
    proof.check('settings-collapse-all', await frame.locator('.event[open]').count() == 0
                and await frame.locator('.output-toggle').get_attribute('aria-expanded') == 'false')
    await frame.locator('#previewSettings').click()
    await frame.locator('#settings [data-close-dialog]').click()
    await expect(frame.locator('#settings')).not_to_be_visible()
    for button in await frame.locator('[data-demo]').all():
        expected = await button.get_attribute('data-demo')
        await button.click()
        await expect(frame.locator('#notice')).to_have_text(expected)
    proof.check('all-demo-option-buttons-explain-no-op', True)
    draft = 'Synthetic demo draft: never send this to any model.'
    await frame.locator('#draft').fill(draft)
    await proof.targets_at(frame, 'draft-filled')
    before_send = await frame.evaluate('window.__threadProof.network.length')
    await frame.locator('#demoSend').click()
    await expect(frame.locator('#notice')).to_have_text('Это макет — сообщение не отправлено.')
    proof.check('demo-send-no-network-or-thread-mutation',
                await frame.locator('#draft').input_value() == draft
                and await frame.locator('.thread').inner_html() == initial_feed
                and await frame.evaluate('window.__threadProof.network.length') == before_send == 0
                and await page.evaluate(PARENT_SNAPSHOT) == baseline)
    await proof.shot(frame, 'demo-send')


async def close_checks(proof, page, frame, theme, baseline):
    if await frame.locator('dialog[open]').count():
        await frame.locator('dialog[open] [data-close-dialog]').click()
    probe = await frame.evaluate('window.__threadProof')
    proof.check('fixture-never-calls-network-native-or-storage',
                not probe['network'] and not probe['native'] and not probe['storage'], probe)
    await frame.locator('#exit').click()
    await expect(page.locator('#threadPreview')).to_have_count(0)
    proof.check('exit-removes-iframe-restores-app-and-state',
                not await page.locator('#app').evaluate('e=>e.inert')
                and await page.evaluate(PARENT_SNAPSHOT) == baseline
                and await page.evaluate('document.activeElement?.dataset.sa') == 'threadpreview')
    # Both alternate exits use the real Settings diagnostics button on reopening.
    for mode in ('settings-exit', 'escape'):
        fresh = await open_preview(page, theme)
        proof.check(mode + '-opens-fresh-nonpersistent-fixture',
                    await fresh.locator('#draft').input_value() == ''
                    and await fresh.locator('.event[open]').count() == 0)
        if mode == 'settings-exit':
            await fresh.locator('#previewSettings').click()
            await fresh.locator('#exitFromSettings').click()
        else:
            await fresh.locator('#draft').focus()
            await page.keyboard.press('Escape')
        await expect(page.locator('#threadPreview')).to_have_count(0)
        proof.check(mode + '-restores-parent',
                    not await page.locator('#app').evaluate('e=>e.inert')
                    and await page.evaluate(PARENT_SNAPSHOT) == baseline)
    await page.locator('#settingsScreen [data-sa="back"]').click()
    await expect(page.locator('#settingsScreen')).not_to_be_visible()
    await expect(page.locator('#inp')).to_have_value(baseline['draft'])
    proof.check('live-renderer-references-and-content-intact', await page.evaluate(PARENT_SNAPSHOT) == baseline)
    # Real event reducer + RAF renderer must still accept a live delta, with the
    # preceding reasoning/tool chronology preserved. No mocked render function.
    await event(page, 'message.part.delta', sessionID=SID, messageID=A1,
                partID='answer', field='text', delta=' Stream still works after preview.')
    await expect(page.locator('#main')).to_contain_text('Stream still works after preview.')
    proof.check('live-renderer-handles-delta-after-close',
                await page.locator('.think').count() == 1
                and await page.locator('.activity').count() == 1
                and await page.evaluate('renderMsgs===window.__originalRenderer&&onEvent===window.__originalOnEvent'))
    after = await page.evaluate(PARENT_SNAPSHOT)
    proof.check('stream-keeps-draft-and-persistent-storage', all(
        after[key] == baseline[key] for key in ('local', 'session', 'cookie', 'draft', 'theme', 'fontScale')))


def source_checks(main_source, preview_source):
    proof = Proof('source', OUT)
    match = re.search(r'function openDesignPreview\(\)\{(.*?)\n\}', main_source, re.S)
    integration = match.group(1) if match else ''
    proof.check('close-message-checks-frame-source-before-close', bool(re.search(
        r'if\s*\(\s*e\.source\s*===\s*frame\.contentWindow\s*&&\s*e\.data\?\.type\s*===\s*[\'"]thread-preview-close[\'"]\s*\)\s*close\(\)', integration)))
    scripts = '\n'.join(re.findall(r'<script\b[^>]*>(.*?)</script>', preview_source, re.S | re.I))
    forbidden = re.findall(r'\b(?:CodexNative|nativeCall|codexNativeResult|fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|indexedDB|caches|serviceWorker|eval)\b|document\.cookie', scripts)
    proof.check('fixture-js-has-no-native-network-or-persistence-api', bool(scripts) and not forbidden, forbidden)
    proof.check('fixture-csp-blocks-connect-and-forms',
                "default-src 'none'" in preview_source and "connect-src 'none'" in preview_source
                and "form-action 'none'" in preview_source)
    return proof.report()


async def run_case(browser, main_source, preview_source, theme, width, height, scenario_checks=None):
    name = f'{theme}-{width}x{height}'
    directory = OUT / name
    directory.mkdir(parents=True, exist_ok=True)
    proof = Proof(name, directory)
    context = await browser.new_context(viewport={'width': width, 'height': height},
                                        device_scale_factor=1, is_mobile=True, has_touch=True,
                                        color_scheme=theme, reduced_motion='reduce',
                                        service_workers='block', locale='ru-RU')
    requests, errors, visits, monitors = [], [], [], []
    page = await context.new_page()
    page.set_default_timeout(4000)
    page.on('pageerror', lambda error: errors.append(str(error)))

    async def route_request(route):
        req = route.request
        parsed = urlsplit(req.url)
        if req.method == 'GET' and parsed.scheme + '://' + parsed.netloc == ORIGIN:
            if parsed.path == '/_ui/index.html':
                await route.fulfill(body=main_source, content_type='text/html; charset=utf-8')
                return
            if parsed.path == '/_ui/thread-preview.html':
                visits.append(req.url)
                await route.fulfill(body=preview_source, content_type='text/html; charset=utf-8')
                return
        requests.append(f'{req.method} {req.url}')
        await route.abort()

    await context.route('**/*', route_request)
    # Initialize ONLY the top-level fresh context, never touch opaque-frame storage.
    await context.add_init_script('if(window===window.top){' +
        f'localStorage.setItem("oc_theme",JSON.stringify({json.dumps(theme)}));' +
        'localStorage.setItem("oc_lp_hint","true");' +
        'localStorage.setItem("oc_font_scale","1.2");' +
        'localStorage.setItem("proof-sentinel","synthetic-preferences");' +
        'sessionStorage.setItem("proof-sentinel","synthetic-session");}' + MONITOR)
    frame = None
    try:
        # Installed Chrome can cold-start beyond the 4s UI action budget. Keep
        # this allowance local to initial navigation, not simulation/assertions.
        await page.goto(ORIGIN + '/_ui/index.html', timeout=15000)
        await page.wait_for_function('typeof renderMsgs==="function"&&typeof onEvent==="function"')
        await seed(page, [user(), message(A1, 'assistant', [
            reason('reason', 'Synthetic parent reasoning, not user data.'),
            tool('tool', 'bash', {'command': 'printf synthetic'}, 'synthetic output'),
            text('answer', 'Synthetic parent answer.')], U1, completed=8000)])
        await page.locator('#inp').fill('Synthetic parent draft must survive preview.')
        await page.evaluate("""() => {
         window.__originalMessages=S.msgs;window.__originalRenderer=renderMsgs;window.__originalOnEvent=onEvent;
         window.CodexNative={request:()=>{window.__threadProof.native.push('request');throw new Error('Unexpected native call')}};
        }""")
        baseline = await page.evaluate(PARENT_SNAPSHOT)
        cookie_baseline = await context.cookies()
        await page.locator('#menu').click()
        await page.locator('#drawer [data-a="settings"]').click()
        await expect(page.locator('#settingsScreen')).to_be_visible()
        proof.check('actual-main-settings-entry-preserves-state', await page.evaluate(PARENT_SNAPSHOT) == baseline)
        frame = await open_preview(page, theme)
        await proof.step('initial', lambda: initial_checks(proof, page, frame, theme, baseline), frame)
        await proof.step('layout', lambda: layout_checks(proof, page, frame), frame)
        await proof.step('controls', lambda: control_checks(proof, page, frame, theme, baseline), frame)
        if scenario_checks is not None:
            frame = await scenario_checks(proof, page, frame, theme, baseline)
        monitors.append(await frame.evaluate('window.__threadProof'))
        await proof.step('close-and-live', lambda: close_checks(proof, page, frame, theme, baseline), frame)
        proof.check('all-fixture-controls-at-least-44px-high', bool(proof.targets) and all(
            v['height'] >= 44 for v in proof.targets.values()),
            [v for v in proof.targets.values() if v['height'] < 44])
        proof.check('cookies-unchanged', await context.cookies() == cookie_baseline)
        proof.check('parent-has-no-network-native-or-storage-writes', await page.evaluate(
            'window.__threadProof.network.length===0&&window.__threadProof.native.length===0&&window.__threadProof.storage.length===0'),
            await page.evaluate('window.__threadProof'))
    except Exception as error:
        proof.check('setup-or-unexpected-error', False, f'{type(error).__name__}: {error}')
        if frame is not None and not frame.is_detached():
            await proof.shot(frame, 'failure')
    finally:
        proof.check('no-javascript-errors', not errors, errors)
        proof.check('no-unexpected-http-requests', not requests, requests)
        proof.metrics['fixtureLoads'] = visits
        proof.metrics['fixtureMonitors'] = monitors
        await context.close()
    passed = sum(c['ok'] for c in proof.checks)
    print(f'{name}: {passed}/{len(proof.checks)} checks passed; screenshots: {directory}', flush=True)
    return proof.report()


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    main_source = (ASSETS / 'codex.html').read_text(encoding='utf-8')
    preview_source = (ASSETS / 'thread-preview.html').read_text(encoding='utf-8')
    results = [source_checks(main_source, preview_source)]
    hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in
              [('codex.html', main_source), ('thread-preview.html', preview_source)]}
    main_source, count = re.subn(r'S\.bootP\s*=\s*boot\(\);(?=\s*</script>\s*</body>)',
                                 'S.bootP=Promise.resolve();', main_source)
    assert count == 1, 'Expected exactly one final boot() invocation; no other app code is replaced'
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for theme in ('light', 'dark'):
                for width, height in VIEWPORTS:
                    results.append(await run_case(browser, main_source, preview_source, theme, width, height))
        finally:
            await browser.close()
    report = {'assetSha256': hashes, 'viewports': VIEWPORTS,
              'notes': ['Fresh offline mobile Chromium contexts; screenshots are fixture-only.',
                        'Only final boot disabled; production Settings, renderer, event reducer and preview scripts retained.',
                        'Clipboard intercepted to avoid changing OS clipboard; no Android bridge accessibility assumption.',
                        '412x412 models a short keyboard-like viewport, not an Android IME/device test.'],
              'results': results}
    path = OUT / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    checks = [check for result in results for check in result['checks']]
    failures = [check for check in checks if not check['ok']]
    print(f'{len(checks)-len(failures)}/{len(checks)} checks passed; report: {path}', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
