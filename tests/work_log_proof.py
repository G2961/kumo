"""Offline first-stage Work summary / native-dialog contract against the real asset.

No model, native bridge, build, or installation. Reuses the existing boot/seed/SSE
helpers without mocking renderers. Run with the project's Playwright Python and
PLAYWRIGHT_BROWSER_EXECUTABLE set to an installed Chrome. Evidence is temporary.
All scenarios run in light/dark at 320/412 px by default; --theme/--width can
select a smaller matrix. Each run snapshots the asset for consistent evidence.
The real-provider companion is reasoning_effort_proof.py --sequence-only.
"""
import argparse
import asyncio
import hashlib
import json
import re
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from reasoning_activity_proof import (
    A1,
    A2,
    A3,
    HTML,
    PATCH,
    SID,
    U1,
    U2,
    activity_segment,
    boot_page,
    event,
    expanded,
    launch_browser,
    message,
    open_thought,
    reason,
    seed,
    text,
    thought,
    tool,
    user,
)

DIALOG = '#workLogDialog'
BODY = '#workLogBody'
SUMMARY = '#main button.work-summary[data-work-open]'


def work_id(mid, pid):
    return f'work_{mid}_{pid}'


def summary(wid):
    return SUMMARY + '[data-work-open=' + json.dumps(wid) + ']'


async def settle(page):
    await page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')


async def assert_main_clean(page):
    # Hidden inline details are not sufficient: the main feed must not own them.
    await expect(page.locator('#main .think, #main .activity, #main .tool-output, '
                              '#main .rtext, #main .change-preview, #main .work-log-entry')).to_have_count(0)


async def main_sequence(page):
    return await page.locator('#main > .msg.user, #main > .msg.ai, ' + SUMMARY).evaluate_all('''els=>els.map(e=>
      e.matches('.work-summary')?e.dataset.workOpen:
      e.matches('.msg.user')?'user:'+e.dataset.mid:'text:'+e.dataset.raw)''')


async def assert_groups(page, groups, timeline=None):
    """groups maps stable first-event IDs to visible detail-entry counts, in order."""
    await expect(page.locator(SUMMARY)).to_have_count(len(groups))
    await settle(page)  # SSE state may advance one frame before the main renderer.
    actual = await page.locator(SUMMARY).evaluate_all('els=>els.map(e=>e.dataset.workOpen)')
    assert actual == list(groups), f'Work group chronology/keys: {actual} != {list(groups)}'
    registry = await page.evaluate('''()=>{
      if(!(S.workBlocks instanceof Map))throw Error('S.workBlocks must be a Map');
      return [...S.workBlocks].map(([id,b])=>({id,count:Array.isArray(b.events)?b.events.length:null}));
    }''')
    assert registry == [{'id': wid, 'count': count} for wid, count in groups.items()], registry
    if timeline is not None:
        actual = await main_sequence(page)
        assert actual == timeline, f'Public text/user/work boundaries changed: {actual} != {timeline}'
    await assert_main_clean(page)


async def open_work(page, wid):
    await page.locator(summary(wid)).click()
    await expect(page.locator(DIALOG)).to_be_visible()
    assert await page.locator(DIALOG).evaluate('e=>e instanceof HTMLDialogElement && e.open && e.matches(":modal")'), \
        'Work details must use showModal(), not merely a visible dialog-shaped div'
    await expect(page.locator(BODY)).to_be_visible()
    await assert_main_clean(page)


async def close_work(page):
    await page.locator(DIALOG + ' [data-work-close]').click()
    await expect(page.locator(DIALOG)).not_to_be_visible()


async def assert_entries(page, expected):
    await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(len(expected))
    actual = await page.locator(BODY + ' .work-log-entry').evaluate_all('''els=>els.map(e=>{
      const rows=e.querySelectorAll('.think,.activity');
      if(rows.length!==1)throw Error('Each entry must contain one existing reasoning/tool row');
      return rows[0].dataset.part;
    })''')
    assert actual == expected, f'Detail chronology changed: {actual} != {expected}'
    await expect(page.locator(BODY + ' .think .activity, ' + BODY + ' .think .tool-output, '
                              + BODY + ' .think .change-preview, ' + BODY + ' .think .tool-command')).to_have_count(0)
    controls = await page.locator(BODY + ' [aria-controls]').evaluate_all('els=>els.map(e=>e.getAttribute("aria-controls"))')
    assert len(controls) == len(set(controls)), f'Duplicate controlled body IDs: {controls}'
    assert await page.locator(BODY + ' [aria-controls]').evaluate_all('els=>els.every(e=>!!document.getElementById(e.getAttribute("aria-controls")))')


async def chronological_groups_reload(page, out):
    first, second = work_id(A1, 'r1'), work_id(A2, 'after')
    saved = [message(A2, 'assistant', [reason('r2', 'Second message reasoning.'),
                tool('read2', 'read', {'filePath': 'b.txt'}, 'SECOND_READ'),
                text('comment', 'Ordinary assistant commentary stays exactly here.'),
                tool('after', 'bash', {'command': 'after-comment'}, 'AFTER_COMMENT'),
                reason('r3', 'Reasoning after commentary.'), text('answer', 'Final answer.')], U1, 15000),
             user(), message(A1, 'assistant', [reason('r1', 'First reasoning.'),
                reason('r1b', 'Contiguous reasoning in the same segment.'),
                tool('read1', 'read', {'filePath': 'a.txt'}, 'FIRST_READ'),
                tool('cmd', 'bash', {'command': 'fixture-command'}, 'COMMAND_OUTPUT')], U1, 8000)]
    groups = {first: 5, second: 2}
    timeline = ['user:' + U1, first, 'text:Ordinary assistant commentary stays exactly here.', second, 'text:Final answer.']
    details = {
        first: [f'{A1}_r1_think', f'{A1}_read1_activity', f'{A1}_cmd_activity', f'{A2}_r2_think', f'{A2}_read2_activity'],
        second: [f'{A2}_after_activity', f'{A2}_r3_think'],
    }
    for phase in ['initial', 'reload']:
        if phase == 'reload':
            await page.reload()  # No DOM or work registry survives this navigation.
        await seed(page, saved)
        await assert_groups(page, groups, timeline)
        for wid, keys in details.items():
            await open_work(page, wid)
            await assert_entries(page, keys)
            if wid == first:
                await open_thought(page, BODY + ' ' + thought(A1, 'r1'))
                await expect(page.locator(BODY + ' ' + thought(A1, 'r1') + ' .rtext')).to_have_text(
                    'First reasoning.\n\nContiguous reasoning in the same segment.')
            await close_work(page)
    await page.screenshot(path=str(out / 'chronological-main.png'))


async def hard_boundaries(page, out):
    # Every case starts from fresh model data; no empty/whitespace pseudo-event.
    cases = [
        ('user', [user(), message(A1, 'assistant', [reason('r1', 'Before user.')], U1, 8000),
                  user(U2), message(A3, 'assistant', [reason('r3', 'After user.')], U1, 9000)],
         {work_id(A1, 'r1'): 1, work_id(A3, 'r3'): 1}),
        ('parent', [user(), message(A1, 'assistant', [reason('r1', 'First parent.')], U1, 8000),
                    message(A2, 'assistant', [reason('r2', 'Different parent.')], U2, 9000)],
         {work_id(A1, 'r1'): 1, work_id(A2, 'r2'): 1}),
        ('public_same_message', [user(), message(A1, 'assistant', [reason('r1', 'Before text.'),
                    text('comment', 'Public boundary.'), reason('r2', 'After text.')], U1, 9000)],
         {work_id(A1, 'r1'): 1, work_id(A1, 'r2'): 1}),
    ]
    failed = message(A1, 'assistant', [reason('r1', 'Before error.')], U1, 8000)
    failed['info']['error'] = {'name': 'APIError', 'data': {'message': 'BOUNDARY_ERROR', 'statusCode': 503}}
    cases.append(('error', [user(), failed,
                  message(A2, 'assistant', [reason('r2', 'After error.')], U1, 9000)],
                  {work_id(A1, 'r1'): 1, work_id(A2, 'r2'): 1}))
    for name, saved, groups in cases:
        await seed(page, saved)
        await assert_groups(page, groups)
        if name == 'error':
            await expect(page.locator('#main .error-card')).to_contain_text('BOUNDARY_ERROR')
            order = await page.locator('#main .work-summary, #main .error-card').evaluate_all(
                'els=>els.map(e=>e.dataset.workOpen||"error")')
            assert order == [work_id(A1, 'r1'), 'error', work_id(A2, 'r2')], order


async def literal_tags_and_no_chatter(page, out):
    literals = [
        'Direct answer without work.',
        'Use `<think>literal inline</think>` as a token.',
        'Example:\n\n```xml\n<think>literal fenced</think>\n```\n\nKeep it.',
        'Example:\n\n```xml\n<think>unfinished fence',
        'The opening token is <think> and the rest is ordinary answer text.',
    ]
    for raw in literals:
        for running in [True, False]:
            await seed(page, [user(), message(A1, 'assistant', [reason('empty', ' \n '), text('literal', raw)],
                                              U1, None if running else 9000)], running=running)
            await assert_groups(page, {}, ['user:' + U1, 'text:' + raw])
            await expect(page.locator('#main .msg.ai')).to_have_attribute('data-raw', raw)
            if '<think>' in raw:
                await expect(page.locator('#main .msg.ai')).to_contain_text('<think>')
    answer = 'Keep `<think>inline example</think>` unchanged.'
    await seed(page, [user(), message(A1, 'assistant', [text('tagged', '<think>Actual tagged reasoning.</think>' + answer)], U1, 9000)])
    wid = work_id(A1, 'tagged')
    await assert_groups(page, {wid: 1}, ['user:' + U1, wid, 'text:' + answer])
    await open_work(page, wid)
    await expect(page.locator(BODY + ' .rtext')).to_have_text('Actual tagged reasoning.')
    await close_work(page)
    # A tool-only turn must not manufacture assistant comments or reasoning.
    await seed(page, [user(), message(A1, 'assistant', [tool('only', 'read', {'filePath': 'a.txt'}, 'A')], U1, 9000)])
    wid = work_id(A1, 'only')
    await assert_groups(page, {wid: 1}, ['user:' + U1, wid])
    await open_work(page, wid)
    await expect(page.locator(BODY + ' .think')).to_have_count(0)
    await expect(page.locator(BODY + ' .activity')).to_have_count(1)


def read_counts(label):
    # Allow "Read 2 files", "2 files read", "2 files", and Russian equivalents;
    # intentionally do not pin a sentence, punctuation, or generic Work label.
    counts = re.findall(r'\b(\d+)\s+(?:files?\b|файл\w*)', label, re.IGNORECASE)
    if not counts:
        counts = re.findall(r'(?:\bread|прочитан\w*)\s*[:·]?\s*(\d+)\b', label, re.IGNORECASE)
    return [int(n) for n in counts]


async def honest_read_counts(page, out):
    local = tool('local', 'read', {'filePath': 'src/shared.txt'}, 'LOCAL_FILE')
    duplicate = tool('duplicate', 'read', {'filePath': 'src/shared.txt'}, 'LOCAL_FILE_AGAIN')
    remote = tool('remote', 'mobile_ssh_ssh_read_file', {'connection_id': 'conn_fixture', 'path': 'src/shared.txt'},
                  json.dumps({'content': 'REMOTE_FILE'}))
    bad = tool('bad', 'read', {'filePath': 'src/missing.txt'}, status='error', error='ENOENT_FIXTURE')
    waiting = tool('waiting', 'read', {'filePath': 'src/pending.txt'}, status='running')
    search = tool('search', 'grep', {'pattern': 'fixture'}, 'MATCH')
    wid = work_id(A1, 'local')
    for parts, expected in [([local, duplicate], 1), ([local, duplicate, remote], 2),
                            ([local, duplicate, remote, bad, waiting, search], 2)]:
        await seed(page, [user(), message(A1, 'assistant', parts, U1, 9000)])
        await assert_groups(page, {wid: len(parts)})
        label = await page.locator(summary(wid)).inner_text()
        assert read_counts(label) == [expected], f'Successful unique file count should be {expected}: {label!r}'
    # A failed-only read must never be advertised as one successfully read file.
    await seed(page, [user(), message(A1, 'assistant', [bad], U1, 9000)])
    label = await page.locator(summary(work_id(A1, 'bad'))).inner_text()
    assert not any(read_counts(label)), f'Failed read advertised as successful: {label}'
    assert re.search(r'fail|error|ошиб|неуда', label, re.IGNORECASE), f'Hidden failure has no summary signal: {label}'


async def details_and_saved_expansion(page, out):
    raw = 'RAW_REASON_START\n' + '\n'.join(f'Observation {i} with literal <tag> & content.' for i in range(35)) + '\nRAW_REASON_END'
    output = 'OUTPUT_START\n' + '\n'.join(f'line {i} <xml> & stdout' for i in range(55)) + '\nOUTPUT_END'
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', raw),
        tool('read', 'read', {'filePath': 'src/input.txt'}, output),
        tool('edit', 'edit', {'filePath': 'src/login.js'}, metadata={'diff': PATCH}),
        text('answer', 'Only this ordinary answer is public.')], U1, 10000)])
    wid = work_id(A1, 'r1')
    await assert_groups(page, {wid: 3})
    await page.evaluate('window.workMainNode=document.querySelector("#main .msg.user")')
    before = await main_sequence(page)
    await open_work(page, wid)
    await assert_entries(page, [f'{A1}_r1_think', f'{A1}_read_activity', f'{A1}_edit_activity'])
    await open_thought(page, BODY + ' ' + thought(A1, 'r1'))
    read = page.locator(BODY + ' ' + activity_segment(A1, 'read'))
    edit = page.locator(BODY + ' ' + activity_segment(A1, 'edit'))
    for row in [read, edit]:
        if not await row.evaluate('e=>e.open'):
            await row.locator(':scope > summary').click()
    assert await page.locator(BODY + ' .rtext').text_content() == raw
    assert await read.locator('.tool-output').text_content() == output
    await expect(edit.locator('.diff-line.a .diff-text')).to_have_text('fixedLogin')
    await expect(edit.locator('.diff-line.d .diff-text')).to_have_text('oldLogin')
    for block in [page.locator(BODY + ' ' + thought(A1, 'r1')), read]:
        button = block.locator('[data-feed-expand]')
        await expect(button).to_be_visible()
        await button.click()
        await expect(button).to_have_attribute('aria-expanded', 'true')
    await page.evaluate('refreshWorkSheet()')
    assert await page.evaluate('window.workMainNode.isConnected'), 'Detail refresh rebuilt the main renderer'
    assert await main_sequence(page) == before, 'Opening/refreshing details changed the public feed'
    assert await read.evaluate('e=>e.open') and await edit.evaluate('e=>e.open')
    await expanded(page, BODY + ' ' + thought(A1, 'r1'), True)
    await expect(page.locator(BODY + ' [data-feed-expand][aria-expanded="true"]')).to_have_count(2)
    await page.evaluate('renderMsgs()')
    await settle(page)
    await expect(page.locator(BODY + ' [data-feed-expand][aria-expanded="true"]')).to_have_count(2)
    await close_work(page)
    await open_work(page, wid)
    assert await read.evaluate('e=>e.open'), 'Explicit activity expansion lost on reopen'
    await expect(read.locator('[data-feed-expand]')).to_have_attribute('aria-expanded', 'true')
    await page.screenshot(path=str(out / 'full-details.png'))


async def pure_presentation(page, out):
    saved = [user(), message(A1, 'assistant', [reason('r1', 'RAW_FIELD_SENTINEL'),
        tool('read', 'read', {'filePath': 'input.txt'}, 'RAW_OUTPUT_SENTINEL'), text('answer', 'Exact public answer.')], U1, 9000)]
    await seed(page, saved)
    source = await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))')
    assert source == saved, 'Initial grouping rewrote source message/part fields'
    await page.evaluate('''()=>{
      S.model={p:'fixture-provider',m:'fixture-model'};S.agent='plan';
      S.efforts={'fixture-provider/fixture-model':'high'};S.webSearch=true;
      window.workWrites=[];
      for(const method of ['setItem','removeItem','clear']){
        const original=Storage.prototype[method];
        Storage.prototype[method]=function(...args){workWrites.push(['storage',method,...args]);return original.apply(this,args)};
      }
      const open=indexedDB.open.bind(indexedDB);
      indexedDB.open=(...args)=>{workWrites.push(['indexedDB',...args]);return open(...args)};
      window.CodexNative={request:value=>{workWrites.push(['native',value]);throw Error('Unexpected native call from Work presentation')}};
    }''')
    snapshot = '''()=>JSON.stringify({messages:[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]})),
      model:S.model,agent:S.agent,efforts:S.efforts,webSearch:S.webSearch,provs:S.provs,dir:S.dir,
      connectionID:S.connectionID,connections:S.connections,remoteRuns:[...S.remoteRuns],
      attachments:S.attachments,localPolicies:S.localPolicies,perms:[...S.perms],questions:[...S.questions],
      local:{...localStorage},session:{...sessionStorage}})'''
    before = await page.evaluate(snapshot)
    requests = []
    listener = lambda request: requests.append(request.method + ' ' + request.url)
    page.on('request', listener)
    try:
        await page.evaluate('renderMsgs()')
        await open_work(page, work_id(A1, 'r1'))
        await open_thought(page, BODY + ' ' + thought(A1, 'r1'))
        await page.locator(BODY + ' .activity > summary').click()
        await page.evaluate('refreshWorkSheet()')
        await close_work(page)
        await page.evaluate('id=>openWorkSheet(id)', work_id(A1, 'r1'))
        await page.evaluate('closeWorkSheet()')
        await settle(page)
        assert await page.evaluate(snapshot) == before, 'Presentation mutated source/config/prompt fields or storage'
        assert await page.evaluate('window.workWrites') == [], 'Work presentation performed persistent/native writes'
        assert not requests, f'Work presentation initiated network requests: {requests}'
    finally:
        page.remove_listener('request', listener)


async def accessible_modal_and_scroll(page, out):
    prose = '\n\n'.join('Public context paragraph ' + str(i) + '. ' + 'Keep this context. ' * 8 for i in range(24))
    wid = work_id(A1, 'r1')
    await seed(page, [user(), message(A1, 'assistant', [text('before', prose), reason('r1', 'Detail reasoning.'),
        text('after', prose)], U1, 9000)])
    await page.fill('#inp', 'Unsent draft: <think>literal</think>')
    await page.locator(summary(wid)).scroll_into_view_if_needed()
    scroll = await page.locator('#main').evaluate('e=>e.scrollTop')
    assert scroll > 0, 'Scroll fixture must actually be scrolled'
    for method in ['button', 'escape', 'backdrop']:
        await open_work(page, wid)
        dialog = page.locator(DIALOG)
        await expect(dialog).to_have_accessible_name(re.compile(r'\S'))
        await expect(dialog.locator('[data-work-close]')).to_have_accessible_name(re.compile(r'\S'))
        assert await dialog.evaluate('e=>e.contains(document.activeElement)'), 'Focus did not enter the modal'
        for _ in range(8):
            await page.keyboard.press('Tab')
            assert await dialog.evaluate('e=>e.contains(document.activeElement)||document.activeElement===document.body'), 'Tab focused a background control'
        for _ in range(3):
            await page.keyboard.press('Shift+Tab')
            assert await dialog.evaluate('e=>e.contains(document.activeElement)||document.activeElement===document.body'), 'Reverse Tab focused a background control'
        expand = dialog.locator('[data-work-expand]')
        await expect(expand).to_have_accessible_name(re.compile(r'\S'))
        box = await dialog.bounding_box()
        await expand.click()
        await settle(page)
        larger = await dialog.bounding_box()
        assert larger['height'] > box['height'] + 1, 'Expand control did not enlarge the sheet'
        await expand.click()
        if method == 'button':
            await close_work(page)
        elif method == 'escape':
            await page.keyboard.press('Escape')
        else:
            await settle(page)
            bounds = await dialog.bounding_box()
            viewport = page.viewport_size
            points = [(2, 2), (viewport['width'] - 2, 2), (2, viewport['height'] - 2)]
            outside = next(((x, y) for x, y in points if not (
                bounds['x'] <= x <= bounds['x'] + bounds['width'] and bounds['y'] <= y <= bounds['y'] + bounds['height'])), None)
            assert outside, 'Collapsed work sheet needs a clickable backdrop'
            await page.mouse.click(*outside)
        await expect(dialog).not_to_be_visible()
        await expect(page.locator(summary(wid))).to_be_focused()
        assert abs(await page.locator('#main').evaluate('e=>e.scrollTop') - scroll) <= 2, 'Main scroll jumped on modal close'
        await expect(page.locator('#inp')).to_have_value('Unsent draft: <think>literal</think>')


async def streaming_scroll_and_new_events(page, out):
    parts = []
    for i in range(16):
        parts.extend([reason(f'r{i}', f'Completed reasoning {i}.'),
                      tool(f't{i}', 'read', {'filePath': f'file-{i}.txt'}, f'OUTPUT_{i}')])
    parts.append(reason('live', 'LIVE_REASONING', 10000, None))
    await seed(page, [user(), message(A1, 'assistant', parts, U1)], running=True)
    wid = work_id(A1, 'r0')
    await assert_groups(page, {wid: 33})
    await open_work(page, wid)
    early = BODY + ' ' + thought(A1, 'r0')
    await open_thought(page, early)
    await page.locator(BODY).evaluate('e=>{e.scrollTop=40;e.dispatchEvent(new Event("scroll"))}')
    await settle(page)
    position = await page.locator(BODY).evaluate('e=>({top:e.scrollTop,max:e.scrollHeight-e.clientHeight})')
    assert position['max'] > position['top'] + 200, f'Stream fixture must overflow: {position}'
    main_top = await page.locator('#main').evaluate('e=>e.scrollTop')
    await event(page, 'message.part.delta', sessionID=SID, messageID=A1, partID='live', field='text', delta='\nLIVE_DELTA')
    await expect(page.locator(BODY + ' ' + thought(A1, 'live') + ' .rtext')).to_have_text('LIVE_REASONING\nLIVE_DELTA')
    await expanded(page, early, True)
    assert abs(await page.locator(BODY).evaluate('e=>e.scrollTop') - position['top']) <= 2, 'A delta stole modal scroll'
    # Same group, genuinely new entry: do not auto-jump away from the reader.
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [
        tool('new', 'bash', {'command': 'new-command'}, 'NEW_EVENT_OUTPUT')])['parts'][0])
    await assert_groups(page, {wid: 34})
    await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(34)
    assert abs(await page.locator(BODY).evaluate('e=>e.scrollTop') - position['top']) <= 2, 'A new entry stole modal scroll'
    await expect(page.locator(DIALOG + ' [data-work-new]')).to_be_visible()
    await page.locator(DIALOG + ' [data-work-new]').click()
    await expect(page.locator(DIALOG + ' [data-work-new]')).not_to_be_visible()
    await page.wait_for_function('()=>{const e=document.querySelector("#workLogBody");return e.scrollHeight-e.scrollTop-e.clientHeight<48}')
    assert abs(await page.locator('#main').evaluate('e=>e.scrollTop') - main_top) <= 2, 'Detail streaming moved main scroll'
    # A public response creates a new group; keep viewing the original selected group.
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [text('comment', 'Public stream commentary.')])['parts'][0])
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [reason('other', 'OTHER_GROUP_REASONING', 20000, None)])['parts'][0])
    await assert_groups(page, {wid: 34, work_id(A1, 'other'): 1})
    await expect(page.locator(BODY)).not_to_contain_text('OTHER_GROUP_REASONING')
    await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(34)
    await page.screenshot(path=str(out / 'stream-open.png'))


async def native_run_shared_row(page, out):
    run = {'id': 'work_native_run', 'sessionID': SID, 'connectionID': 'conn_fixture', 'kind': 'exec',
           'command': 'fixture-native-command', 'stdout': 'NATIVE_OUTPUT_START\n' + 'native line\n' * 40,
           'stderr': '', 'status': 'running'}
    pending = tool('ssh', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': run['command']},
                   json.dumps({'runID': run['id']}), status='running')
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Native run reasoning.'), pending], U1)],
               running=True, runs=[run])
    wid = work_id(A1, 'r1')
    await assert_groups(page, {wid: 2})
    await open_work(page, wid)
    row = page.locator(BODY + ' .activity[data-run-id="work_native_run"]')
    await expect(row).to_have_count(1)
    await expect(row.locator('.tool-environment')).to_contain_text('SSH')
    # Mark auto-open activity explicitly open, then expand its long output.
    if await row.evaluate('e=>e.open'):
        await row.locator(':scope > summary').click()
    await row.locator(':scope > summary').click()
    await row.locator('[data-feed-expand]').click()
    await page.evaluate('''ev=>window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:ev}))''',
                        {'type': 'ssh.command.delta', 'runID': run['id'], 'sessionID': SID, 'stream': 'stdout', 'delta': 'NATIVE_DELTA'})
    await settle(page)
    await expect(row.locator('.tool-output')).to_contain_text('NATIVE_DELTA')
    assert await row.evaluate('e=>e.open')
    await expect(row.locator('[data-feed-expand]')).to_have_attribute('aria-expanded', 'true')
    final = 'FINAL_OUTPUT_START\n' + 'complete native line\n' * 80 + 'FINAL_OUTPUT_END'
    finished = tool('ssh', 'mobile_ssh_ssh_exec', pending['state']['input'],
                    json.dumps({'runID': run['id'], 'stdout': final, 'stderr': 'FINAL_STDERR', 'exitCode': 0}))
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [finished])['parts'][0])
    await assert_groups(page, {wid: 2})
    await expect(row).to_have_count(1)
    assert await row.locator('.tool-output').all_text_contents() == [final, 'FINAL_STDERR']
    await expect(row.locator('[data-feed-expand]').first).to_have_attribute('aria-expanded', 'true')
    await page.evaluate('refreshWorkSheet()')
    await close_work(page)
    await open_work(page, wid)
    assert await row.locator('.tool-output').all_text_contents() == [final, 'FINAL_STDERR'], 'Long final output truncated on refresh/reopen'
    await expect(row.locator('[data-feed-expand]').first).to_have_attribute('aria-expanded', 'true')
    await expect(page.locator(BODY + ' .think')).not_to_contain_text('FINAL_OUTPUT')
    await assert_main_clean(page)


async def remote_delta_reader_focus(page, out):
    output = 'REMOTE_START\n' + ''.join(f'Native output {i}: <literal> & full line.\n' for i in range(80))
    run = {'id': 'work_focus_run', 'sessionID': SID, 'connectionID': 'conn_fixture', 'kind': 'exec',
           'command': 'fixture-focus-command', 'stdout': output, 'stderr': '', 'status': 'running'}
    parts = [reason('r1', 'Review native output without losing the reader.'),
             tool('earlier', 'read', {'filePath': 'earlier.txt'}, 'EARLIER_OUTPUT')]
    parts.extend(tool(f'padding{i}', 'read', {'filePath': f'padding{i}.txt'}, f'PADDING_{i}') for i in range(12))
    parts.append(tool('ssh', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': run['command']},
                      json.dumps({'runID': run['id']}), status='running'))
    await seed(page, [user(), message(A1, 'assistant', parts, U1)], running=True, runs=[run])
    wid = work_id(A1, 'r1')
    await assert_groups(page, {wid: len(parts)})
    await open_work(page, wid)
    row = page.locator(BODY + ' .activity[data-run-id="work_focus_run"]')
    if not await row.evaluate('e=>e.open'):
        await row.locator(':scope > summary').click()
    await row.locator('[data-feed-expand]').click()
    assert await row.locator('.tool-output').text_content() == output
    earlier = page.locator(BODY + ' ' + activity_segment(A1, 'earlier') + ' > summary')
    await earlier.scroll_into_view_if_needed()
    await earlier.focus()
    await settle(page)
    position = await page.locator(BODY).evaluate('e=>({top:e.scrollTop,max:e.scrollHeight-e.clientHeight})')
    assert position['max'] - position['top'] > 200, f'Reader must be well above the remote output: {position}'
    await expect(page.locator(DIALOG + ' [data-work-new]')).not_to_be_visible()
    main_top = await page.locator('#main').evaluate('e=>e.scrollTop')
    # A plain refresh replaces the focused DOM row, but must not announce new work.
    await page.evaluate('refreshWorkSheet()')
    await settle(page)
    await expect(earlier).to_be_focused()
    await expect(page.locator(DIALOG + ' [data-work-new]')).not_to_be_visible()
    assert abs(await page.locator(BODY).evaluate('e=>e.scrollTop') - position['top']) <= 2
    source = await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))')
    delta = 'REMOTE_DELTA_START\n' + 'Additional native output <xml> & intact.\n' * 45 + 'REMOTE_DELTA_END'
    await page.evaluate('''ev=>window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:ev}))''',
                        {'type': 'ssh.command.delta', 'runID': run['id'], 'sessionID': SID, 'stream': 'stdout', 'delta': delta})
    await settle(page)
    await assert_groups(page, {wid: len(parts)})
    await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(len(parts))
    await expect(row).to_have_count(1)
    assert await row.locator('.tool-output').text_content() == output + delta, 'Native delta lost/truncated output'
    assert await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))') == source, \
        'Native-only fixture unexpectedly rewrote model parts'
    await expect(earlier).to_be_focused()
    assert abs(await page.locator(BODY).evaluate('e=>e.scrollTop') - position['top']) <= 2, 'Native delta stole reader scroll'
    assert abs(await page.locator('#main').evaluate('e=>e.scrollTop') - main_top) <= 2, 'Native delta moved the main feed'
    await expect(row.locator('[data-feed-expand]')).to_have_attribute('aria-expanded', 'true')
    await expect(page.locator(DIALOG + ' [data-work-new]')).to_be_visible()
    await page.screenshot(path=str(out / 'remote-reader-new-events.png'))
    await page.locator(DIALOG + ' [data-work-new]').click()
    await expect(page.locator(DIALOG + ' [data-work-new]')).not_to_be_visible()
    await page.wait_for_function('()=>{const e=document.querySelector("#workLogBody");return e.scrollHeight-e.scrollTop-e.clientHeight<48}')
    await page.evaluate('refreshWorkSheet()')
    assert await row.locator('.tool-output').text_content() == output + delta
    await expect(page.locator(DIALOG + ' [data-work-new]')).not_to_be_visible()


async def native_close_after_trigger_rerender(page, out):
    prose = '\n\n'.join(f'Context {i}. ' + 'Keep the main scroll and draft. ' * 8 for i in range(20))
    await seed(page, [user(), message(A1, 'assistant', [text('before', prose), reason('r1', 'Work details.'),
        text('after', prose)], U1, 9000)])
    wid = work_id(A1, 'r1')
    draft = 'Unsent draft survives native close <think>literal</think>'
    await page.fill('#inp', draft)
    await page.locator(summary(wid)).scroll_into_view_if_needed()
    await page.locator(summary(wid)).evaluate('e=>{window.originalWorkTrigger=e}')
    await open_work(page, wid)
    await page.evaluate('renderMsgs()')
    await settle(page)
    assert not await page.evaluate('window.originalWorkTrigger.isConnected'), 'Fixture must replace the opener DOM node'
    # Exercise the latest main scroll, not only the position at modal open.
    position = await page.locator('#main').evaluate('''e=>{
      e.scrollTop=Math.min(e.scrollHeight-e.clientHeight-100,e.scrollTop+120);
      return {main:e.scrollTop,document:document.scrollingElement.scrollTop};
    }''')
    assert position['main'] > 0
    await page.locator(DIALOG).evaluate('e=>e.close("native-fixture")')
    await settle(page)
    await expect(page.locator(DIALOG)).not_to_be_visible()
    await expect(page.locator(summary(wid))).to_be_focused()
    assert await page.locator(DIALOG).evaluate('e=>e.returnValue') == 'native-fixture'
    assert abs(await page.locator('#main').evaluate('e=>e.scrollTop') - position['main']) <= 2, 'Native close jumped main scroll'
    assert abs(await page.evaluate('document.scrollingElement.scrollTop') - position['document']) <= 2
    await expect(page.locator('#inp')).to_have_value(draft)
    await page.evaluate('refreshWorkSheet();renderMsgs()')
    await settle(page)
    await expect(page.locator(DIALOG)).not_to_be_visible()
    await open_work(page, wid)
    await close_work(page)
    await expect(page.locator('#inp')).to_have_value(draft)


async def native_provisional_handoff(page, out):
    for mode in ['open', 'expanded', 'closed']:
        # Same session, new view: old disclosure caches must not bleed into this run.
        await page.evaluate('resetThread()')
        await seed(page, [user()], running=True)
        run_id = 'handoff_' + mode
        output = 'HANDOFF_START\n' + 'Preserve native <xml> & output.\n' * 75 + 'HANDOFF_END'
        for payload in [
            {'type': 'ssh.command.started', 'command': 'fixture-handoff-command'},
            {'type': 'ssh.command.delta', 'stream': 'stdout', 'delta': output},
        ]:
            await page.evaluate('''ev=>window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:ev}))''',
                                dict(payload, runID=run_id, sessionID=SID, connectionID='conn_fixture'))
            await settle(page)
        await expect(page.locator(SUMMARY)).to_have_count(1)
        provisional = await page.locator(SUMMARY).get_attribute('data-work-open')
        await assert_groups(page, {provisional: 1}, ['user:' + U1, provisional])
        assert await page.evaluate('id=>S.workBlocks.get(id).events[0].provisional===true', provisional)
        await open_work(page, provisional)
        row = page.locator(BODY + ' .activity[data-run-id=' + json.dumps(run_id) + ']')
        await expect(row).to_have_count(1)
        if await row.evaluate('e=>e.open'):
            await row.locator(':scope > summary').click()
        if mode != 'closed':
            await row.locator(':scope > summary').click()
        old_key = await row.get_attribute('data-part')
        if mode == 'expanded':
            await row.locator('[data-feed-expand]').click()
            await expect(row.locator('[data-feed-expand]')).to_have_attribute('aria-expanded', 'true')
            await expect(row.locator('[data-feed-expand]')).to_be_focused()
        await page.locator(DIALOG).evaluate('e=>{window.handoffCloses=0;e.addEventListener("close",()=>window.handoffCloses++)}')
        await event(page, 'message.updated', info=message(A1, 'assistant', parent=U1)['info'])
        pending = tool('handoff', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': 'fixture-handoff-command'},
                       json.dumps({'runID': run_id}), status='running')
        await event(page, 'message.part.updated', part=message(A1, 'assistant', [pending])['parts'][0])
        real = work_id(A1, 'handoff')
        await assert_groups(page, {real: 1}, ['user:' + U1, real])
        assert real != provisional
        assert await page.evaluate('id=>!S.workBlocks.has(id)', provisional)
        bindings = await page.evaluate('''()=>[...buildWorkTranscript([...S.msgs.values()].sort((a,b)=>a.info.id.localeCompare(b.info.id))).runBlocks]''')
        assert bindings == [[run_id, real]], bindings
        await expect(page.locator(DIALOG)).to_be_visible()
        assert await page.locator(DIALOG).evaluate('e=>e.open&&e.matches(":modal")')
        assert await page.evaluate('window.handoffCloses') == 0, 'Handoff closed/reopened the selected native log'
        await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(1)
        await expect(row).to_have_count(1)
        await expect(row).to_have_attribute('data-tool-part', 'handoff')
        assert await row.get_attribute('data-part') != old_key
        assert await row.evaluate('e=>e.open') == (mode != 'closed'), f'{mode} disclosure lost in handoff'
        assert await row.locator('.tool-output').text_content() == output
        if mode == 'expanded':
            await expect(row.locator('[data-feed-expand]')).to_have_text('Show less')
            await expect(row.locator('[data-feed-expand]')).to_be_focused()
            await expect(row.locator('[data-feed-expand]')).to_have_attribute('aria-expanded', 'true')
        if mode == 'closed':
            assert await row.evaluate('e=>e.hasAttribute("data-closed")')
        await page.evaluate('refreshWorkSheet();renderMsgs()')
        await settle(page)
        assert await row.evaluate('e=>e.open') == (mode != 'closed')
        if mode == 'expanded':
            await expect(row.locator('[data-feed-expand]')).to_be_focused()
        await close_work(page)
        await expect(page.locator(summary(real))).to_be_focused()
        await open_work(page, real)
        assert await row.evaluate('e=>e.open') == (mode != 'closed')
        assert await row.locator('.tool-output').text_content() == output
        await close_work(page)
        # Clear the session view, then reconstruct the same IDs as completed source.
        await page.evaluate('sid=>resetThread(sid)', SID)
        completed = tool('handoff', pending['tool'], pending['state']['input'],
                         json.dumps({'runID': run_id, 'stdout': output, 'exitCode': 0}))
        await seed(page, [user(), message(A1, 'assistant', [completed], U1, 9000)])
        await open_work(page, real)
        assert not await row.evaluate('e=>e.open'), 'Prior view disclosure cache leaked into reconstructed session'
        await expect(row.locator('.feed-text-window.expanded')).to_have_count(0)
        await close_work(page)


async def native_exact_id_no_phantom_output(page, out):
    command = 'fixture-identical-command'
    expected_id, other_id = 'expected_native_run', 'different_native_run'
    other_output = 'OTHER_RUN_OUTPUT\n' + 'Distinct native output.\n' * 35 + 'OTHER_RUN_END'
    run = {'id': other_id, 'sessionID': SID, 'connectionID': 'conn_fixture', 'kind': 'exec',
           'command': command, 'stdout': other_output, 'stderr': '', 'status': 'running'}
    part = tool('exact', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': command},
                json.dumps({'runID': expected_id}), status='running')
    saved = [user(), message(A1, 'assistant', [part], U1)]
    await seed(page, saved, running=True, runs=[run])
    wid = work_id(A1, 'exact')
    await assert_groups(page, {wid: 2}, ['user:' + U1, wid])
    await open_work(page, wid)
    exact = page.locator(BODY + ' ' + activity_segment(A1, 'exact'))
    other = page.locator(BODY + ' .activity[data-run-id="different_native_run"]')
    await expect(exact).to_have_attribute('data-run-id', expected_id)
    await expect(other).to_have_count(1)
    await expect(exact).not_to_contain_text('OTHER_RUN_OUTPUT')
    assert await other.locator('.tool-output').text_content() == other_output
    await expect(page.locator(BODY + ' .tool-output', has_text='OTHER_RUN_OUTPUT')).to_have_count(1)
    await expect(page.locator(BODY + ' .activity')).to_have_count(2)
    # The missing exact run arrives later; it must replace neither the other run nor its output.
    expected_output = 'EXPECTED_RUN_OUTPUT <literal> & intact'
    for payload in [{'type': 'ssh.command.started', 'command': command},
                    {'type': 'ssh.command.delta', 'stream': 'stdout', 'delta': expected_output}]:
        await page.evaluate('''ev=>window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:ev}))''',
                            dict(payload, runID=expected_id, sessionID=SID, connectionID='conn_fixture'))
        await settle(page)
    await assert_groups(page, {wid: 2})
    await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(2)
    assert await exact.locator('.tool-output').text_content() == expected_output
    assert await other.locator('.tool-output').text_content() == other_output
    for marker in ['OTHER_RUN_OUTPUT', 'EXPECTED_RUN_OUTPUT']:
        await expect(page.locator(BODY + ' .tool-output', has_text=marker)).to_have_count(1)
    await page.evaluate('refreshWorkSheet();renderMsgs()')
    await settle(page)
    await expect(page.locator(BODY + ' .activity')).to_have_count(2)
    assert await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))') == saved
    await page.screenshot(path=str(out / 'native-exact-no-phantom.png'))


async def provisional_handoff_after_commentary(page, out):
    run_id = 'commentary_handoff_run'
    output = 'COMMENTARY_HANDOFF_START\n' + 'Full native output <xml> & line.\n' * 80 + 'COMMENTARY_HANDOFF_END'
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Earlier reasoning.', end=None)], U1)], running=True)
    for payload in [{'type': 'ssh.command.started', 'command': 'fixture-commentary-handoff'},
                    {'type': 'ssh.command.delta', 'stream': 'stdout', 'delta': output}]:
        await page.evaluate('''ev=>window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:ev}))''',
                            dict(payload, runID=run_id, sessionID=SID, connectionID='conn_fixture'))
        await settle(page)
    old = work_id(A1, 'r1')
    await assert_groups(page, {old: 2})
    await open_work(page, old)
    row = page.locator(BODY + ' .activity[data-run-id="commentary_handoff_run"]')
    if await row.evaluate('e=>e.open'):
        await row.locator(':scope > summary').click()
    await row.locator(':scope > summary').click()
    await row.locator('[data-feed-expand]').click()
    await expect(row.locator('[data-feed-expand]')).to_be_focused()
    await page.locator(DIALOG).evaluate('e=>{window.partialHandoffCloses=0;e.addEventListener("close",()=>window.partialHandoffCloses++)}')
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [text('comment', 'Public commentary before the real tool part.')])['parts'][0])
    keys = await page.locator(SUMMARY).evaluate_all('els=>els.map(e=>e.dataset.workOpen)')
    assert len(keys) == 2 and keys[0] == old
    provisional = keys[1]
    await assert_groups(page, {old: 1, provisional: 1}, ['user:' + U1, old,
                        'text:Public commentary before the real tool part.', provisional])
    # The old group still exists: selected native work must follow its moved run anyway.
    await expect(page.locator(DIALOG)).to_be_visible()
    await expect(page.locator(BODY + ' .think')).to_have_count(0)
    await expect(row).to_have_count(1)
    await expect(row.locator('[data-feed-expand]')).to_have_attribute('aria-expanded', 'true')
    await expect(row.locator('[data-feed-expand]')).to_be_focused()
    real_part = tool('real', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': 'fixture-commentary-handoff'},
                     json.dumps({'runID': run_id}), status='running')
    await event(page, 'message.part.updated', part=message(A1, 'assistant', [real_part])['parts'][0])
    real = work_id(A1, 'real')
    await assert_groups(page, {old: 1, real: 1}, ['user:' + U1, old,
                        'text:Public commentary before the real tool part.', real])
    assert await page.evaluate('id=>S.workBlocks.has(id)', old), 'Old reasoning group must remain'
    assert await page.evaluate('window.partialHandoffCloses') == 0, 'Partial handoff closed the log'
    await expect(page.locator(BODY + ' .work-log-entry')).to_have_count(1)
    await expect(row).to_have_attribute('data-tool-part', 'real')
    assert await row.evaluate('e=>e.open')
    assert await row.locator('.tool-output').text_content() == output
    await expect(row.locator('[data-feed-expand]')).to_have_text('Show less')
    await expect(row.locator('[data-feed-expand]')).to_be_focused()
    await page.evaluate('refreshWorkSheet();renderMsgs()')
    await settle(page)
    await expect(row.locator('[data-feed-expand]')).to_be_focused()
    await close_work(page)
    await expect(page.locator(summary(real))).to_be_focused()
    await open_work(page, old)
    await expect(page.locator(BODY + ' .activity')).to_have_count(0)
    await expect(page.locator(BODY + ' .rtext')).to_have_text('Earlier reasoning.')


async def polling_permission_interrupts_work(page, out):
    saved = [user(), message(A1, 'assistant', [reason('r1', 'Inspect the saved work.')], U1, 9000)]
    await seed(page, saved)
    await page.evaluate('''()=>{S.spec={paths:{'/permission/{requestID}/reply':{post:{}}}}}''')
    wid = work_id(A1, 'r1')
    await page.fill('#inp', 'Polling must not lose this draft')
    await open_work(page, wid)
    requests, permissions = [], []
    storage = await page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    responses = {'/session/' + SID + '/message': saved, '/session/status': {}, '/question': []}

    async def snapshot_reads(route):
        path = urlsplit(route.request.url).path
        requests.append((route.request.method, path))
        if route.request.method == 'GET' and path == '/permission':
            await route.fulfill(json=permissions)
        elif route.request.method == 'GET' and path in responses:
            await route.fulfill(json=responses[path])
        else:
            await route.fallback()

    async def poll():
        await page.evaluate('''async()=>{S.sending=false;try{await syncSession()}finally{S.sending=true}}''')
        await settle(page)

    await page.route('**/*', snapshot_reads)
    try:
        await poll()
        await expect(page.locator(DIALOG)).to_be_visible()
        permissions.append({'id': 'foreign_permission', 'sessionID': 'other_session',
                            'permission': 'bash', 'patterns': ['foreign-command']})
        await poll()
        await expect(page.locator(DIALOG)).to_be_visible()
        permissions.append({'id': 'polled_permission', 'sessionID': SID,
                            'permission': 'bash', 'patterns': ['fixture-polled-command']})
        await poll()
        await expect(page.locator(DIALOG)).not_to_be_visible()
        card = page.locator('#main [data-pid="polled_permission"]')
        await expect(card).to_be_visible()
        await card.locator('[data-r="once"]').click(trial=True)
        await expect(page.locator('#main [data-pid="foreign_permission"]')).to_have_count(0)
        await expect(page.locator('#inp')).to_have_value('Polling must not lose this draft')
        await open_work(page, wid)
        await poll()  # An already-known permission must not repeatedly kick the reader out.
        await expect(page.locator(DIALOG)).to_be_visible()
        assert await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))') == saved
        assert await page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})') == storage
        expected = [('GET', path) for path in [*responses, '/permission']]
        assert sorted(requests) == sorted(expected * 4), f'Polling made non-read or unexpected requests: {requests}'
    finally:
        await page.evaluate('S.sending=true')
        await page.unroute('**/*', snapshot_reads)


async def ssh_write_authoritative_path_count(page, out):
    resolved = '/srv/project/src/login.js'
    result = json.dumps({'path': resolved, 'patch': PATCH, 'additions': 1, 'deletions': 1})
    relative = tool('relative', 'mobile_ssh_ssh_write_file',
                    {'connection_id': 'conn_fixture', 'path': 'src/login.js', 'content': 'fixedLogin\n'}, result)
    absolute = tool('absolute', 'mobile_ssh_ssh_write_file',
                    {'connection_id': 'conn_fixture', 'path': resolved, 'content': 'fixedLogin\n'}, result)
    wid = work_id(A1, 'relative')
    for parts in [[relative], [relative, absolute]]:
        saved = [user(), message(A1, 'assistant', parts, U1, 9000)]
        await seed(page, saved)
        await assert_groups(page, {wid: len(parts)})
        assert await page.evaluate('id=>S.workBlocks.get(id).edits', wid) == 1, 'One resolved SSH file was counted twice'
        label = await page.locator(summary(wid)).inner_text()
        assert re.search(r'\b1\s+(?:file\b|файл\w*)', label, re.IGNORECASE), label
        assert not re.search(r'\b2\s+(?:files?\b|файл\w*)', label, re.IGNORECASE), label
        await open_work(page, wid)
        for row in await page.locator(BODY + ' .activity').all():
            if not await row.evaluate('e=>e.open'):
                await row.locator(':scope > summary').click()
            await expect(row.locator('.diff-line.a .diff-text')).to_contain_text('fixedLogin')
            await expect(row.locator('.diff-line.d .diff-text')).to_contain_text('oldLogin')
        await page.evaluate('refreshWorkSheet();renderMsgs()')
        await settle(page)
        assert await page.evaluate('id=>S.workBlocks.get(id).edits', wid) == 1
        assert await page.evaluate('[...S.msgs.values()].map(m=>({info:m.info,parts:[...m.parts.values()]}))') == saved
        await close_work(page)


async def permission_interrupts_work(page, out):
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Awaiting approval.', end=None)], U1)], running=True)
    await page.evaluate('''()=>{S.spec={paths:{'/permission/{requestID}/reply':{post:{}}}}}''')
    await page.fill('#inp', 'Keep this approval-time draft')
    await open_work(page, work_id(A1, 'r1'))
    await event(page, 'permission.asked', id='perm_interrupt', sessionID=SID,
                permission='bash', patterns=['fixture-approval-command'])
    await expect(page.locator(DIALOG)).not_to_be_visible()
    card = page.locator('#main [data-pid="perm_interrupt"]')
    await expect(card).to_be_visible()
    await expect(card.locator('[data-r="once"]')).to_be_enabled()
    # Trial performs Playwright's hit-target checks without sending any approval.
    await card.locator('[data-r="once"]').click(trial=True)
    await expect(page.locator('#inp')).to_have_value('Keep this approval-time draft')
    await expect(page.locator(BODY + ' [data-pid]')).to_have_count(0)
    await assert_main_clean(page)


async def error_badge_during_running(page, out):
    parts = [tool('read', 'read', {'filePath': 'good.txt'}, 'GOOD'),
             tool('bad', 'read', {'filePath': 'missing.txt'}, status='error', error='EARLIER_READ_ERROR'),
             tool('active', 'read', {'filePath': 'later.txt'}, status='running')]
    await seed(page, [user(), message(A1, 'assistant', parts, U1)], running=True)
    wid = work_id(A1, 'read')
    await assert_groups(page, {wid: 3})
    counts = await page.evaluate('id=>{const b=S.workBlocks.get(id);return {reads:b.reads,errors:b.errors,running:b.running}}', wid)
    assert counts == {'reads': 1, 'errors': 1, 'running': 1}, counts
    label = await page.locator(summary(wid)).inner_text()
    assert read_counts(label) == [1], label
    assert re.search(r'fail|error|ошиб|неуда', label, re.IGNORECASE), f'Earlier error hidden by later running work: {label}'
    assert re.search(r'work|running|reading|работ|чтен', label, re.IGNORECASE), f'Running activity not indicated: {label}'
    await open_work(page, wid)
    await expect(page.locator(BODY + ' ' + activity_segment(A1, 'bad'))).to_contain_text('EARLIER_READ_ERROR')


async def hidden_errors(page, out):
    for bad in [tool('bad', 'bash', {'command': 'failing-command'}, status='error', error='COMMAND_ERROR'),
                tool('bad', 'mobile_ssh_ssh_exec', {'connection_id': 'conn_fixture', 'command': 'nonzero-command'},
                     json.dumps({'stdout': '', 'stderr': 'NONZERO_ERROR', 'exitCode': 7}))]:
        await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Reason before failure.'), bad], U1, 9000)])
        wid = work_id(A1, 'r1')
        await assert_groups(page, {wid: 2})
        label = await page.locator(summary(wid)).inner_text()
        assert re.search(r'fail|error|ошиб|неуда', label, re.IGNORECASE), f'Tool failure hidden behind a neutral summary: {label}'
        await open_work(page, wid)
        await expect(page.locator(BODY + ' .activity')).to_contain_text(re.compile('COMMAND_ERROR|NONZERO_ERROR'))
        await close_work(page)
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Interrupted reasoning.', end=None)], U1)], running=True)
    await event(page, 'session.error', sessionID=SID, error={'name': 'APIError', 'data': {'message': 'SESSION_ERROR'}})
    await expect(page.locator('#main .error-card')).to_contain_text('SESSION_ERROR')
    await open_work(page, work_id(A1, 'r1'))
    await expect(page.locator(BODY + ' .think[data-running]')).to_have_count(0)


async def pending_cards_and_lifecycle(page, out):
    await seed(page, [user(), message(A1, 'assistant', [reason('r1', 'Waiting for user permission.')], U1, 9000)])
    # Real handlers/spec checks, with supported reply paths but no actual reply.
    await page.evaluate('''()=>{S.spec={paths:{'/permission/{requestID}/reply':{post:{}},'/question/{requestID}/reply':{post:{}}}}}''')
    await event(page, 'permission.asked', id='perm_work', sessionID=SID, permission='bash', patterns=['fixture-safe-command'])
    await event(page, 'question.asked', id='question_work', sessionID=SID,
                questions=[{'header': 'Target', 'question': 'Which target?', 'options': [{'label': 'Local', 'description': 'Keep local'}]}])
    await page.evaluate('closeSheet();renderMsgs()')
    await expect(page.locator('#main [data-pid="perm_work"]')).to_be_visible()
    await expect(page.locator('#main .q-pending')).to_be_visible()
    wid = work_id(A1, 'r1')
    await open_work(page, wid)
    await expect(page.locator(BODY + ' [data-pid], ' + BODY + ' .q-pending, ' + BODY + ' .qsheet')).to_have_count(0)
    # Other ordinary sheets and the special question sheet both replace Work.
    await page.evaluate('modelSheet()')
    await expect(page.locator(DIALOG)).not_to_be_visible()
    await expect(page.locator('#sheet')).to_have_class(re.compile(r'\bon\b'))
    await page.evaluate('closeSheet()')
    await open_work(page, wid)
    await page.evaluate('openQuestionSheet()')
    await expect(page.locator(DIALOG)).not_to_be_visible()
    await expect(page.locator('#sheet .qsheet')).to_be_visible()
    await page.evaluate('closeSheet();renderMsgs()')
    await open_work(page, wid)
    # Exercise the real session switch, fulfilling only its expected read APIs.
    async def session_reads(route):
        path = urlsplit(route.request.url).path
        if route.request.method == 'GET' and path in ['/session/work_other/message', '/session/status', '/permission', '/question']:
            await route.fulfill(json={} if path == '/session/status' else [])
        else:
            await route.fallback()
    await page.route('**/*', session_reads)
    try:
        await page.evaluate('openSession("work_other")')
        await expect(page.locator(DIALOG)).not_to_be_visible()
        assert await page.evaluate('S.sid') == 'work_other'
        await expect(page.locator(SUMMARY)).to_have_count(0)
        assert await page.evaluate('S.workBlocks.size') == 0, 'Old work registry survived a session switch'
    finally:
        await page.unroute('**/*', session_reads)


CASES = [chronological_groups_reload, hard_boundaries, literal_tags_and_no_chatter,
         honest_read_counts, details_and_saved_expansion, pure_presentation,
         accessible_modal_and_scroll, streaming_scroll_and_new_events,
         native_run_shared_row, remote_delta_reader_focus, native_close_after_trigger_rerender,
         native_provisional_handoff, native_exact_id_no_phantom_output, provisional_handoff_after_commentary,
         polling_permission_interrupts_work, ssh_write_authoritative_path_count,
         permission_interrupts_work, error_badge_during_running,
         hidden_errors, pending_cards_and_lifecycle]


async def main(args):
    out = Path(tempfile.mkdtemp(prefix='work-log-proof-'))
    print(f'Evidence: {out}', flush=True)
    source = HTML.read_text(encoding='utf-8')
    themes, widths = args.theme or ['light', 'dark'], args.width or [320, 412]
    variants = [(theme, width) for theme in dict.fromkeys(themes) for width in dict.fromkeys(widths)]
    report = {'asset_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(), 'checks': []}
    selected = [case for case in CASES if not args.case or case.__name__ in args.case]
    async with async_playwright() as pw:
        browser = await launch_browser(pw, args.browser_executable)
        try:
            for theme, width in variants:
                variant_out = out / f'{theme}-{width}'
                variant_out.mkdir()
                for case in selected:
                    context, page, errors, unexpected = await boot_page(browser, theme=theme, source=source)
                    result = {'case': case.__name__, 'theme': theme, 'viewport': {'width': width, 'height': 800}}
                    try:
                        await page.set_viewport_size(result['viewport'])
                        await settle(page)
                        await case(page, variant_out)
                        assert not errors, f'Browser errors: {errors}'
                        assert not unexpected, f'Unexpected requests: {unexpected}'
                        result['status'] = 'PASS'
                    except Exception:  # noqa: BLE001 - report every scenario failure before exiting nonzero
                        result.update(status='FAIL', error=traceback.format_exc(), page_errors=errors, unexpected_requests=unexpected)
                        await page.screenshot(path=str(variant_out / (case.__name__ + '-failure.png')))
                        (variant_out / (case.__name__ + '-failure.html')).write_text(await page.content(), encoding='utf-8')
                    finally:
                        await context.close()
                    report['checks'].append(result)
                    print(f'{result["status"]} {theme}/{width} {case.__name__}', flush=True)
                    if result['status'] == 'FAIL':
                        print(result['error'], flush=True)
        finally:
            await browser.close()
    total = len(report['checks'])
    failed = sum(check['status'] == 'FAIL' for check in report['checks'])
    report['totals'] = {'scenarios': len(selected), 'configurations': len(variants),
                        'executed': total, 'passed': total - failed, 'failed': failed}
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'{total - failed}/{total} passed ({len(selected)} scenarios x {len(variants)} configurations); report: {out / "report.json"}', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=[case.__name__ for case in CASES])
    parser.add_argument('--browser-executable')
    parser.add_argument('--theme', action='append', choices=['light', 'dark'], help='Default: both themes')
    parser.add_argument('--width', action='append', type=int, choices=[320, 412], help='Default: both widths')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
