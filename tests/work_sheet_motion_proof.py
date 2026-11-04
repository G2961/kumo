"""Real-frame Work sheet motion/lifecycle proof; no model, build or installation.

Uses the actual asset and existing offline Playwright fixtures. Samples computed
transforms/opacity/backdrop on animation frames, not just CSS declarations.
Run with ../.local/api-test-venv/Scripts/python.exe tests/work_sheet_motion_proof.py
  --browser-executable "C:/Program Files/Google/Chrome/Application/chrome.exe"
Evidence (including all sampled frames) is written to a temporary directory.
"""
import argparse
import asyncio
import hashlib
import json
import tempfile
import traceback
from pathlib import Path

from playwright.async_api import async_playwright, expect

from reasoning_activity_proof import (
    A1, HTML, SID, U1, boot_page, launch_browser, message, reason, seed, text, tool, user,
)
from work_log_proof import BODY, DIALOG, summary, work_id

WID = work_id(A1, 'r1')
OTHER = work_id(A1, 'r2')
FRAME_HELPERS = '''()=>{
  window.motionFrame=()=>{
    const d=document.querySelector('#workLogDialog'),s=getComputedStyle(d);
    const transform=s.transform==='none'?'matrix(1,0,0,1,0,0)':s.transform;
    return {time:performance.now(),open:d.open,modal:d.matches(':modal'),
      y:new DOMMatrixReadOnly(transform).m42,opacity:Number(s.opacity),height:d.offsetHeight,
      backdrop:Number(getComputedStyle(d,'::backdrop').opacity),closing:workLogState.closing,
      running:d.getAnimations({subtree:true}).filter(a=>a.playState==='running').length,
      focusInside:d.contains(document.activeElement),main:document.querySelector('#main').scrollTop};
  };
  window.motionSample=async action=>{
    const frames=[];action();const start=performance.now();
    do {frames.push(motionFrame());await new Promise(requestAnimationFrame)} while(performance.now()-start<340);
    frames.push(motionFrame());return frames;
  };
}'''


async def fixture(page):
    prose = '\n\n'.join(f'Context {i}. ' + 'Preserve the reader and unsent draft. ' * 9 for i in range(18))
    parts = [text('before', prose), reason('r1', 'Inspect work details.')]
    parts += [tool('t' + str(i), 'read', {'filePath': f'source-{i}.txt'}, 'Original output.') for i in range(24)]
    parts += [text('boundary', 'A public boundary.'), reason('r2', 'Another work block.'), text('after', prose)]
    await seed(page, [user(), message(A1, 'assistant', parts, U1, 9000)])
    await page.fill('#inp', 'Unsent motion fixture draft')
    await page.locator(summary(WID)).scroll_into_view_if_needed()
    await page.locator(summary(WID)).focus()
    await page.evaluate(FRAME_HELPERS)


async def ready(page, wid=WID):
    await page.evaluate('id=>openWorkSheet(id)', wid)
    await page.wait_for_function('isWorkSheetOpen()&&!workLogState.motion&&!workLogState.closing')


def assert_motion(frames, closing):
    active = [f for f in frames if f['open']]
    middle = [f for f in active if 1 < f['y'] < f['height'] - 1 and .6 < f['opacity'] < 1]
    assert len(middle) >= 2, f'No actual intermediate {"closing" if closing else "opening"} frames: {frames}'
    assert all(f['modal'] and f['focusInside'] for f in active), 'Top layer/focus released before animation finished'
    assert any(0 < f['backdrop'] < 1 for f in active), f'Backdrop snapped: {frames}'
    ys = [f['y'] for f in active]
    assert all((b >= a - 1 if closing else b <= a + 1) for a, b in zip(ys, ys[1:])), f'Bounce/restart: {ys}'
    assert len({round(f['y'], 1) for f in middle}) >= 2, 'Intermediate transform was frozen'
    if closing:
        assert not frames[-1]['open'] and not frames[-1]['modal']
        assert not frames[-1]['running']
    else:
        assert frames[-1]['open'] and abs(frames[-1]['y']) < 1 and frames[-1]['opacity'] == 1


async def opening_and_closing(page, evidence):
    scroll = await page.locator('#main').evaluate('e=>e.scrollTop')
    frames = await page.evaluate('id=>motionSample(()=>openWorkSheet(id))', WID)
    evidence['opening'] = frames
    assert_motion(frames, False)
    assert all(abs(f['main'] - scroll) <= 2 for f in frames), 'Open scrolled the main feed'
    timing = await page.evaluate('''()=>{
      requestCloseWorkSheet('motion-result');
      return workLogState.motion.effect.getTiming();
    }''')
    assert 180 <= timing['duration'] <= 240, timing
    await page.wait_for_function('!isWorkSheetOpen()')
    await ready(page)
    frames = await page.evaluate('()=>motionSample(()=>requestCloseWorkSheet("sampled-close"))')
    evidence['closing'] = frames
    assert_motion(frames, True)
    assert all(abs(f['main'] - scroll) <= 2 for f in frames), 'Close scrolled the main feed'
    await expect(page.locator(summary(WID))).to_be_focused()
    assert await page.locator(DIALOG).evaluate('e=>e.returnValue') == 'sampled-close'
    await expect(page.locator('#inp')).to_have_value('Unsent motion fixture draft')


async def user_close_paths(page, evidence):
    for mode in ['button', 'escape', 'backdrop', 'native']:
        await ready(page)
        await page.evaluate('''()=>{
          window.userMotion=[];window.userMotionUntil=performance.now()+500;
          const tick=()=>{userMotion.push(motionFrame());if(performance.now()<userMotionUntil)requestAnimationFrame(tick)};
          requestAnimationFrame(tick);
        }''')
        if mode == 'button':
            await page.locator(DIALOG + ' [data-work-close]').click()
        elif mode == 'escape':
            await page.keyboard.press('Escape')
        elif mode == 'backdrop':
            await page.mouse.click(2, 2)
        else:
            await page.locator(DIALOG).evaluate('e=>e.close("native-result")')
        await expect(page.locator(DIALOG)).not_to_be_visible()
        frames = await page.evaluate('window.userMotion')
        assert len([f for f in frames if f['closing'] and f['open'] and 1 < f['y'] < f['height'] - 1]) >= 2, mode
        evidence[mode] = frames
        await expect(page.locator(summary(WID))).to_be_focused()
    assert await page.locator(DIALOG).evaluate('e=>e.returnValue') == 'native-result'


async def cancellation_and_replacements(page, evidence):
    result = await page.evaluate('''async ({id,other,sid})=>{
      const check=(ok,message)=>{if(!ok)throw Error(message)};
      openWorkSheet(id);requestCloseWorkSheet();openWorkSheet(id);
      await new Promise(r=>setTimeout(r,300));
      check(isWorkSheetOpen()&&!workLogState.closing,'Rapid opening/closing/reopen was closed late');
      requestCloseWorkSheet();await new Promise(r=>setTimeout(r,65));
      const before=motionFrame();openWorkSheet(id);const after=motionFrame();
      check(Math.abs(before.y-after.y)<1,'Reopen jumped to start');
      requestCloseWorkSheet();openWorkSheet(other);
      await new Promise(r=>setTimeout(r,300));
      check(isWorkSheetOpen()&&workLogState.id===other,'Old block closed new block');
      for(const opening of [true,false]){
        closeWorkSheet();openWorkSheet(id);
        if(!opening)requestCloseWorkSheet();
        sheet('<button id="replacement-action">Replacement sheet</button>');
        check(!document.querySelector('#workLogDialog').open&&workLogState.motion===null,'Replacement not synchronous');
        document.querySelector('#replacement-action').focus();
        await new Promise(r=>setTimeout(r,280));
        check(document.activeElement.id==='replacement-action','Stale finish stole replacement focus');
        closeSheet();
      }
      openWorkSheet(id);requestCloseWorkSheet();
      const signature=workLogState.signature;
      check(refreshWorkSheet()===false&&workLogState.signature===signature,'Closing refreshed details');
      S.view++;refreshWorkSheet();check(!isWorkSheetOpen(),'View switch kept modal');
      openWorkSheet(id);requestCloseWorkSheet();
      const block=S.workBlocks.get(id);S.workBlocks.delete(id);refreshWorkSheet();
      check(!isWorkSheetOpen(),'Missing block kept closing modal');S.workBlocks.set(id,block);
      openWorkSheet(id);requestCloseWorkSheet();
      S.sid='motion-new-session';refreshWorkSheet();check(!isWorkSheetOpen(),'Session switch kept modal');
      S.sid=sid;openWorkSheet(id);
      await new Promise(r=>setTimeout(r,300));check(isWorkSheetOpen(),'Old session closed reopened sheet');
      requestCloseWorkSheet();
      onEvent({type:'permission.asked',properties:{id:'motion-perm',sessionID:sid,permission:'bash',patterns:['fixture']}});
      check(!document.querySelector('#workLogDialog').open,'Permission delayed by closing modal');
      await new Promise(r=>setTimeout(r,280));
      const hit=document.elementFromPoint(2,2);
      check(!document.querySelector('#workLogDialog').contains(hit),'Hidden modal intercepts permission');
      return {before,after,final:motionFrame()};
    }''', {'id': WID, 'other': OTHER, 'sid': SID})
    evidence.update(result)


async def latest_focus_and_scroll(page, evidence):
    await ready(page)
    result = await page.evaluate('''async id=>{
      const d=document.querySelector('#workLogDialog'),body=document.querySelector('#workLogBody');
      body.scrollTop=body.scrollHeight/2;rememberWorkSheetPosition();
      const old=workSheetTrigger(id),before=body.scrollTop;
      let focuses=0,closes=0;
      const onFocus=e=>{if(e.target.matches('[data-work-open]'))focuses++};
      const onClose=()=>closes++;
      document.addEventListener('focusin',onFocus);d.addEventListener('close',onClose);
      requestCloseWorkSheet();renderMsgs();
      const main=document.querySelector('#main');main.scrollTop-=80;const scroll=main.scrollTop;
      await new Promise(r=>setTimeout(r,320));
      document.removeEventListener('focusin',onFocus);d.removeEventListener('close',onClose);
      return {replaced:!old.isConnected,focused:document.activeElement===workSheetTrigger(id),focuses,closes,
        bodyBefore:before,bodyAfter:body.scrollTop,mainBefore:scroll,mainAfter:main.scrollTop};
    }''', WID)
    evidence.update(result)
    assert result['replaced'] and result['focused'] and result['focuses'] == result['closes'] == 1, result
    assert abs(result['mainBefore'] - result['mainAfter']) <= 2, result


async def drag_handoff_and_settle(page, evidence):
    # Freeze only the input handoff phase so browser/tool latency cannot miss the
    # moving 28px handle. opening_and_closing samples unpaused real-time frames.
    await page.evaluate('''id=>{
      openWorkSheet(id);workLogState.motion.pause();workLogState.motion.currentTime=60;
    }''', WID)
    bounds = await page.locator(DIALOG + ' [data-work-grab]').bounding_box()
    await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 10)
    await page.mouse.down()
    assert await page.evaluate('!!workLogState.drag&&!workLogState.motion'), 'Drag did not cancel opening animation'
    before = await page.evaluate('motionFrame()')
    await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 46)
    after = await page.evaluate('motionFrame()')
    assert abs(after['y'] - before['y'] - 36) < 2, (before, after)
    await page.mouse.up()
    await page.wait_for_function('!workLogState.motion')
    assert await page.evaluate('isWorkSheetOpen()&&Math.abs(motionFrame().y)<1'), 'Small drag did not settle'
    evidence.update(before=before, after=after)
    bounds = await page.locator(DIALOG + ' [data-work-grab]').bounding_box()
    await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 10)
    await page.mouse.down()
    await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 120)
    before = await page.evaluate('motionFrame()')
    await page.mouse.up()
    after = await page.evaluate('motionFrame()')
    assert after['open'] and after['closing'] and after['y'] >= before['y'] - 2, (before, after)
    await page.wait_for_function('!isWorkSheetOpen()')
    await ready(page)
    # pointercancel must settle from its current offset, not replay the entrance.
    bounds = await page.locator(DIALOG + ' [data-work-grab]').bounding_box()
    await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 10)
    await page.mouse.down()
    await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 50)
    result = await page.evaluate('''()=>{
      const before=motionFrame();
      document.querySelector('[data-work-grab]').dispatchEvent(new PointerEvent('pointercancel',{pointerId:workLogState.drag.id}));
      return {before,after:motionFrame()};
    }''')
    assert abs(result['before']['y'] - result['after']['y']) < 1, result
    await page.mouse.up()
    await page.wait_for_function('!workLogState.motion')
    assert await page.evaluate('isWorkSheetOpen()&&Math.abs(motionFrame().y)<1')


async def reduced_motion(page, evidence):
    await page.emulate_media(reduced_motion='reduce')
    result = await page.evaluate('''id=>{
      openWorkSheet(id);const opened=motionFrame();
      requestCloseWorkSheet('reduced');const closed=motionFrame();
      return {opened,closed};
    }''', WID)
    evidence.update(result)
    assert result['opened']['open'] and result['opened']['y'] == 0 and result['opened']['opacity'] == 1
    assert result['opened']['running'] == result['closed']['running'] == 0, result
    assert not result['closed']['open'] and not result['closed']['modal'], result
    await page.emulate_media(reduced_motion='no-preference')
    await page.evaluate('id=>openWorkSheet(id)', WID)
    await page.emulate_media(reduced_motion='reduce')
    await page.wait_for_function('!workLogState.motion')
    assert await page.evaluate('isWorkSheetOpen()&&motionFrame().y===0')
    await page.emulate_media(reduced_motion='no-preference')
    await page.evaluate('requestCloseWorkSheet("changed-preference")')
    await page.emulate_media(reduced_motion='reduce')
    await page.wait_for_function('!isWorkSheetOpen()')
    assert await page.locator(DIALOG).evaluate('e=>e.returnValue') == 'changed-preference'


CASES = [opening_and_closing, user_close_paths, cancellation_and_replacements,
         latest_focus_and_scroll, drag_handoff_and_settle, reduced_motion]


async def main(args):
    out = Path(tempfile.mkdtemp(prefix='work-sheet-motion-'))
    source = HTML.read_text(encoding='utf-8')
    report = {'asset_sha256': hashlib.sha256(source.encode()).hexdigest(), 'checks': []}
    print(f'Evidence: {out}', flush=True)
    async with async_playwright() as pw:
        browser = await launch_browser(pw, args.browser_executable)
        try:
            for theme in args.theme or ['light', 'dark']:
                for width in args.width or [320, 412]:
                    for case in CASES:
                        if args.case and case.__name__ not in args.case:
                            continue
                        context, page, errors, unexpected = await boot_page(browser, theme=theme, source=source)
                        result = {'case': case.__name__, 'theme': theme, 'width': width}
                        try:
                            await page.set_viewport_size({'width': width, 'height': 800})
                            await page.emulate_media(reduced_motion='no-preference')
                            await fixture(page)
                            await case(page, result)
                            assert not errors, errors
                            assert not unexpected, unexpected
                            result['status'] = 'PASS'
                        except Exception:
                            result.update(status='FAIL', error=traceback.format_exc(), errors=errors, requests=unexpected)
                            await page.screenshot(path=str(out / f'{case.__name__}-{theme}-{width}.png'))
                            print(result['error'], flush=True)
                        finally:
                            await context.close()
                        report['checks'].append(result)
                        print(f'{result["status"]} {theme}/{width} {case.__name__}', flush=True)
        finally:
            await browser.close()
    failed = sum(r['status'] == 'FAIL' for r in report['checks'])
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'{len(report["checks"]) - failed}/{len(report["checks"])} passed; report: {out / "report.json"}', flush=True)
    return bool(failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable')
    parser.add_argument('--theme', action='append', choices=['light', 'dark'])
    parser.add_argument('--width', action='append', type=int, choices=[320, 412])
    parser.add_argument('--case', action='append', choices=[case.__name__ for case in CASES])
    raise SystemExit(asyncio.run(main(parser.parse_args())))
