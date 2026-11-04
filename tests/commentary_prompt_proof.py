"""Offline Stage-3 commentary guidance proof against the actual HTML submit handler.

Run: ../.local/api-test-venv/Scripts/python.exe tests/commentary_prompt_proof.py
Set PLAYWRIGHT_BROWSER_EXECUTABLE or CHROME_EXECUTABLE for an installed Chrome.
Only boot/network/native transport are fixtures (the ssh_optional_proof Fixture
pattern); submit, prompt serialization and the feed renderer are production code.
No model, SSH or build is run. Three proofs:

1. The Communication guidance is present exactly once per submit in every
   mode x connection x websearch combination, is appended after the websearch
   line, and preserves every earlier system phrase verbatim.
2. A silent model (tools + reasoning, zero public text before the final answer)
   yields no fabricated chatter nodes in the main feed and an honest work
   summary status/label (work_log_proof helpers).
3. The guidance expresses the user's-language requirement; no hardcoded
   language name replaces it.
"""
import asyncio
import json
import tempfile
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

from reasoning_activity_proof import (
    A1,
    A2,
    U1,
    boot_page,
    launch_browser,
    message,
    reason,
    seed,
    text,
    tool,
    user,
)
from work_log_proof import (
    assert_entries,
    assert_main_clean,
    close_work,
    main_sequence,
    open_work,
    read_counts,
    settle,
    summary,
    work_id,
)

SID = 'ses_commentary_proof'
LOCAL = '/storage/emulated/0/Projects/local-only-project'
REMOTE = '/srv/services/not-a-project-mirror'
CONNECTION = {'id': 'conn_commentary', 'name': 'Commentary fixture',
              'host': 'ssh.invalid', 'directory': REMOTE,
              # Deliberate canaries: submit must whitelist metadata, not serialize a connection.
              'password': 'PASSWORD_CANARY', 'privateKey': 'PRIVATE_KEY_CANARY',
              'passphrase': 'PASSPHRASE_CANARY'}
SECRETS = ('PASSWORD_CANARY', 'PRIVATE_KEY_CANARY', 'PASSPHRASE_CANARY')

# Stage-3 contract: every requirement must stay an exact production substring.
GUIDANCE = {
    'headline': 'Communication: before each significant step',
    'user_language': "in the user's language",
    'brevity': 'one or two sentences',
    'findings': 'report important findings, changes of approach and obstacles',
    'no_tool_narration': 'Do not narrate every tool call',
    'no_reasoning_leak': 'do not reveal internal reasoning',
    'intent_vs_fact': 'State intentions as intentions',
    'no_unverified_claims': 'never claim that something works or was verified without an actual verified result',
}
PRESERVED_BASE = ['primary workspace', 'local file, search and shell tools',
                  'earlier messages used SSH-only instructions']
SSH_PRESERVED = ['Optional SSH access is available', 'separate from the local project directory',
                 'Never expose credentials']
SSH_ABSENT_MARKER = 'No SSH connection is selected'
WEBSEARCH_SENTENCE = 'Use the websearch tool when web information is needed.'
BANNED_PHRASE = 'Do not run local shell'
# The guidance must never prescribe a concrete language instead of the user's one.
HARDCODED_LANGUAGES = ['russian', 'english', 'русск', 'англи', 'language=ru', 'language=en',
                       'lang=ru', 'lang=en', 'locale=ru', 'locale=en']

ANSWER = 'Final answer states only verified facts.'
REASON_TEXT = 'SILENT_MODEL_INTERNAL_REASONING_TOKEN internal planning that must stay out of the main feed. '


class Fixture:
    """Same interception pattern as ssh_optional_proof: /config, /session, PATCH,
    prompt_async capture; everything else denied."""

    async def start(self, browser, *, mode='build', connected=False, websearch=False):
        self.context, self.page, self.errors, self.unexpected = await boot_page(browser)
        self.requests = []

        async def route_request(route):
            request = route.request
            url = urlsplit(request.url)
            path, method = url.path, request.method
            body = request.post_data_json if request.post_data else None
            entry = {'path': path, 'method': method, 'body': body,
                     'directory': parse_qs(url.query).get('directory', [])}
            self.requests.append(entry)
            assert entry['directory'] == [LOCAL], entry
            if path == '/config' and method == 'GET':
                await route.fulfill(json={})
            elif path == '/session' and method == 'POST':
                await route.fulfill(json={'id': SID, 'title': 'Offline test'})
            elif path == '/session/' + SID and method == 'GET':
                await route.fulfill(json={'id': SID, 'permission': []})
            elif path == '/session/' + SID and method == 'PATCH':
                await route.fulfill(json={'id': SID, **(body or {})})
            elif path == '/session/' + SID + '/prompt_async' and method == 'POST':
                await route.fulfill(status=204)
            else:
                self.unexpected.append(method + ' ' + request.url)
                await route.abort()

        # Page routes override boot_page's deny-by-default context routes.
        await self.page.route('**/*', route_request)
        await self.page.evaluate('''({sid,dir,mode,connection,connected,websearch})=>{
          S.dir=dir;S.sid=sid;S.agent=mode;S.model=null;S.efforts={};
          S.webSearch=websearch;S.localPolicies={};S.running=false;S.sending=false;
          S.connections=[connection];S.connectionID=connected?connection.id:null;
          lset('oc_local_policies',S.localPolicies);
          window.nativeRequests=[];window.nativeErrors=[];
          window.CodexNative={request(raw){
            const r=JSON.parse(raw);nativeRequests.push(r);
            if(r.action==='connections')window.codexNativeResult(r.id,{connections:[connection]});
            else if(['historyRegister','sessionContext'].includes(r.action))window.codexNativeResult(r.id,{saved:true});
            else {nativeErrors.push(r.action);window.codexNativeResult(r.id,{error:'Unexpected native action'});}
          }};
          chips();setRun();
        }''', {'sid': SID, 'dir': LOCAL, 'mode': mode, 'connection': CONNECTION,
               'connected': connected, 'websearch': websearch})
        return self

    async def send(self, prompt='Fix the failing test and keep me posted.'):
        start = len(self.requests)
        await self.page.fill('#inp', prompt)
        async with self.page.expect_response(lambda r: urlsplit(r.url).path.endswith('/prompt_async')):
            await self.page.click('#send')
        await self.page.wait_for_function('!S.sending')
        # Stop only the polling watchdog between fixture turns; do not replace submit.
        await self.page.evaluate('S.running=false;setRun()')
        prompts = [r for r in self.requests[start:] if r['path'].endswith('/prompt_async')]
        assert len(prompts) == 1, prompts
        return prompts[0]['body']

    async def finish(self):
        assert not self.errors, self.errors
        assert not self.unexpected, self.unexpected
        assert not await self.page.evaluate('nativeErrors')
        assert all(r['method'] == 'GET' for r in self.requests if 'config' in r['path'])
        await self.context.close()


def check_guidance(body, prompt, mode, connected, websearch):
    system = body['system']
    # Exactly once per submit, appended after the websearch line, ending the system text.
    assert system.count('Communication:') == 1, system
    assert system.count(GUIDANCE['headline']) == 1, system
    assert system.endswith(GUIDANCE['no_unverified_claims'] + '.'), system[-120:]
    base_idx = system.index('primary workspace')
    ssh_idx = system.index(SSH_PRESERVED[0] if connected else SSH_ABSENT_MARKER)
    comm_idx = system.index('Communication:')
    assert base_idx < ssh_idx < comm_idx, system
    if websearch:
        assert system.index(WEBSEARCH_SENTENCE) > ssh_idx
        assert system.index(WEBSEARCH_SENTENCE) < comm_idx
    else:
        assert WEBSEARCH_SENTENCE not in system
    for needle in PRESERVED_BASE:
        assert needle in system, needle
    if connected:
        for needle in SSH_PRESERVED:
            assert needle in system, needle
        assert REMOTE in system
        assert 'connection_id=' + CONNECTION['id'] in system
        assert 'session_id=' + SID in system
    else:
        assert REMOTE not in system
        assert SSH_PRESERVED[0] not in system
    for needle in GUIDANCE.values():
        assert needle in system, needle
    assert BANNED_PHRASE not in system
    for secret in SECRETS:
        assert secret not in system
    assert body['parts'] == [{'type': 'text', 'text': prompt}], body['parts']
    assert 'tools' not in body, 'Deprecated tools would silently overwrite approvals'
    assert body['agent'] == ('plan' if mode == 'plan' else 'build'), body['agent']
    return system


async def guidance_matrix(browser, report):
    combos = [(mode, connected, websearch)
              for mode in ['build', 'plan', 'auto']
              for connected in [False, True]
              for websearch in [False, True]]
    for mode, connected, websearch in combos:
        f = await Fixture().start(browser, mode=mode, connected=connected, websearch=websearch)
        prompt = 'Fix the failing test and keep me posted.'
        body = await f.send(prompt)
        system = check_guidance(body, prompt, mode, connected, websearch)
        for secret in SECRETS:
            assert secret not in json.dumps(f.requests), secret
        # A second submit on the same session must rebuild the guidance, not grow it.
        second = await f.send(prompt)
        check_guidance(second, prompt, mode, connected, websearch)
        await f.finish()
        tag = f'{mode}_{"ssh" if connected else "local"}_web{"on" if websearch else "off"}'
        report['combos'].append({'combo': tag, 'guidance_occurrences': system.count('Communication:'),
                                 'system_chars': len(system)})
        print('PASS guidance_' + tag, flush=True)


async def language_neutrality(browser, report):
    f = await Fixture().start(browser, mode='build', connected=True, websearch=True)
    system = (await f.send())['system']
    assert GUIDANCE['user_language'] in system, 'Guidance must require the user\'s language'
    lowered = system.lower()
    for token in HARDCODED_LANGUAGES:
        assert token not in lowered, f'Hardcoded language leaked into guidance: {token}'
    await f.finish()
    report['language_tokens_checked'] = len(HARDCODED_LANGUAGES)
    print('PASS language_neutral_user_language_requirement', flush=True)


async def silent_model_smoke(browser, report):
    context, page, errors, unexpected = await boot_page(browser)
    try:
        # Tools + reasoning, zero public text parts before the final answer.
        silent = message(A1, 'assistant',
                         [reason('r1', REASON_TEXT),
                          tool('t1', 'read', {'filePath': 'src/silent.txt'}, 'SILENT_FILE'),
                          text('a1', ANSWER)], U1, 9000)
        await seed(page, [user(), silent])
        wid = work_id(A1, 'r1')
        await settle(page)
        timeline = await main_sequence(page)
        assert timeline == ['user:' + U1, wid, 'text:' + ANSWER], timeline
        # No fabricated model chatter: the only .msg.ai is the final answer itself.
        await expect(page.locator('#main .msg.ai')).to_have_count(1)
        await expect(page.locator('#main .msg.ai')).to_have_attribute('data-raw', ANSWER)
        main_text = await page.locator('#main').inner_text()
        assert 'SILENT_MODEL_INTERNAL_REASONING_TOKEN' not in main_text
        assert 'SILENT_FILE' not in main_text
        # Honest summary: real tool facts, real status, no invented chatter or status.
        await expect(page.locator(summary(wid))).to_have_attribute('data-status', 'completed')
        label = await page.locator(summary(wid)).inner_text()
        assert read_counts(label) == [1], label
        assert 'Working' not in label and 'Failed' not in label and 'Stopped' not in label
        await open_work(page, wid)
        await assert_entries(page, [A1 + '_r1_think', A1 + '_t1_activity'])
        await close_work(page)
        await assert_main_clean(page)
        # A failed read must never be advertised as successfully read work.
        failing = message(A2, 'assistant',
                          [reason('r2', REASON_TEXT),
                           tool('t2', 'read', {'filePath': 'src/missing.txt'}, status='error',
                                error='ENOENT_SILENT_FIXTURE'),
                           text('a2', ANSWER)], U1, 9000)
        await seed(page, [user(), failing])
        wid2 = work_id(A2, 'r2')
        await settle(page)
        assert await main_sequence(page) == ['user:' + U1, wid2, 'text:' + ANSWER]
        await expect(page.locator('#main .msg.ai')).to_have_count(1)
        await expect(page.locator(summary(wid2))).to_have_attribute('data-status', 'error')
        label2 = await page.locator(summary(wid2)).inner_text()
        assert not any(read_counts(label2)), f'Failed read advertised as successful: {label2}'
        assert 'fail' in label2.lower(), f'Hidden failure has no summary signal: {label2}'
        assert not errors, errors
        assert not unexpected, unexpected
    finally:
        await context.close()
    report['silent_model'] = {'timeline': ['user', 'work-summary', 'final-text'],
                              'fabricated_msg_ai': 0, 'honest_counts': True}
    print('PASS silent_model_no_fabricated_chatter_honest_summary', flush=True)


async def main():
    expect.set_options(timeout=2500)
    report = {'combos': [], 'guidance_keys': len(GUIDANCE)}
    out = Path(tempfile.mkdtemp(prefix='commentary-prompt-proof-'))
    report['evidence'] = str(out)
    print(f'Evidence: {out}', flush=True)
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            try:
                await guidance_matrix(browser, report)
                await language_neutrality(browser, report)
                await silent_model_smoke(browser, report)
            except Exception:
                report['failure'] = traceback.format_exc()
                raise
        finally:
            await browser.close()
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f"PASS all {len(report['combos'])} submit combos (x2 sends each) "
          f"+ language neutrality + silent-model smoke; report: {out / 'report.json'}", flush=True)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()) or 0)
