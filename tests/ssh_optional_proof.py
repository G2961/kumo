"""Offline SSH-option regression against the actual HTML submit handler.

Run: ../.local/api-test-venv/Scripts/python.exe tests/ssh_optional_proof.py
Set PLAYWRIGHT_BROWSER_EXECUTABLE or CHROME_EXECUTABLE for an installed Chrome.
Only boot/network/native transport are fixtures; submit, policy restoration,
UI events and nativeCall are production code. No model, SSH or build is run.
"""
import asyncio
import copy
import fnmatch
import json
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect
from reasoning_activity_proof import boot_page, launch_browser

SID = 'ses_optional_ssh'
LOCAL = '/storage/emulated/0/Projects/local-only-project'
REMOTE = '/srv/services/not-a-project-mirror'
CONNECTION = {'id': 'conn_optional', 'name': 'Optional fixture',
              'host': 'ssh.invalid', 'directory': REMOTE,
              # Deliberate canaries: submit must whitelist metadata, not serialize a connection.
              'password': 'PASSWORD_CANARY', 'privateKey': 'PRIVATE_KEY_CANARY',
              'passphrase': 'PASSPHRASE_CANARY'}
LOCAL_TOOLS = ['bash', 'read', 'write', 'edit', 'apply_patch', 'glob', 'grep', 'task']
SSH_TOOLS = ['mobile_ssh_' + name for name in
             ['ssh_connections', 'ssh_exec', 'ssh_read_file', 'ssh_write_file']]
WRITES = ['write', 'edit', 'apply_patch']


def rule(permission, action, pattern='*'):
    return {'permission': permission, 'pattern': pattern, 'action': action}


def effective(rules, permission, pattern):
    """Session rules use last matching rule, not the first matching tool name."""
    matches = [r for r in rules if fnmatch.fnmatchcase(permission, r['permission'])
               and fnmatch.fnmatchcase(pattern, r['pattern'])]
    assert matches, f'Missing restored permission for {permission} {pattern}'
    return matches[-1]['action']


def old_remote_rules(prefix=()):
    return [*prefix, rule('websearch', 'deny'),
            *[rule(t, 'ask' if t.endswith(('ssh_exec', 'ssh_write_file')) else 'allow')
              for t in SSH_TOOLS], *[rule(t, 'deny') for t in LOCAL_TOOLS]]


class Fixture:
    async def start(self, browser, *, mode='build', connected=True, config=None,
                    previous=(), cached=None, fresh=False, patch_fails=False):
        self.context, self.page, self.errors, self.unexpected = await boot_page(browser)
        self.config = copy.deepcopy(config or {})
        self.permission = copy.deepcopy(list(previous))
        self.requests = []
        self.native_errors = []
        self.patch_fails = patch_fails

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
                await route.fulfill(json=self.config)
            elif path == '/session' and method == 'POST':
                await route.fulfill(json={'id': SID, 'title': 'Offline test'})
            elif path == '/session/' + SID and method == 'GET':
                await route.fulfill(json={'id': SID, 'permission': self.permission})
            elif path == '/session/' + SID and method == 'PATCH':
                if 'permission' in body:
                    if self.patch_fails:
                        await route.fulfill(status=500, json={'error': 'Fixture policy failure'})
                        return
                    self.permission = copy.deepcopy(body['permission'])
                await route.fulfill(json={'id': SID, **body})
            elif path == '/session/' + SID + '/prompt_async' and method == 'POST':
                await route.fulfill(status=204)
            else:
                self.unexpected.append(method + ' ' + request.url)
                await route.abort()

        # Page routes override boot_page's deny-by-default context routes.
        await self.page.route('**/*', route_request)
        await self.page.evaluate('''({sid,dir,mode,connection,connected,cached,fresh})=>{
          S.dir=dir;S.sid=fresh?null:sid;S.agent=mode;S.model=null;S.efforts={};
          S.webSearch=false;S.localPolicies={};S.running=false;S.sending=false;
          S.connections=[connection];S.connectionID=connected?connection.id:null;
          if(cached!==null)S.localPolicies[dir+'\\n'+sid]=cached;
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
               'connected': connected, 'cached': cached, 'fresh': fresh})
        return self

    async def send(self, prompt='Inspect and fix the selected local project.'):
        start = len(self.requests)
        await self.page.fill('#inp', prompt)
        if self.patch_fails:
            await self.page.click('#send')
            await self.page.wait_for_function('!S.sending && S.lastErr')
            assert not any(r['path'].endswith('/prompt_async') for r in self.requests)
            return None
        async with self.page.expect_response(lambda r: urlsplit(r.url).path.endswith('/prompt_async')):
            await self.page.click('#send')
        await self.page.wait_for_function('!S.sending')
        # Stop only the polling watchdog between fixture turns; do not replace submit.
        await self.page.evaluate('S.running=false;setRun()')
        sent = self.requests[start:]
        patches = [r for r in sent if r['method'] == 'PATCH' and 'permission' in r['body']]
        prompts = [r for r in sent if r['path'].endswith('/prompt_async')]
        assert len(patches) == len(prompts) == 1
        assert sent.index(patches[0]) < sent.index(prompts[0])
        body = prompts[0]['body']
        assert 'tools' not in body, 'Deprecated tools would silently overwrite approvals'
        assert body['parts'] == [{'type': 'text', 'text': prompt}]
        assert LOCAL in body['system'] and 'primary workspace' in body['system']
        assert 'local file, search and shell tools' in body['system']
        assert 'earlier messages used SSH-only instructions' in body['system']
        assert 'Do not run local shell' not in body['system']
        for secret in ('PASSWORD_CANARY', 'PRIVATE_KEY_CANARY', 'PASSPHRASE_CANARY'):
            assert secret not in json.dumps(self.requests)
            assert secret not in await self.page.evaluate('JSON.stringify(localStorage)')
        return body

    async def finish(self):
        assert not self.errors, self.errors
        assert not self.unexpected, self.unexpected
        assert not await self.page.evaluate('nativeErrors')
        assert all(r['method'] == 'GET' for r in self.requests if 'config' in r['path'])
        await self.context.close()


def check_local(rules, mode):
    for tool in LOCAL_TOOLS:
        expected = ('deny' if mode == 'plan' else 'ask' if mode == 'build' else 'allow') if tool in WRITES else 'allow'
        assert effective(rules, tool, 'src/main.js') == expected, (mode, tool, rules)
    for path in ['.env', 'src/.env', 'config/prod.env', '.env.production']:
        assert effective(rules, 'read', path) == 'ask', (mode, path)
    assert effective(rules, 'read', '.env.example') == 'allow'


def check_ssh(rules, mode, connected):
    for tool in SSH_TOOLS:
        expected = 'allow'
        if not connected or (mode == 'plan' and tool.endswith(('ssh_exec', 'ssh_write_file'))):
            expected = 'deny'
        elif mode == 'build' and tool.endswith(('ssh_exec', 'ssh_write_file')):
            expected = 'ask'
        assert effective(rules, tool, '*') == expected, (mode, connected, tool, rules)


async def mode_matrix(browser):
    for mode in ['build', 'plan', 'auto']:
        local = None
        for connected in [False, True]:
            f = await Fixture().start(browser, mode=mode, connected=connected, fresh=True)
            body = await f.send('Deploy using SSH if server access is required; start with the local project.')
            assert body['agent'] == ('plan' if mode == 'plan' else 'build')
            check_local(f.permission, mode)
            check_ssh(f.permission, mode, connected)
            native = await f.page.evaluate('nativeRequests')
            bindings = [r['data'] for r in native if r['action'] == 'sessionContext']
            assert bindings == ([{'sessionID': SID, 'connectionID': CONNECTION['id']}] if connected else [])
            assert [r['data'] for r in native if r['action'] == 'historyRegister'] == [{'directory': LOCAL}]
            if connected:
                assert REMOTE in body['system'] and 'separate from the local project directory' in body['system']
                assert 'user requests it or the task requires server access' in body['system']
                assert 'Do not assume the project is mirrored' in body['system']
                assert 'or run a remote filesystem-wide search for the local project' in body['system']
                assert 'ask the user for its path' in body['system']
                assert 'session_id=' + SID in body['system']
                assert 'connection_id=' + CONNECTION['id'] in body['system']
                assert 'Never expose credentials' in body['system']
                assert [r for r in f.permission if r['permission'] in LOCAL_TOOLS] == local
            else:
                assert 'No SSH connection is selected' in body['system'] and REMOTE not in body['system']
                local = [r for r in f.permission if r['permission'] in LOCAL_TOOLS]
            await f.finish()
            print(f'PASS submit_{mode}_{"optional_ssh" if connected else "local"}', flush=True)


async def recover_old_sessions(browser):
    prefix = [rule('read', 'deny', 'private/*'), rule('bash', 'deny', 'rm *'),
              rule('external_directory', 'ask')]
    stale = old_remote_rules(prefix)
    for cache in [None, stale]:
        f = await Fixture().start(browser, previous=stale, cached=cache)
        await f.send()
        check_local(f.permission, 'build')
        assert effective(f.permission, 'read', 'private/key') == 'deny'
        assert effective(f.permission, 'bash', 'rm file') == 'deny'
        assert rule('external_directory', 'ask') in f.permission
        assert await f.page.evaluate('S.localPolicies[S.dir+"\\n"+S.sid]') == prefix
        # Simulate reload losing the cache: generated rules must not become baseline.
        await f.page.evaluate("S.localPolicies={};S.connectionID=null;S.agent='plan';chips()")
        await f.send()
        check_local(f.permission, 'plan')
        check_ssh(f.permission, 'plan', False)
        await f.page.evaluate("S.localPolicies={};S.connectionID=S.connections[0].id;S.agent='build';chips()")
        await f.send()
        check_local(f.permission, 'build')
        check_ssh(f.permission, 'build', True)
        assert effective(f.permission, 'read', 'private/key') == 'deny'
        await f.finish()
        print('PASS stale_remote_policy_' + ('cached' if cache else 'server') + '_and_mode_transitions', flush=True)


async def configured_guardrails(browser):
    # Native runtime asks for SSH mutations; auto is the only opt-in override.
    for configured_action in ['allow', 'ask']:
        for mode in ['build', 'plan', 'auto']:
            config = {'permission': {tool: configured_action for tool in SSH_TOOLS}}
            f = await Fixture().start(browser, mode=mode, config=config)
            await f.send()
            for tool in ['mobile_ssh_ssh_exec', 'mobile_ssh_ssh_write_file']:
                assert effective(f.permission, tool, '*') == {'build': 'ask', 'plan': 'deny', 'auto': 'allow'}[mode]
            await f.page.evaluate('S.connectionID=null;chips()')
            await f.send()
            check_ssh(f.permission, mode, False)
            await f.finish()
            print(f'PASS_configured_ssh_{configured_action}_{mode}_and_deselect', flush=True)

    config = {'permission': {
        'bash': {'*': 'allow', 'rm *': 'deny'}, 'read': {'secrets/*': 'deny'},
        'edit': {'protected/*': 'deny'}, 'write': 'deny', 'apply_patch': 'deny',
        'task': {'dangerous-agent': 'deny'}, 'mobile_ssh_ssh_exec': {'rm *': 'deny'},
        'mobile_ssh_ssh_write_file': 'deny'},
        'agent': {'build': {'permission': {'glob': 'deny', 'read': {'agent-private/*': 'deny'}}}}}
    baseline = [rule('grep', 'deny', 'credentials/*'), rule('external_directory', 'deny'),
                rule('websearch', 'ask'), rule('mobile_ssh_ssh_connections', 'ask')]
    for mode in ['build', 'plan', 'auto']:
        f = await Fixture().start(browser, mode=mode, config=config, previous=baseline)
        await f.send()
        for tool, path in [('bash', 'rm /tmp/file'), ('read', 'secrets/key'), ('write', 'src/file'),
                           ('edit', 'protected/file'), ('apply_patch', '*'), ('task', 'dangerous-agent'),
                           ('grep', 'credentials/password'), ('external_directory', '/other'),
                           ('mobile_ssh_ssh_exec', 'rm /srv/file'), ('mobile_ssh_ssh_write_file', '/srv/file')]:
            assert effective(f.permission, tool, path) == 'deny', (mode, tool, path)
        assert effective(f.permission, 'read', '.env.production') == 'ask'
        assert effective(f.permission, 'read', 'src/main.js') == 'allow'
        if mode != 'plan':
            assert effective(f.permission, 'glob', '*') == 'deny'
            assert effective(f.permission, 'read', 'agent-private/key') == 'deny'
        # Two coincidental names are not enough to identify an app-generated suffix.
        assert await f.page.evaluate('S.localPolicies[S.dir+"\\n"+S.sid]') == baseline
        await f.finish()
        print('PASS configured_denies_and_sensitive_reads_' + mode, flush=True)


async def auto_local_write_approvals(browser):
    config = {'permission': {
        **{tool: {'*': 'ask', 'protected/*': 'deny'} for tool in WRITES},
        'read': {'sensitive/*': 'ask', 'secrets/*': 'deny'},
        'bash': 'ask'}}
    local_rules = None
    for connected in [False, True]:
        f = await Fixture().start(browser, mode='auto', connected=connected, config=config)
        body = await f.send()
        assert body['agent'] == 'build'
        for tool in WRITES:
            assert effective(f.permission, tool, 'src/main.js') == 'allow', (connected, tool)
            assert effective(f.permission, tool, 'protected/main.js') == 'deny', (connected, tool)
        for path in ['.env', 'src/.env', '.env.production', 'sensitive/key']:
            assert effective(f.permission, 'read', path) == 'ask', (connected, path)
        assert effective(f.permission, 'read', 'secrets/key') == 'deny'
        assert effective(f.permission, 'read', '.env.example') == 'allow'
        assert effective(f.permission, 'bash', 'git status') == 'ask'
        check_ssh(f.permission, 'auto', connected)
        restored = [r for r in f.permission if r['permission'] in LOCAL_TOOLS]
        if connected:
            assert restored == local_rules
        else:
            local_rules = restored
        await f.finish()
        print(f'PASS auto_local_write_ask_override_{"optional_ssh" if connected else "local"}', flush=True)


async def ui_and_failure(browser):
    f = await Fixture().start(browser)
    await expect(f.page.locator('#cEnv')).to_have_text('Local · SSH available')
    await expect(f.page.locator('#cEnv')).to_have_attribute('aria-label', 'Local + optional SSH: Optional fixture')
    await f.page.click('#cEnv')
    await expect(f.page.locator('#sheet')).to_contain_text('selected local project stays primary')
    await expect(f.page.locator('#sheet')).to_contain_text('SSH is optional')
    await f.page.locator('[data-connection=""]').click()
    await expect(f.page.locator('#cEnv')).to_have_text('Local')
    assert await f.page.evaluate('S.dir') == LOCAL
    await f.page.click('#cEnv')
    await f.page.locator('[data-connection="conn_optional"]').click()
    await expect(f.page.locator('#cEnv')).to_have_text('Local · SSH available')
    assert await f.page.evaluate('S.dir') == LOCAL
    await f.page.evaluate('connectionForm({})')
    await expect(f.page.locator('label[for="sd"]')).to_have_text('Remote working directory')
    await f.page.evaluate('closeAll();S.connections[0].directory="";S.webSearch=true;chips()')
    body = await f.send()
    assert '(remote home directory)' in body['system']
    assert 'Use the websearch tool' in body['system']
    assert effective(f.permission, 'websearch', '*') == 'allow'
    await f.finish()
    print('PASS_optional_selector_remote_directory_and_websearch', flush=True)

    f = await Fixture().start(browser, patch_fails=True)
    await f.send()
    assert not await f.page.evaluate('S.running')
    await f.finish()
    print('PASS_no_prompt_when_permission_patch_fails', flush=True)


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            await mode_matrix(browser)
            await recover_old_sessions(browser)
            await configured_guardrails(browser)
            await auto_local_write_approvals(browser)
            await ui_and_failure(browser)
        finally:
            await browser.close()
    print('PASS all 21 offline SSH-option scenarios (31 actual submits)', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
