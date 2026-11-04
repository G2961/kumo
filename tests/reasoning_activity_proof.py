"""Offline regression for strict contiguous reasoning/tool/text segments.

Run with ../.local/api-test-venv/Scripts/python.exe tests/reasoning_activity_proof.py.
Loads the actual asset, removing only its final boot() invocation. All requests
are intercepted; no OpenCode process, native bridge, model, or SSH is required.
Screenshots and a JSON report go to a printed temporary directory by default.
"""
import argparse
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'app/src/main/assets/codex.html'
ORIGIN = 'http://reasoning.test'
SID = 'ses_reasoning_fixture'
U1, A1, A2 = 'msg_001_user', 'msg_002_assistant', 'msg_003_assistant'
U2, A3 = 'msg_004_user', 'msg_005_assistant'
def thought(mid, pid):
    return f'.think[data-part="{mid}_{pid}_think"]'


def activity_segment(mid, pid):
    return f'.activity.tool-event[data-part="{mid}_{pid}_activity"][data-tool-part="{pid}"]'


THINK = thought(A1, 'r1')
LONG_REASON = ('Investigate the boundary conditions. ' +
               'The explanation preserves every observation rather than inventing tool steps. ' * 14 +
               'RAW_REASONING_END')
PATCH = '--- a/login.js\n+++ b/login.js\n@@ -1 +1 @@\n-oldLogin\n+fixedLogin\n'


def message(mid, role, parts=(), parent=None, completed=None):
    info = {'id': mid, 'sessionID': SID, 'role': role, 'time': {'created': 1000}}
    if parent:
        info['parentID'] = parent
    if completed is not None:
        info['time']['completed'] = completed
    return {'info': info, 'parts': [dict(p, messageID=mid, sessionID=SID) for p in parts]}


def text(pid, value):
    return {'id': pid, 'type': 'text', 'text': value}


def reason(pid, value, start=1000, end: int | None = 7000):
    timing = {'start': start}
    if end is not None:
        timing['end'] = end
    return {'id': pid, 'type': 'reasoning', 'text': value, 'time': timing}


def tool(pid, name, inputs, output='', status='completed', **state):
    return {'id': pid, 'type': 'tool', 'tool': name, 'callID': 'call_' + pid,
            'state': {'status': status, 'input': inputs, 'output': output, **state}}


def user(mid=U1):
    return message(mid, 'user', [text('prompt_' + mid, 'Check the implementation.')])


async def launch_browser(pw, executable=None):
    """Use an existing browser; never download Playwright browsers during a proof."""
    executable = executable or os.environ.get('PLAYWRIGHT_BROWSER_EXECUTABLE') or os.environ.get('CHROME_EXECUTABLE')
    return await pw.chromium.launch(**({'executable_path': str(executable)} if executable else {}))


async def boot_page(browser, theme='dark', source=None):
    source = HTML.read_text(encoding='utf-8') if source is None else source
    # Keep all production rendering/event handlers. Do not mock renderMsgs or onEvent.
    source, count = re.subn(r'S\.bootP\s*=\s*boot\(\);(?=\s*</script>\s*</body>)',
                            'S.bootP=Promise.resolve();', source)
    assert count == 1, 'Expected exactly one final boot() call in the real HTML asset'
    context = await browser.new_context(viewport={'width': 360, 'height': 800},
                                        color_scheme=theme, service_workers='block')
    errors, unexpected = [], []
    page = await context.new_page()
    page.set_default_timeout(2500)
    page.set_default_navigation_timeout(15000)
    page.on('pageerror', lambda error: errors.append(str(error)))

    async def route_request(route):
        path = urlsplit(route.request.url).path
        if route.request.url == ORIGIN + '/_ui/index.html':
            await route.fulfill(body=source, content_type='text/html; charset=utf-8')
        elif route.request.method == 'GET' and path == '/session':
            # session.idle refreshes the sidebar after finishing a streamed turn.
            await route.fulfill(json=[])
        else:
            unexpected.append(f'{route.request.method} {route.request.url}')
            await route.abort()

    await context.route('**/*', route_request)
    await page.add_init_script('localStorage.clear();' +
                              f'localStorage.setItem("oc_theme", JSON.stringify({json.dumps(theme)}));' +
                              'localStorage.setItem("oc_lp_hint", "true");')
    await page.goto(ORIGIN + '/_ui/index.html')
    await page.wait_for_function('typeof renderMsgs === "function" && typeof onEvent === "function"')
    return context, page, errors, unexpected


async def seed(page, messages, running=False, runs=()):
    await page.evaluate('''({messages,running,runs,sid})=>{
      document.querySelector('#main').innerHTML='';
      S.sid=sid;S.dir='';S.running=running;S.status={type:running?'busy':'idle'};
      // Prevent the production polling watchdog from replacing offline fixtures.
      S.sending=true;S.lastErr=null;S.pendingUser=null;S.revision=0;
      S.msgs=new Map(messages.map(m=>[m.info.id,{info:m.info,parts:new Map(m.parts.map(p=>[p.id,p]))}]));
      S.perms=new Map();S.questions=new Map();S.historyFiles=new Map();S.changeHints=new Map();
      S.connections=[{id:'conn_fixture',name:'Fixture SSH',host:'fixture.invalid'}];
      S.remoteRuns=new Map(runs.map(r=>[r.id,r]));
      setRun();renderMsgs(true);
    }''', {'messages': messages, 'running': running, 'runs': list(runs), 'sid': SID})


async def event(page, event_type, **properties):
    await page.evaluate('ev=>onEvent(ev)', {'type': event_type, 'properties': properties})
    # Exercise the real requestAnimationFrame scheduler, not an extra forced render.
    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')


async def rerender(page):
    await page.evaluate('renderMsgs(true)')
    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(resolve))')


async def expanded(page, selector, value):
    button = page.locator(selector + ' > .think-head')
    await expect(button).to_have_attribute('aria-expanded', str(value).lower())
    control = await button.get_attribute('aria-controls')
    assert control, 'Thought button must reference its controlled body'

    target_info = await button.evaluate('''b=>{
      const target=document.getElementById(b.getAttribute('aria-controls'));
      return target&&{inside:b.closest('.think').contains(target),height:target.getBoundingClientRect().height};
    }''')
    assert target_info and target_info['inside'], f'Broken aria-controls: {control}'
    await page.wait_for_timeout(240)
    height = await page.locator('[id=' + json.dumps(control) + ']').evaluate('e=>e.getBoundingClientRect().height')
    assert (height > 1) == value, f'aria-expanded={value}, controlled body height={height}'
    inert = await page.locator('[id=' + json.dumps(control) + ']').evaluate('e=>e.inert')
    assert inert == (not value), f'Collapsed reasoning must be inert, got {inert}'


async def open_thought(page, selector=None):
    if selector is None:
        selector = '.think[data-part=' + json.dumps(await page.locator('.think').first.get_attribute('data-part')) + ']'
    await expect(page.locator(selector)).to_have_count(1)
    if await page.locator(selector + ' > .think-head').get_attribute('aria-expanded') != 'true':
        await page.locator(selector + ' > .think-head').click()
    await expanded(page, selector, True)


async def assert_sequence(page, expected):
    actual = await page.locator('#main > .think, #main > .activity, #main > .msg.ai').evaluate_all('''els=>els.map(e=>
      e.matches('.msg.ai')?'text:'+e.dataset.raw:e.dataset.part)''')
    assert actual == expected, f'Segments reordered or merged: {actual!r} != {expected!r}'
    controls = await page.locator('.think-head').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))')
    assert all(controls) and len(set(controls)) == len(controls), f'Segment controls must be unique: {controls}'


async def assert_separation(page):
    await expect(page.locator('.topic')).to_have_count(0)
    await expect(page.locator('.think .tool-event, .think .card, .think .activity, '
                              '.think .collapsible-output, .think .change-preview')).to_have_count(0)
    await expect(page.locator('.think-indicator, .reason-step, .think-steps, .reasoning-details')).to_have_count(0)
    for block in await page.locator('.think').all():
        await expect(block.locator('.feed-text-window > .rtext')).to_have_count(1)
        await expect(block.locator('.think-label')).to_have_count(1)
        await expect(block.locator('.think-duration')).to_have_count(1)
    for activity in await page.locator('.activity').all():
        assert await activity.evaluate('e=>e.matches("details.tool-event, div.tool-event") && !!e.dataset.toolPart')
        await expect(activity.locator('details, .card, .tool-event')).to_have_count(0)
        if await activity.evaluate('e=>e.matches("div")'):
            await expect(activity.locator('summary, .caret, .activity-body, [data-feed-expand]')).to_have_count(0)


async def native_timeline(page, out):
    body = '## Inspect the request\n\nCompare the alternatives.\n\nCheck the conclusion.'
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', body), text('answer', 'Done.')], U1, 7500)])
    await expect(page.locator('.think')).to_have_count(1)
    await expect(page.locator(THINK + ' .think-label')).to_have_text('Inspect the request')
    await expect(page.locator(THINK + ' .think-duration')).to_have_text('6s')
    await expanded(page, THINK, False)
    await open_thought(page)
    await expect(page.locator(THINK + ' .rtext')).to_have_count(1)
    assert await page.locator(THINK + ' .rtext').text_content() == body
    await expect(page.locator(THINK + ' [data-feed-expand]')).to_be_hidden()
    await assert_separation(page)
    await page.screenshot(path=str(out / 'native-open.png'))


async def grouped_turns(page, out):
    await seed(page, [
        # Deliberately shuffled input: IDs still represent real chronological order.
        message(A2, 'assistant', [reason('r2', 'Brief.', 8000, 9000), text('answer', 'Final answer.')], U1, 9500),
        user(), message(A1, 'assistant', [reason('r1', 'Consider the first constraint.'),
            tool('local', 'bash', {'command': 'fixture-local-command'}, 'LOCAL_OUTPUT',
                 time={'start': 7000, 'end': 8000})], U1, 7500)])
    await expect(page.locator('.think')).to_have_count(2)
    await assert_sequence(page, [f'{A1}_r1_think', f'{A1}_local_activity', f'{A2}_r2_think', 'text:Final answer.'])
    await expect(page.locator(THINK + ' .think-duration')).to_have_text('6s')
    await expect(page.locator(thought(A2, 'r2') + ' .think-duration')).to_have_text('1s')
    await expanded(page, thought(A2, 'r2'), False)
    await open_thought(page)
    labels = await page.locator('.think .rtext').all_text_contents()
    assert labels == ['Consider the first constraint.', 'Brief.'], labels
    await expect(page.locator('.activity')).to_have_count(1)
    assert await page.locator('.activity').evaluate('e=>e.parentElement===document.querySelector(".think").parentElement')
    assert 'fixture-local-command' not in await page.locator(THINK).text_content()
    await event(page, 'message.updated', info=message(U2, 'user')['info'])
    await event(page, 'message.part.updated', part=message(U2, 'user', [text('u2text', 'One more thing.')])['parts'][0])
    await event(page, 'message.updated', info=message(A3, 'assistant', parent=U2, completed=20000)['info'])
    await event(page, 'message.part.updated', part=message(A3, 'assistant', [reason('r3', 'Separate turn.', 18000, 19000)])['parts'][0])
    await expect(page.locator('.think')).to_have_count(3)
    await expect(page.locator(thought(A3, 'r3') + ' .think-duration')).to_have_text('1s')
    await expanded(page, THINK, True)
    await assert_separation(page)
    # The first message may contain only a tool. Later reasoning must keep its key.
    await seed(page, [user(), message(A1, 'assistant', [
        tool('first_tool', 'bash', {'command': 'fixture-first-tool'}, 'FIRST_TOOL_OUTPUT')], U1, 7500)], running=True)
    await event(page, 'message.updated', info=message(A2, 'assistant', parent=U1)['info'])
    await event(page, 'message.part.updated', part=message(A2, 'assistant', [
        reason('later_reason', 'Reason after the first tool.', 8000, None)])['parts'][0])
    await expect(page.locator('.think')).to_have_count(1)
    later = thought(A2, 'later_reason')
    await expect(page.locator(later + '[data-running]')).to_have_count(1)
    await expect(page.locator('.think .rtext')).to_have_text('Reason after the first tool.')
    assert 'FIRST_TOOL_OUTPUT' not in await page.locator(later).text_content()
    await assert_sequence(page, [f'{A1}_first_tool_activity', f'{A2}_later_reason_think'])


async def same_message_segments(page, out):
    parts = [reason('r1', 'Inspect the first constraint.', 1000, 3000),
             reason('r1b', 'Compare the contiguous evidence.', 2000, 5000),
             tool('t1', 'bash', {'command': 'fixture-first-command'}, 'FIRST_OUTPUT',
                  time={'start': 5000, 'end': 99000}),
             reason('r2', 'Check the second constraint.', 100000, 102000),
             tool('t2', 'read', {'filePath': 'src/input.txt'}, 'SECOND_OUTPUT'),
             text('answer', 'Both constraints checked.')]
    saved = [user(), message(A1, 'assistant', parts, U1, 200000)]
    expected = [f'{A1}_r1_think', f'{A1}_t1_activity', f'{A1}_r2_think',
                f'{A1}_t2_activity', 'text:Both constraints checked.']
    await seed(page, saved)
    await assert_sequence(page, expected)
    first, second = thought(A1, 'r1'), thought(A1, 'r2')
    for selector, duration in [(first, '4s'), (second, '2s')]:
        await expanded(page, selector, False)
        await expect(page.locator(selector + ' .think-duration')).to_have_text(duration)
    await expect(page.locator(first + ' .rtext')).to_have_text(
        'Inspect the first constraint.\n\nCompare the contiguous evidence.')
    await expect(page.locator(second + ' .rtext')).to_have_text('Check the second constraint.')
    await open_thought(page, first)
    await expanded(page, second, False)
    await page.locator(activity_segment(A1, 't1') + ' > summary').click()
    await rerender(page)
    await expanded(page, first, True)
    await expanded(page, second, False)
    assert await page.locator(activity_segment(A1, 't1')).evaluate('e=>e.open')
    assert not await page.locator(activity_segment(A1, 't2')).evaluate('e=>e.open')
    await open_thought(page, second)
    await page.locator(first + ' > .think-head').click()
    await rerender(page)
    await expanded(page, first, False)
    await expanded(page, second, True)
    for pid, label in [('t1', 'fixture-first-command'), ('t2', 'read')]:
        activity = page.locator(activity_segment(A1, pid))
        await expect(activity.locator(':scope > summary')).to_contain_text(re.compile(re.escape(label), re.IGNORECASE))
        await expect(activity.locator(':scope > summary')).not_to_contain_text('Activity')
        await expect(activity).to_have_attribute('data-tool-part', pid)
        await expect(activity.locator('details, .card')).to_have_count(0)
    await assert_separation(page)
    await page.screenshot(path=str(out / 'same-message-sequence.png'))
    # A real navigation destroys all DOM expansion state; reconstruct from saved parts only.
    await page.reload()
    await seed(page, saved)
    await assert_sequence(page, expected)
    for selector, duration in [(first, '4s'), (second, '2s')]:
        await expanded(page, selector, False)
        await expect(page.locator(selector + ' .think-duration')).to_have_text(duration)
    await expect(page.locator('.think[data-running]')).to_have_count(0)


async def text_and_tag_boundaries(page, out):
    await seed(page, [user(), message(A1, 'assistant', [
        reason('native', 'Native reasoning.', 1000, 3000),
        text('embedded', '<think>Embedded reasoning.</think>Visible boundary.'),
        reason('after_text', 'Reasoning after visible text.', 8000, 9000),
        tool('t1', 'bash', {'command': 'fixture-one'}, 'ONE'),
        text('commentary', 'Commentary between commands.'),
        tool('t2', 'bash', {'command': 'fixture-two'}, 'TWO'),
        tool('t3', 'read', {'filePath': 'README.md'}, 'THREE'),
        reason('last', 'Last reasoning.', 12000, 14000),
        text('answer', 'Final answer.')], U1, 15000)])
    await assert_sequence(page, [f'{A1}_native_think', f'{A1}_embedded_think', 'text:Visible boundary.',
        f'{A1}_after_text_think', f'{A1}_t1_activity', 'text:Commentary between commands.',
        f'{A1}_t2_activity', f'{A1}_t3_activity', f'{A1}_last_think', 'text:Final answer.'])
    await expect(page.locator(thought(A1, 'embedded') + ' .think-label')).to_have_text('Reasoning')
    for pid, duration in [('native', '2s'), ('after_text', '1s'), ('last', '2s')]:
        await expect(page.locator(thought(A1, pid) + ' .think-duration')).to_have_text(duration)
    await expect(page.locator(activity_segment(A1, 't2') + ' > summary .activity-label')).to_have_text('fixture-two')
    await expect(page.locator(activity_segment(A1, 't3') + ' > summary .activity-label')).to_contain_text('README.md')
    assert await page.locator('.tool-event').evaluate_all('els=>els.map(e=>e.dataset.toolPart)') == ['t1', 't2', 't3']
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await assert_separation(page)


async def compact_previews(page, out):
    paragraphs = ['Short first sentence. This second sentence must remain in the original body.',
                  'X' * 140, 'Third observation. More detail stays in the source.',
                  'Fourth observation.', 'Fifth observation.']
    raw = '\n\n'.join(paragraphs)
    for running in [False, True]:
        await seed(page, [user(), message(A1, 'assistant', [reason('r1', raw, end=None if running else 7000)],
                                          U1, None if running else 8000)], running=running)
        await open_thought(page)
        block = page.locator(THINK)
        await expect(block.locator('.think-label')).to_have_text('Reasoning')
        await expect(block.locator('.rtext')).to_have_count(1)
        assert await block.locator('.rtext').text_content() == raw
        window = block.locator('.feed-text-window')
        assert await window.evaluate('e=>e.scrollHeight>e.clientHeight && e.getBoundingClientRect().height<=133')
        button = block.locator('[data-feed-expand]')
        await expect(button).to_have_text('Show more')
        await button.click()
        assert await window.evaluate('e=>e.getBoundingClientRect().height>132')
        await rerender(page)
        assert await window.evaluate('e=>e.getBoundingClientRect().height>132')
        assert await block.locator('.rtext').text_content() == raw
        await assert_separation(page)
    await page.screenshot(path=str(out / 'compact-preview.png'))


async def current_segment_streaming(page, out):
    for across_messages in [False, True]:
        mid = A2 if across_messages else A1
        await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'First live thought.', end=None)], U1)], running=True)
        await expanded(page, THINK, True)
        await event(page, 'message.part.updated', part=message(A1, 'assistant', [
            tool('t1', 'bash', {'command': 'fixture-boundary'}, status='running')])['parts'][0])
        await expanded(page, THINK, False)
        await expect(page.locator('.think[data-running]')).to_have_count(0)
        assert await page.evaluate('S.running')
        await event(page, 'message.part.updated', part=message(A1, 'assistant', [
            reason('r1', 'First live thought.', 1000, 5000)])['parts'][0])
        await open_thought(page, THINK)
        if across_messages:
            await event(page, 'message.updated', info=message(A1, 'assistant', parent=U1, completed=7000)['info'])
            await event(page, 'message.updated', info=message(A2, 'assistant', parent=U1)['info'])
        await event(page, 'message.part.updated', part=message(mid, 'assistant', [
            reason('r2', 'Second live thought.', 9000, None)])['parts'][0])
        current = thought(mid, 'r2')
        await expanded(page, current, True)
        await expanded(page, THINK, True)
        before = await page.locator(THINK).text_content()
        await event(page, 'message.part.delta', sessionID=SID, messageID=mid, partID='r2',
                    field='text', delta='\n\nOnly the current segment receives this delta.')
        assert await page.locator(THINK).text_content() == before, 'A later delta modified earlier reasoning'
        await expect(page.locator(current + ' .rtext')).to_have_text(
            'Second live thought.\n\nOnly the current segment receives this delta.')
        await expect(page.locator(current + '[data-running]')).to_have_count(1)
        await expect(page.locator(THINK + '[data-running]')).to_have_count(0)
        await page.locator(current + ' > .think-head').click()
        await event(page, 'message.part.delta', sessionID=SID, messageID=mid, partID='r2',
                    field='text', delta='\n\nStill independently closed.')
        await expanded(page, current, False)
        await expanded(page, THINK, True)
        await event(page, 'message.part.updated', part=message(mid, 'assistant', [
            tool('t2', 'read', {'filePath': 'README.md'}, 'READ_RESULT')])['parts'][0])
        await expect(page.locator('.think[data-running]')).to_have_count(0)
        await event(page, 'message.part.updated', part=message(mid, 'assistant', [
            reason('r2', 'Second live thought.\n\nOnly the current segment receives this delta.\n\nStill independently closed.', 9000, 11000)])['parts'][0])
        await event(page, 'message.part.updated', part=message(mid, 'assistant', [text('answer', 'Stream finished.')])['parts'][0])
        await assert_sequence(page, [f'{A1}_r1_think', f'{A1}_t1_activity', f'{mid}_r2_think',
                                    f'{mid}_t2_activity', 'text:Stream finished.'])
        await expect(page.locator(THINK + ' .think-duration')).to_have_text('4s')
        await expect(page.locator(current + ' .think-duration')).to_have_text('2s')
        await expanded(page, THINK, True)
        await expanded(page, current, False)
        saved = await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))')
        await page.reload()
        await seed(page, saved, running=True)
        await assert_sequence(page, [f'{A1}_r1_think', f'{A1}_t1_activity', f'{mid}_r2_think',
                                    f'{mid}_t2_activity', 'text:Stream finished.'])
        await expanded(page, THINK, False)
        await expanded(page, current, False)
        await expect(page.locator('.think[data-running]')).to_have_count(0)


async def busy_boundaries(page, out):
    # A boundary ends reasoning even when the provider omitted its end timestamp.
    for mid in [A1, A2]:
        for boundary in [tool('boundary', 'bash', {'command': 'fixture-break'}, status='running'),
                         text('boundary', 'Visible commentary ends reasoning.')]:
            await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Unfinished source reasoning.', end=None)], U1)], running=True)
            await expanded(page, THINK, True)
            if mid != A1:
                await event(page, 'message.updated', info=message(mid, 'assistant', parent=U1)['info'])
            await event(page, 'message.part.updated', part=message(mid, 'assistant', [boundary])['parts'][0])
            assert await page.evaluate('S.running')
            await expanded(page, THINK, False)
            await expect(page.locator('.think[data-running]')).to_have_count(0)
            boundary_key = f'{mid}_boundary_activity' if boundary['type'] == 'tool' else 'text:' + boundary['text']
            await assert_sequence(page, [f'{A1}_r1_think', boundary_key])
            await event(page, 'message.part.updated', part=message(mid, 'assistant', [
                reason('next', 'New reasoning after the boundary.', 9000, None)])['parts'][0])
            await expanded(page, thought(mid, 'next'), True)
            await expanded(page, THINK, False)
            await expect(page.locator('.think[data-running]')).to_have_count(1)
            await expect(page.locator(THINK + ' .rtext')).to_have_text('Unfinished source reasoning.')
            await assert_sequence(page, [f'{A1}_r1_think', boundary_key, f'{mid}_next_think'])


async def completed_reasoning_busy(page, out):
    # No tool, visible answer, message completion, or session idle to hide a stale spinner.
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Awaiting the next part.', end=None)], U1)], running=True)
    await expanded(page, THINK, True)
    await expect(page.locator(THINK + '[data-running]')).to_have_count(1)
    done = reason('r1', 'Awaiting the next part.', 1000, 6000)
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [done])['parts'][0])
    assert await page.evaluate('S.running && !S.msgs.get(' + json.dumps(A1) + ').info.time.completed')
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expanded(page, THINK, False)
    await expect(page.locator(THINK + ' .think-duration')).to_have_text('5s')
    await page.reload()
    await seed(page, [user(), message(A1, 'assistant', [done], U1)], running=True)
    await expanded(page, THINK, False)
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expect(page.locator(THINK + ' .think-duration')).to_have_text('5s')
    await open_thought(page)
    await event(page, 'session.status', sessionID=SID, status={'type': 'busy'})
    await expanded(page, THINK, True)
    await expect(page.locator('.think[data-running]')).to_have_count(0)


async def tagged_and_raw(page, out):
    tagged = '<think>Plan the tagged answer.\n\nVerify the tagged result.</think>Visible answer only.'
    await seed(page, [user(), message(A1, 'assistant', [text('tagged', tagged)], U1, 9000)])
    await open_thought(page)
    assert await page.locator('.think .rtext').text_content() == 'Plan the tagged answer.\n\nVerify the tagged result.'
    await expect(page.locator('.msg.ai')).to_have_text('Visible answer only.')
    await expect(page.locator('.activity')).to_have_count(0)
    # Long source must remain available verbatim, not just its abbreviated label.
    raw = LONG_REASON + '\n\nLiteral <img src=x onerror=alert(1)> stays text.'
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [reason('long', raw)])['parts'][0])
    await assert_sequence(page, [f'{A1}_tagged_think', 'text:Visible answer only.', f'{A1}_long_think'])
    await open_thought(page, thought(A1, 'long'))
    block = page.locator(thought(A1, 'long'))
    await expect(block.locator('.rtext')).to_have_count(1)
    assert await block.locator('.rtext').text_content() == raw, 'Full raw reasoning was lost'
    await expect(page.locator('.think img, .think script')).to_have_count(0)
    await expect(block.locator('.think-label')).to_have_text('Reasoning')
    button = block.locator('[data-feed-expand]')
    await expect(button).to_have_text('Show more')
    await button.click()
    await rerender(page)
    assert await block.locator('.feed-text-window').evaluate('e=>e.getBoundingClientRect().height>132')
    await button.click()
    await rerender(page)
    assert await block.locator('.feed-text-window').evaluate('e=>e.getBoundingClientRect().height<=133')
    assert await block.locator('.rtext').text_content() == raw
    await assert_separation(page)


async def reasoning_identifiers_and_raw(page, out):
    plain = 'Inspect __init__ and __name__ before deciding.'
    formatted = '## Inspect `__init__`\n\nCompare **__name__** with the module name.'
    fenced = 'Inspect the initializer.\n\n```python\ndef __init__(self):\n    self.module = __name__\n```'
    for raw, label in [(plain, 'Reasoning'), (formatted, 'Inspect __init__'), (fenced, 'Reasoning')]:
        await seed(page, [user(), message(A1, 'assistant', [reason('identifiers', raw)], U1, 9000)])
        await open_thought(page)
        await expect(page.locator('.think-label')).to_have_text(label)
        await expect(page.locator('.think .rtext')).to_have_count(1)
        assert await page.locator('.think .rtext').text_content() == raw, 'Original Markdown/code was transformed'
        await expect(page.locator('.think .rtext code, .think .rtext pre')).to_have_count(0)
        await rerender(page)
        await expanded(page, thought(A1, 'identifiers'), True)
        assert await page.locator('.think .rtext').text_content() == raw
        await assert_separation(page)
    await page.screenshot(path=str(out / 'identifiers-raw.png'))


async def literal_think_tags(page, out):
    fixtures = [
        ('Use `<think>literal inline</think>` as a token.', '<think>literal inline</think>', 'code'),
        ('Use `<think>` as the opening token.', '<think>', 'code'),
        ('Example:\n\n```xml\n<think>literal fenced</think>\n```\n\nKeep the example.', '<think>literal fenced</think>', 'pre code'),
        ('Example:\n\n```xml\n<think>literal unfinished fence', '<think>literal unfinished fence', None),
        ('The opening token is <think> and the rest is ordinary answer text.',
         'The opening token is <think> and the rest is ordinary answer text.', None),
    ]
    for raw, visible, selector in fixtures:
        for running in [True, False]:
            await seed(page, [user(), message(A1, 'assistant', [text('literal', raw)], U1,
                                              None if running else 9000)], running=running)
            await expect(page.locator('.think')).to_have_count(0)
            await expect(page.locator('.msg.ai')).to_have_count(1)
            await expect(page.locator('.msg.ai')).to_have_attribute('data-raw', raw)
            await expect(page.locator('.msg.ai')).to_contain_text(visible)
            if selector:
                await expect(page.locator('.msg.ai ' + selector)).to_contain_text(visible)
            await rerender(page)
            await expect(page.locator('.think')).to_have_count(0)
    # Real reasoning and literal examples can coexist in the same text part.
    answer = 'Keep `<think>inline example</think>` and this example:\n\n```xml\n<think>fenced example</think>\n```'
    await seed(page, [user(), message(A1, 'assistant', [
        text('mixed', '<think>Decide which example to show.</think>' + answer)], U1, 9000)])
    await open_thought(page)
    await expect(page.locator('.think .rtext')).to_have_text('Decide which example to show.')
    await expect(page.locator('.msg.ai')).to_have_attribute('data-raw', answer)
    await expect(page.locator('.msg.ai code')).to_have_text(['<think>inline example</think>', '<think>fenced example</think>'])
    await page.screenshot(path=str(out / 'literal-tags.png'))


async def interrupted_leading_tag(page, out):
    raw = '  <think>Inspect __init__.\n\nCheck __name__ before proceeding.'
    expected = 'Inspect __init__.\n\nCheck __name__ before proceeding.'
    await seed(page, [user(), message(A1, 'assistant', [text('interrupted', raw)], U1)], running=True)
    await expect(page.locator('.think .rtext')).to_have_text(expected)
    await expect(page.locator('.msg.ai')).to_have_count(0)
    await expect(page.locator(thought(A1, 'interrupted') + '[data-running]')).to_have_count(1)
    aborted = message(A1, 'assistant', parent=U1)['info']
    aborted['error'] = {'name': 'MessageAbortedError', 'data': {'message': 'Request aborted'}}
    await event(page, 'message.updated', info=aborted)
    assert not await page.evaluate('S.running')
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expect(page.locator('.error-card')).to_contain_text('Stopped')
    await expect(page.locator('.msg.ai')).to_have_count(0)
    await open_thought(page)
    await expect(page.locator('.think .rtext')).to_have_text(expected)
    await rerender(page)
    await expect(page.locator('.think .rtext')).to_have_text(expected)
    await expect(page.locator('.msg.ai')).to_have_count(0)
    await page.screenshot(path=str(out / 'interrupted-tag.png'))
    # Reloading a saved aborted message must retain the same classification.
    saved = message(A1, 'assistant', [text('interrupted', raw)], U1)
    saved['info'] = aborted
    await seed(page, [user(), saved])
    await open_thought(page)
    await expect(page.locator('.think .rtext')).to_have_text(expected)
    await expect(page.locator('.msg.ai, .think[data-running]')).to_have_count(0)


async def no_fake_thought(page, out):
    fixtures = [
        [text('plain', 'Direct answer.')],
        [reason('empty', '  \n '), text('plain', 'Direct answer.')],
        [tool('onlytool', 'bash', {'command': 'fixture-tool-only'}, 'TOOL_ONLY_OUTPUT')],
    ]
    for completed in [9000, None]:
        for parts in fixtures:
            await seed(page, [user(), message(A1, 'assistant', parts, U1, completed)])
            await expect(page.locator('.think')).to_have_count(0)
            if parts[0]['type'] == 'tool':
                await expect(page.locator(activity_segment(A1, 'onlytool'))).to_have_count(1)
                assert not await page.locator('.activity').evaluate('e=>e.open')
            else:
                await expect(page.locator('.activity')).to_have_count(0)
                await expect(page.locator('.msg.ai')).to_have_text('Direct answer.')


async def activity_cards(page, out):
    remote = tool('ssh', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': 'fixture-ssh-command'},
                  json.dumps({'runID': 'remote_fixture'}), status='running')
    parts = [reason('r1', 'Evaluate the requested change.'),
             tool('local', 'bash', {'command': 'fixture-local-command'}, 'LOCAL_OUTPUT'),
             tool('read', 'read', {'filePath': 'src/input.txt'}, 'READ_OUTPUT'),
             tool('edit', 'edit', {'filePath': 'src/login.js'}, metadata={'diff': PATCH}),
             remote]
    run = {'id': 'remote_fixture', 'sessionID': SID, 'connectionID': 'conn_fixture', 'kind': 'exec',
           'command': 'fixture-ssh-command', 'stdout': 'LIVE_REMOTE_OUTPUT', 'stderr': '', 'status': 'running'}
    await seed(page, [user(), message(A1, 'assistant', parts, U1)], running=True, runs=[run])
    activities = page.locator('.activity')
    await expect(activities).to_have_count(4)
    await assert_separation(page)
    for pid in ['local', 'read', 'edit', 'ssh']:
        activity = page.locator(activity_segment(A1, pid))
        await expect(activity).to_have_attribute('data-tool-part', pid)
        await expect(activity.locator('details, .card')).to_have_count(0)
        assert await activity.evaluate('e=>e.open') == (pid == 'ssh'), 'Only the running tool auto-opens'
    local = page.locator(activity_segment(A1, 'local'))
    remote = page.locator(activity_segment(A1, 'ssh'))
    await expect(local).to_contain_text('LOCAL_OUTPUT')
    await expect(remote.locator('.tool-environment')).to_have_text('SSH · Fixture SSH')
    await expect(remote).to_contain_text('LIVE_REMOTE_OUTPUT')
    await expect(page.locator('#main pre', has_text='LIVE_REMOTE_OUTPUT')).to_have_count(1)
    assert await remote.locator('.tool-output').text_content() == 'LIVE_REMOTE_OUTPUT'
    assert await page.evaluate('S.renderedRemoteRuns.has("remote_fixture")'), 'Matched remote run was not claimed'
    for pid in ['read', 'edit']:
        card = page.locator(activity_segment(A1, pid))
        await card.locator(':scope > summary').click()
        await expect(card).to_have_attribute('open', '')
    read = page.locator(activity_segment(A1, 'read'))
    edit = page.locator(activity_segment(A1, 'edit'))
    await expect(read.locator('.tool-output')).to_have_text('READ_OUTPUT')
    added = edit.locator('.diff-line.a')
    deleted = edit.locator('.diff-line.d')
    await expect(added).to_have_count(1)
    await expect(deleted).to_have_count(1)
    await expect(added.locator('.diff-text')).to_have_text('fixedLogin')
    await expect(deleted.locator('.diff-text')).to_have_text('oldLogin')
    await expect(added.locator('.diff-sign')).to_have_text('+')
    await expect(deleted.locator('.diff-sign')).to_have_text('−')
    await expect(added.locator('.diff-num')).to_have_text('1')
    await expect(deleted.locator('.diff-num')).to_have_text('1')
    for forbidden in ['fixture-local-command', 'LOCAL_OUTPUT', 'READ_OUTPUT', 'fixedLogin', 'fixture-ssh-command', 'LIVE_REMOTE_OUTPUT']:
        assert forbidden not in await page.locator('.think').text_content(), f'Tool content leaked into Thought: {forbidden}'
    below = await read.evaluate('''e=>{
      const head=e.querySelector('summary').getBoundingClientRect(),output=e.querySelector('.tool-output').getBoundingClientRect();
      return output.top>=head.bottom-1;
    }''')
    assert below, 'Read output must be below its own summary'
    await rerender(page)
    for pid in ['read', 'edit']:
        assert await page.locator(activity_segment(A1, pid)).evaluate('e=>e.open'), f'{pid} expansion lost on rerender'
    await read.locator(':scope > summary').click()
    await rerender(page)
    assert not await read.evaluate('e=>e.open')
    await open_thought(page)
    await page.locator('.think').scroll_into_view_if_needed()
    await page.screenshot(path=str(out / 'activity-open.png'))


async def ssh_independent_stream_fallback(page, out):
    inputs = {'connection_id': 'conn_fixture', 'command': 'fixture-stream-command'}
    # Each channel falls back independently; the full final result takes precedence
    # over a bounded native-event buffer, never the other way around.
    final_stdout = 'FINAL_STDOUT_START\n' + 'complete stdout line\n' * 260 + 'FINAL_STDOUT_END'
    final_stderr = 'FINAL_STDERR_START\n' + 'complete stderr line\n' * 120 + 'FINAL_STDERR_END'
    for live, final, expected, excluded in [
        ({'stdout': 'LIVE_PARTIAL_STDOUT', 'stderr': ''},
         {'stderr': 'FINAL_STDERR'}, ['LIVE_PARTIAL_STDOUT', 'FINAL_STDERR'], None),
        ({'stdout': '', 'stderr': 'LIVE_PARTIAL_STDERR'},
         {'stdout': 'FINAL_STDOUT'}, ['FINAL_STDOUT', 'LIVE_PARTIAL_STDERR'], None),
        ({'stdout': 'BOUNDED_STDOUT_BUFFER', 'stderr': 'BOUNDED_STDERR_BUFFER'},
         {'stdout': final_stdout, 'stderr': final_stderr},
         [final_stdout, final_stderr], 'BOUNDED_'),
    ]:
        run = {'id': 'mixed_stream_run', 'sessionID': SID, 'connectionID': 'conn_fixture',
               'kind': 'exec', 'command': inputs['command'], 'status': 'running', **live}
        pending = tool('mixed_stream', 'mobile_ssh_ssh_exec', inputs, status='running')
        await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Inspect both output streams.'), pending], U1)],
                   running=True, runs=[run])
        final_part = tool('mixed_stream', 'mobile_ssh_ssh_exec', inputs,
                          json.dumps({'runID': run['id'], **final}))
        await event(page, 'message.part.updated', part=message(A1, 'assistant', [final_part])['parts'][0])
        activity = page.locator('.activity')
        assert not await activity.evaluate('e=>e.open'), 'Completed activity should no longer be auto-open'
        await activity.locator(':scope > summary').click()
        output = activity.locator('.tool-output')
        await expect(output).to_have_count(2)
        for part in await output.all():
            await expect(part).to_be_visible()
        await expect(activity.locator('pre.tool-command, .tool-command pre')).to_contain_text('fixture-stream-command')
        assert await output.all_text_contents() == expected
        for marker in expected:
            await expect(page.locator('#main pre', has_text=marker)).to_have_count(1)
            assert marker not in await page.locator('.think').text_content()
        if excluded:
            assert excluded not in await page.locator('#main').text_content()
        assert await page.evaluate('S.renderedRemoteRuns.has("mixed_stream_run")')
        await rerender(page)
        assert await output.all_text_contents() == expected
        for part in await output.all():
            await expect(part).to_be_visible()
        await assert_separation(page)
    await page.screenshot(path=str(out / 'ssh-mixed-streams.png'))


async def activity_after_commentary(page, out):
    commentary = '\n\n'.join(f'Observation {i}: this commentary explains the findings before the next command.' for i in range(1, 25))
    for earlier_tool in [False, True]:
        parts = [reason('r1', 'Review the evidence before continuing.')]
        if earlier_tool:
            parts.append(tool('earlier', 'bash', {'command': 'fixture-earlier-command'}, 'EARLIER_OUTPUT'))
        parts.append(text('commentary', commentary))
        await seed(page, [user(), message(A1, 'assistant', parts, U1, 9000)], running=True)
        await page.wait_for_timeout(240)
        initial = await page.evaluate('''()=>{
          const m=document.querySelector('#main'),comment=document.querySelector('.msg.ai');
          m.scrollTop=m.scrollHeight;
          return {commentHeight:comment.getBoundingClientRect().height,viewportHeight:m.clientHeight,
            distance:m.scrollHeight-m.scrollTop-m.clientHeight};
        }''')
        assert initial['commentHeight'] > initial['viewportHeight'], f'Commentary fixture is too short: {initial}'
        assert initial['distance'] <= 1, initial
        await event(page, 'message.updated', info=message(A2, 'assistant', parent=U1)['info'])
        active = tool('current', 'bash', {'command': 'fixture-current-command'}, 'CURRENT_OUTPUT', status='running')
        await event(page, 'message.part.updated', part=message(A2, 'assistant', [active])['parts'][0])
        current = page.locator(activity_segment(A2, 'current'))
        await expect(current).to_have_count(1)
        await expect(page.locator('.activity')).to_have_count(2 if earlier_tool else 1)
        await expect(page.locator('.think')).to_have_count(1)
        await expect(page.locator(THINK)).to_have_count(1)
        await expect(current).to_have_attribute('data-tool-part', 'current')
        await expect(current.locator('details, .card, .tool-event')).to_have_count(0)
        await expect(current).to_contain_text('CURRENT_OUTPUT')
        await expect(current.locator(':scope > summary')).to_contain_text('fixture-current-command')
        await expect(current.locator(':scope > summary')).not_to_contain_text('Activity')
        assert await current.evaluate('e=>e.open'), 'The current tool activity should auto-open'
        if earlier_tool:
            previous = page.locator(activity_segment(A1, 'earlier'))
            await expect(previous).to_have_count(1)
            await expect(previous).to_have_attribute('data-tool-part', 'earlier')
            assert not await previous.evaluate('e=>e.open'), 'New activity must not open the old message activity'
        for delta in ['', '\nCURRENT_OUTPUT_DELTA']:
            if delta:
                active['state']['output'] += delta
                await event(page, 'message.part.updated', part=message(A2, 'assistant', [active])['parts'][0])
                await expect(current).to_contain_text('CURRENT_OUTPUT_DELTA')
            # Do not click or scrollIntoView here: the real SSE render must keep following.
            position = await current.evaluate('''e=>{
              const main=document.querySelector('#main'),comment=document.querySelector('.msg.ai'),thought=document.querySelector('.think');
              const view=main.getBoundingClientRect(),rect=e.getBoundingClientRect(),summary=e.querySelector('summary').getBoundingClientRect();
              return {after:!!(comment.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING),
                sibling:e.parentElement===comment.parentElement&&e.parentElement===thought.parentElement,
                below:rect.top>=comment.getBoundingClientRect().bottom-1,
                visible:summary.top>=view.top-1&&rect.bottom<=view.bottom+1,
                distance:main.scrollHeight-main.scrollTop-main.clientHeight};
            }''')
            assert position['after'] and position['sibling'] and position['below'], f'Activity moved above earlier commentary: {position}'
            assert position['visible'] and position['distance'] <= 1, f'Following scroll lost the current Activity: {position}'
        await assert_separation(page)
        await page.screenshot(path=str(out / ('two-message-activities.png' if earlier_tool else 'activity-after-commentary.png')))


async def activity_state(page, out):
    pending = tool('pending', 'bash', {'command': 'fixture-pending'}, status='pending')
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Wait for the evidence.'), pending], U1)], running=True)
    activity = page.locator('.activity')
    await expect(activity).to_have_count(1)
    assert not await activity.evaluate('e=>e.open'), 'Pending activity must remain collapsed'
    status = activity.locator('.tool-status')
    status_text = await status.get_attribute('aria-label') or await status.inner_text()
    assert 'Completed' not in status_text and re.search(r'Pending|Running', status_text), status_text
    await expect(page.locator(THINK + '[data-running]')).to_have_count(0)
    await expanded(page, THINK, False)
    await activity.locator(':scope > summary').click()
    await activity.locator(':scope > summary').click()
    await open_thought(page)
    await page.locator('.think-head').click()
    await expanded(page, THINK, False)
    running = tool('pending', 'bash', {'command': 'fixture-pending'}, 'PARTIAL_OUTPUT', status='running')
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [running])['parts'][0])
    assert not await activity.evaluate('e=>e.open'), 'Explicitly closed Activity reopened while streaming'
    await expanded(page, THINK, False)
    failed = tool('pending', 'bash', {'command': 'fixture-pending'}, status='error', error='FIXTURE_TOOL_FAILURE')
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [failed])['parts'][0])
    assert not await activity.evaluate('e=>e.open'), 'Failure ignored explicit Activity closure'
    await expect(activity.locator('.tool-error')).to_be_visible()
    await expect(activity.locator('.tool-error')).to_contain_text('FIXTURE_TOOL_FAILURE')
    await expect(activity.locator('.tool-status')).to_have_attribute('aria-label', 'Failed')
    await activity.locator(':scope > summary').click()
    await expect(activity).to_contain_text('FIXTURE_TOOL_FAILURE')
    await rerender(page)
    assert await activity.evaluate('e=>e.open'), 'Explicitly open Activity should survive rerender'
    # Failure stays visible without forcing open a fresh tool disclosure.
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Inspect the failure.'), failed], U1, 9000)])
    assert not await activity.evaluate('e=>e.open'), 'Failed activity must remain collapsed'
    await expect(activity.locator('.tool-error')).to_be_visible()
    await expect(activity.locator('.tool-error')).to_have_text('FIXTURE_TOOL_FAILURE')
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    # Auto-open is not explicit user intent: completing a tool collapses the section.
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Wait for the evidence.'), running], U1)], running=True)
    assert await activity.evaluate('e=>e.open'), 'Actual running tool should auto-open'
    done = tool('pending', 'bash', {'command': 'fixture-pending'}, 'DONE_OUTPUT')
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [done])['parts'][0])
    assert not await activity.evaluate('e=>e.open'), 'Completed Activity should collapse after automatic opening'
    await activity.locator(':scope > summary').click()
    await event(page, 'session.status', sessionID=SID, status={'type': 'idle'})
    await rerender(page)
    assert await activity.evaluate('e=>e.open'), 'Explicitly opened completed Activity should stay open'
    await expect(page.locator('.think[data-running]')).to_have_count(0)


async def streaming_and_stop(page, out):
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Consider the first fact.', end=None)], U1)], running=True)
    await expanded(page, THINK, True)
    await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='r1', field='text', delta='\n\nCheck the second fact.')
    assert await page.locator('.think .rtext').text_content() == 'Consider the first fact.\n\nCheck the second fact.'
    await event(page, 'session.status', sessionID=SID, status={'type': 'retry', 'attempt': 1, 'message': 'Retry fixture'})
    await expect(page.locator('.think[data-running]')).to_have_count(1)
    await event(page, 'session.error', sessionID=SID, error={'name': 'APIError', 'data': {'message': 'FIXTURE_SESSION_FAILURE'}})
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expect(page.locator('.error-card')).to_contain_text('FIXTURE_SESSION_FAILURE')
    for stop_type in ['session.idle', 'abort']:
        await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Keep the partial reasoning.', end=None)], U1)], running=True)
        if stop_type == 'abort':
            info = message(A1, 'assistant', parent=U1)['info']
            info['error'] = {'name': 'MessageAbortedError', 'data': {'message': 'Request aborted'}}
            await event(page, 'message.updated', info=info)
        else:
            await event(page, 'session.idle', sessionID=SID)
        assert not await page.evaluate('S.running'), f'{stop_type} did not stop the turn'
        await expect(page.locator('.think[data-running]')).to_have_count(0)
        await open_thought(page)
        await expect(page.locator('.think .rtext')).to_have_text('Keep the partial reasoning.')
    # Missing completed timestamps in an old turn must not produce a stale spinner.
    await seed(page, [user(), message(A1, 'assistant', [reason('old', 'Old partial reasoning.')], U1),
                     user(U2), message(A3, 'assistant', [reason('new', 'Latest reasoning.', end=None)], U2)], running=True)
    await expect(page.locator('.think[data-running]')).to_have_count(1)
    await expect(page.locator(thought(A3, 'new') + '[data-running]')).to_have_count(1)
    await expect(page.locator(thought(A1, 'old') + '[data-running]')).to_have_count(0)
    await expect(page.locator(thought(A1, 'old') + ' .think-label')).to_have_text('Reasoning')
    # A streaming text part can carry an unfinished <think> tag.
    await seed(page, [user(), message(A1, 'assistant', [text('tag_stream', '<think>Consider the streamed tag.')], U1)], running=True)
    await expect(page.locator('.think .rtext')).to_have_text('Consider the streamed tag.')
    await expect(page.locator('.msg.ai')).to_have_count(0)
    await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='tag_stream',
                field='text', delta='\n\nVerify the streamed tag.</think>Streamed final answer.')
    await expect(page.locator('.think .rtext')).to_have_text('Consider the streamed tag.\n\nVerify the streamed tag.')
    await expect(page.locator('.msg.ai')).to_have_text('Streamed final answer.')
    assert await page.evaluate('S.running'), 'The reasoning must finish before session idle'
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expanded(page, thought(A1, 'tag_stream'), False)
    await event(page, 'session.status', sessionID=SID, status={'type': 'idle'})
    await expect(page.locator('.think[data-running]')).to_have_count(0)


async def narrow_layout(page, out):
    await page.set_viewport_size({'width': 320, 'height': 740})
    await seed(page, [user(), message(A1, 'assistant', [reason('long', LONG_REASON),
        tool('cmd', 'bash', {'command': 'fixture-command'}, '\n'.join('LONG_OUTPUT_' * 7 for _ in range(20)))], U1, 20000)])
    await open_thought(page)
    rects = await page.locator('.think').evaluate('''e=>{
      const box=n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}};
      return {head:box(e.querySelector('.think-head')),body:box(e.querySelector('.think-body')),
        window:box(e.querySelector('.feed-text-window')),width:innerWidth,scroll:document.documentElement.scrollWidth};
    }''')
    assert rects['body']['top'] >= rects['head']['bottom'] - 1, f'Reasoning overlaps its heading: {rects}'
    assert rects['scroll'] <= rects['width'], f'Page overflows horizontally: {rects}'
    assert rects['window']['right'] <= rects['width'] and rects['window']['bottom'] - rects['window']['top'] <= 133, rects
    await page.screenshot(path=str(out / 'narrow-thought.png'))
    await page.locator('.think-head').focus()
    await page.keyboard.press('Enter')
    await expanded(page, thought(A1, 'long'), False)
    await rerender(page)
    await expanded(page, thought(A1, 'long'), False)
    await page.locator('.think-head').focus()
    await page.keyboard.press('Space')
    await expanded(page, thought(A1, 'long'), True)
    await rerender(page)
    await expanded(page, thought(A1, 'long'), True)
    await page.locator('.activity > summary').click()
    await expect(page.locator('.activity [data-feed-expand]')).to_have_count(1)
    await page.locator('.activity [data-feed-expand]').click()
    assert await page.locator('.activity .feed-text-window').evaluate('e=>e.getBoundingClientRect().height>132')
    await page.locator('.think').scroll_into_view_if_needed()
    await page.screenshot(path=str(out / 'narrow-activity.png'))


async def sync_snapshot(page, messages, status='busy'):
    """Run production syncSession against controlled HTTP snapshots, not a mocked merge."""
    responses = {f'/session/{SID}/message': messages,
                 '/session/status': {SID: {'type': status}}, '/permission': [], '/question': []}
    received = []
    pattern = re.compile(re.escape(ORIGIN) + r'/(?:session/[^/?]+/message|session/status|permission|question)(?:\?.*)?$')

    async def route_snapshot(route):
        path = urlsplit(route.request.url).path
        assert route.request.method == 'GET' and path in responses, route.request.url
        received.append(path)
        await route.fulfill(json=responses[path])

    await page.route(pattern, route_snapshot)
    try:
        await page.evaluate('''async()=>{
          S.sending=false;
          try{await syncSession()}finally{S.sending=true}
        }''')
        assert set(received) == set(responses), f'syncSession did not fetch its snapshot: {received}'
    finally:
        await page.unroute(pattern, route_snapshot)


async def snapshot_preserves_live_segments(page, out):
    parts = [reason('r1', 'First complete reasoning.', 1000, 5000),
             tool('t1', 'read', {'filePath': 'first.txt'}, 'FIRST_OUTPUT'),
             text('commentary', 'Visible streamed commentary.'),
             reason('r2', 'Second complete reasoning.', 7000, 9000),
             tool('t2', 'read', {'filePath': 'second.txt'}, 'OLD_OUTPUT', status='running'),
             reason('r3', 'Current streamed reasoning.', 10000, None)]
    live = [user(), message(A1, 'assistant', parts, U1)]
    expected = [f'{A1}_r1_think', f'{A1}_t1_activity', 'text:Visible streamed commentary.',
                f'{A1}_r2_think', f'{A1}_t2_activity', f'{A1}_r3_think']
    await seed(page, live, running=True)
    current = thought(A1, 'r3')
    await open_thought(page, THINK)
    await page.locator(current + ' > .think-head').click()
    await page.locator(activity_segment(A1, 't1') + ' > summary').click()
    await page.locator(activity_segment(A1, 't2') + ' > summary').click()
    controls = await page.locator('.think-head').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))')
    for prefix_length in [0, 12]:
        snapshot = json.loads(json.dumps(live))
        for part in snapshot[1]['parts']:
            if part['type'] in ['text', 'reasoning']:
                part['text'] = part['text'][:prefix_length]
                part['metadata'] = {'snapshot': prefix_length}
            elif part['id'] == 't2':
                part['state']['output'] = 'SNAPSHOT_OUTPUT'
        await sync_snapshot(page, snapshot)
        await assert_sequence(page, expected)
        await expanded(page, THINK, True)
        await expanded(page, thought(A1, 'r2'), False)
        await expanded(page, current, False)
        assert await page.locator(activity_segment(A1, 't1')).evaluate('e=>e.open')
        assert not await page.locator(activity_segment(A1, 't2')).evaluate('e=>e.open')
        await expect(page.locator(current + '[data-running]')).to_have_count(1)
        await expect(page.locator('.think[data-running]')).to_have_count(1)
        await expect(page.locator(THINK + ' .think-duration')).to_have_text('4s')
        await expect(page.locator(thought(A1, 'r2') + ' .think-duration')).to_have_text('2s')
        assert await page.locator('.think-head').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))') == controls
        for pid in ['r1', 'commentary', 'r2', 'r3']:
            actual = await page.evaluate('pid=>S.msgs.get(' + json.dumps(A1) + ').parts.get(pid)', pid)
            original = next(p for p in parts if p['id'] == pid)
            assert actual['text'] == original['text'], actual
            assert actual['metadata'] == {'snapshot': prefix_length}, 'Only text should be preserved, not old metadata'
        await expect(page.locator('.tool-event[data-tool-part="t2"]')).to_contain_text('SNAPSHOT_OUTPUT')
        assert 'OLD_OUTPUT' not in await page.locator('.tool-event[data-tool-part="t2"]').text_content()
    await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='r3', field='text', delta='\n\nDelta after polling.')
    await expect(page.locator(current + ' .rtext')).to_have_text('Current streamed reasoning.\n\nDelta after polling.')
    await expect(page.locator(THINK + ' .rtext')).to_have_text('First complete reasoning.')
    await expanded(page, current, False)
    # New snapshot timing must stop the spinner even when its text still lags SSE.
    snapshot[1]['parts'][-1]['time']['end'] = 13000
    await sync_snapshot(page, snapshot)
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expect(page.locator(current + ' .think-duration')).to_have_text('3s')
    await expanded(page, THINK, True)
    await expanded(page, current, False)
    await assert_sequence(page, expected)
    await assert_separation(page)
    await page.screenshot(path=str(out / 'snapshot-boundaries.png'))


async def snapshot_authoritative_changes(page, out):
    live_text = 'Streaming source continues.'
    for kind in ['text', 'reasoning']:
        for label, server_text, completed in [
            ('changed', 'Server correction.', None),
            ('equal', live_text, None),
            ('longer', live_text + ' Server continuation.', None),
            ('completed_prefix', 'Streaming', 20000),
            ('completed_empty', '', 20000),
        ]:
            original = text('part', live_text) if kind == 'text' else reason('part', live_text, end=None)
            await seed(page, [user(), message(A1, 'assistant', [original], U1)], running=True)
            server_part = dict(original, text=server_text)
            await sync_snapshot(page, [user(), message(A1, 'assistant', [server_part], U1, completed)])
            actual = await page.evaluate('S.msgs.get(' + json.dumps(A1) + ').parts.get("part").text')
            assert actual == server_text, f'{kind}/{label}: server snapshot lost authority: {actual!r}'
            if kind == 'text':
                await expect(page.locator('.msg.ai')).to_have_count(1 if server_text else 0)
                if server_text:
                    await expect(page.locator('.msg.ai')).to_have_attribute('data-raw', server_text)
            else:
                await expect(page.locator('.think')).to_have_count(1 if server_text else 0)
                if server_text:
                    await expect(page.locator('.think .rtext')).to_have_text(server_text)
                if completed:
                    await expect(page.locator('.think[data-running]')).to_have_count(0)
    # A prefix from a different type is not the same live part.
    for original, replacement, expected in [
        (reason('part', 'Long reasoning source.', end=None), text('part', 'Long'), ['text:Long']),
        (text('part', 'Long visible source.'), reason('part', 'Long', end=None), [f'{A1}_part_think']),
    ]:
        await seed(page, [user(), message(A1, 'assistant', [original], U1)], running=True)
        await sync_snapshot(page, [user(), message(A1, 'assistant', [replacement], U1)])
        await assert_sequence(page, expected)
        assert await page.evaluate('S.msgs.get(' + json.dumps(A1) + ').parts.get("part").text') == 'Long'
    # Missing parts/messages are deletions, not permission to resurrect live content.
    await seed(page, [user(), message(A1, 'assistant', [reason('removed', 'Remove me.', end=None),
                      text('removed_text', 'Remove this answer too.'),
                      tool('keep', 'read', {'filePath': 'README.md'}, 'KEEP_OUTPUT')], U1)], running=True)
    await sync_snapshot(page, [user(), message(A1, 'assistant', [tool('keep', 'read', {'filePath': 'README.md'}, 'KEEP_OUTPUT')], U1)])
    await assert_sequence(page, [f'{A1}_keep_activity'])
    assert await page.evaluate('[...S.msgs.get(' + json.dumps(A1) + ').parts.keys()]') == ['keep']
    await sync_snapshot(page, [user()])
    await expect(page.locator('.think, .activity, .msg.ai')).to_have_count(0)
    assert not await page.evaluate('S.msgs.has(' + json.dumps(A1) + ')')


async def adjacent_tag_streaming(page, out):
    prefix = '<think>A</think>'
    first = thought(A1, 'adjacent')
    second = thought(A1, f'adjacent_at_{len(prefix)}')
    expected = [f'{A1}_adjacent_think', f'{A1}_adjacent_at_{len(prefix)}_think']
    for ending in ['close', 'abort']:
        await seed(page, [user(), message(A1, 'assistant', [text('adjacent', '<think>A')], U1)], running=True)
        await expanded(page, first, True)
        await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='adjacent',
                    field='text', delta='</think><think>B')
        await assert_sequence(page, expected)
        await expect(page.locator('.think .rtext')).to_have_text(['A', 'B'])
        await expect(page.locator('.msg.ai')).to_have_count(0)
        await expanded(page, first, False)
        await expanded(page, second, True)
        await expect(page.locator(first + '[data-running]')).to_have_count(0)
        await expect(page.locator(second + '[data-running]')).to_have_count(1)
        await open_thought(page, first)
        controls = await page.locator('.think-head').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))')
        # A lagging text snapshot must not drop or merge the second tagged segment.
        await sync_snapshot(page, [user(), message(A1, 'assistant', [text('adjacent', prefix)], U1)])
        await assert_sequence(page, expected)
        await expanded(page, first, True)
        await expanded(page, second, True)
        if ending == 'close':
            await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='adjacent', field='text', delta='</think>Answer.')
            expected_after = expected + ['text:Answer.']
            assert await page.evaluate('S.running'), 'Closing the tag must end reasoning before session idle'
        else:
            aborted = message(A1, 'assistant', parent=U1)['info']
            aborted['error'] = {'name': 'MessageAbortedError', 'data': {'message': 'Request aborted'}}
            await event(page, 'message.updated', info=aborted)
            expected_after = expected
            await expect(page.locator('.error-card')).to_contain_text('Stopped')
        await assert_sequence(page, expected_after)
        await expanded(page, first, True)
        await expanded(page, second, False)
        await expect(page.locator('.think[data-running]')).to_have_count(0)
        assert await page.locator('.think-head').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))') == controls
        await rerender(page)
        await assert_sequence(page, expected_after)
        await expanded(page, first, True)
        await expanded(page, second, False)
        saved = await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))')
        await page.reload()
        await seed(page, saved)
        await assert_sequence(page, expected_after)
        await expect(page.locator('.think .rtext')).to_have_text(['A', 'B'])
        await expanded(page, first, False)
        await expanded(page, second, False)
        await expect(page.locator('.think[data-running]')).to_have_count(0)
        assert await page.locator('.think-head').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))') == controls
    # Visible text, unlike an earlier closed think tag, prevents treating a later
    # unfinished literal tag as leading provider reasoning.
    await seed(page, [user(), message(A1, 'assistant', [
        text('adjacent', prefix + 'Visible prefix <think>B')], U1)], running=True)
    await assert_sequence(page, [f'{A1}_adjacent_think', 'text:Visible prefix <think>B'])
    await expect(page.locator('.think[data-running]')).to_have_count(0)
    await expect(page.locator('.think .rtext')).to_have_text('A')
    await page.screenshot(path=str(out / 'adjacent-tags.png'))


CASES = [native_timeline, grouped_turns, same_message_segments, text_and_tag_boundaries,
         snapshot_preserves_live_segments, snapshot_authoritative_changes, adjacent_tag_streaming,
         compact_previews, current_segment_streaming, busy_boundaries, completed_reasoning_busy,
         tagged_and_raw, reasoning_identifiers_and_raw,
         literal_think_tags, interrupted_leading_tag, no_fake_thought, activity_cards,
         ssh_independent_stream_fallback, activity_after_commentary, activity_state,
         streaming_and_stop, narrow_layout]


async def main(source=None, output_dir=None, browser_executable=None, cases=None):
    expect.set_options(timeout=2500)
    out = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix='reasoning-activity-proof-'))
    out.mkdir(parents=True, exist_ok=True)
    print(f'Evidence: {out}', flush=True)
    checks = []
    async with async_playwright() as pw:
        browser = await launch_browser(pw, browser_executable)
        try:
            for theme in ['dark', 'light']:
                for case in CASES:
                    if cases and case.__name__ not in cases:
                        continue
                    context, page, errors, requests = await boot_page(browser, theme, source)
                    case_out = out / theme / case.__name__
                    case_out.mkdir(parents=True, exist_ok=True)
                    failure = None
                    try:
                        await case(page, case_out)
                        assert not errors, f'JavaScript errors: {errors}'
                        assert not requests, f'Unexpected network requests (blocked): {requests}'
                    except Exception as error:
                        failure = f'{type(error).__name__}: {error}'
                        await page.screenshot(path=str(case_out / 'failure.png'))
                    finally:
                        await context.close()
                    result = {'name': f'{theme}/{case.__name__}', 'ok': failure is None,
                              'failure': failure, 'pageErrors': errors, 'unexpectedRequests': requests}
                    checks.append(result)
                    print(('PASS ' if result['ok'] else 'FAIL ') + result['name'] +
                          (f'\n{failure}' if failure else ''), flush=True)
        finally:
            await browser.close()
    (out / 'report.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    failed = [c['name'] for c in checks if not c['ok']]
    print(f'{len(checks) - len(failed)}/{len(checks)} passed; report: {out / "report.json"}', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, default=HTML, help='Asset snapshot to test (defaults to actual app HTML)')
    parser.add_argument('--output', type=Path, help='Evidence directory (defaults to a fresh system temp directory)')
    parser.add_argument('--browser-executable', type=Path, help='Existing Chrome/Chromium executable (or PLAYWRIGHT_BROWSER_EXECUTABLE)')
    parser.add_argument('--case', action='append', choices=[case.__name__ for case in CASES], help='Run only selected cases in both themes')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.html.read_text(encoding='utf-8'), args.output, args.browser_executable, args.case)))
