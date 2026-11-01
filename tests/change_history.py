"""Real OpenCode + real SSH/SFTP + production Kotlin/JGit, driven through the UI."""
import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path
from uuid import uuid4

import asyncssh
from playwright.async_api import async_playwright
from history_support import ROOT, LOCAL, HistoryCore, compile_core
from mobile_features import Server, shell, PASSWORD, SSHROOT, wait_file
from history_provider import HistoryProvider


async def main():
    task = LOCAL / ('change-history-' + uuid4().hex[:8]); task.mkdir()
    project = task / 'project'; project.mkdir()
    existing = task / 'existing'; existing.mkdir()
    local_file = project / 'local.txt'
    local_file.write_text(''.join(f'LOCAL_CONTEXT_{i}\n' for i in range(40)) + 'LOCAL_OLD\n', 'utf-8', newline='\n')
    (project / 'user.txt').write_text('USER_STAGED_CONTENT\n', 'utf-8')
    remote_dir = SSHROOT / 'root'; remote_dir.mkdir()
    remote_file = remote_dir / 'test-2.txt'
    remote_original = ''.join(f'REMOTE_CONTEXT_{i}\n' for i in range(80))
    remote_file.write_text(remote_original, 'utf-8', newline='\n')
    report = {'checks': [], 'provider': 'local scripted OpenAI fixture'}; processes = []; core = None; output = None
    provider = HistoryProvider(local_file, local_file.read_text('utf-8'), remote_original)

    def check(name):
        report['checks'].append(name); print('PASS ' + name, flush=True)

    def git(directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    for args in [('init', '-q'), ('config', '--local', 'user.name', 'Existing User'),
                 ('config', '--local', 'user.email', 'existing@example.invalid')]:
        git(existing, *args)
    (existing / 'keep.txt').write_text('Existing user file\n', 'utf-8')
    git(existing, 'add', '--', 'keep.txt'); git(existing, 'commit', '-qm', 'Existing commit')
    old_head = git(existing, 'rev-parse', 'HEAD')
    server = await asyncssh.create_server(Server, '127.0.0.1', 0,
        server_host_keys=[asyncssh.generate_private_key('ssh-rsa', key_size=2048)],
        process_factory=shell, sftp_factory=lambda ch: asyncssh.SFTPServer(ch, chroot=str(SSHROOT)))
    try:
        core = await HistoryCore.start(compile_core(), [project, existing])
        assert (await core.request('historyRegister', project)).get('registered')
        assert git(project, 'ls-tree', '-r', '--name-only', 'HEAD') == ''
        assert git(project, 'config', '--local', 'user.name') == 'Codex Mobile'
        git(project, 'add', '--', 'user.txt')
        original_index = hashlib.sha256((project / '.git/index').read_bytes()).hexdigest()
        initial_head = git(project, 'rev-parse', 'HEAD')
        await core.request('historyRegister', existing)
        assert git(existing, 'rev-parse', 'HEAD') == old_head
        assert git(existing, 'config', '--local', 'user.name') == 'Existing User'
        assert git(existing, 'config', '--local', 'user.email') == 'existing@example.invalid'
        check('git_init_without_user_file_snapshots_existing_config_preserved')
        assert 'error' in await core.request('historyRegister', task)
        assert 'error' in await core.request('historyRecord', project, sessionID='ses_probe', turns=[{
            'id': 'msg_probe', 'files': [{'source': 'local', 'file': '../escape.txt',
                'patch': '--- /dev/null\n+++ ../escape.txt\n@@ -0,0 +1,1 @@\n+outside\n'}]}])
        check('unregistered_directories_and_path_traversal_rejected')
        cp = str(LOCAL / 'ssh-test-classes') + os.pathsep + str(LOCAL / 'ssh-test-libs/*')
        ready = task / 'service.json'
        jvm = subprocess.Popen(['java', '-cp', cp, 'MobileSshHarness', str(ready)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); processes.append(jvm)
        service = await wait_file(ready, jvm)
        env = dict(os.environ)
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'mcp': {'mobile_ssh': {
            'type': 'remote', 'url': service['url'], 'oauth': False, 'timeout': 300000,
            'headers': {'Authorization': 'Bearer ' + service['token']}}},
            'permission': {'mobile_ssh_ssh_exec': 'ask', 'mobile_ssh_ssh_write_file': 'ask', 'edit': 'ask'},
            'provider': {'history-probe': {'npm': '@ai-sdk/openai-compatible',
                'options': {'baseURL': provider.url, 'apiKey': 'local-test-only'},
                'models': {'history-probe': {'name': 'History tool probe',
                    'limit': {'context': 32000, 'output': 8000},
                    'modalities': {'input': ['text'], 'output': ['text']}}}}}})
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        output = (task / 'opencode.log').open('w', encoding='utf-8')
        runtime = subprocess.Popen(['D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe',
            'serve', '--hostname', '127.0.0.1', '--port', str(port)], env=env, cwd=project,
            stdout=output, stderr=output); processes.append(runtime)
        origin = f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                with urllib.request.urlopen(origin + '/global/health', timeout=2) as response:
                    health = json.load(response)
                break
            except Exception:
                await asyncio.sleep(.25)
        assert health['healthy'] and health['version'] == '1.19.3'
        for _ in range(80):
            with urllib.request.urlopen(origin + '/mcp', timeout=15) as response:
                mcp = json.load(response)
            if mcp.get('mobile_ssh', {}).get('status') == 'connected':
                break
            await asyncio.sleep(.25)
        assert mcp.get('mobile_ssh', {}).get('status') == 'connected', 'Real MCP service did not connect'
        report['serverVersion'] = health['version']
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={'width': 424, 'height': 900})
            await page.context.grant_permissions(['local-network-access'], origin=origin)
            errors = []; page.on('pageerror', lambda error: errors.append(type(error).__name__))

            async def history_request(raw):
                request = json.loads(raw); data = request['data']; directory = data.pop('directory')
                return await core.request(request['action'], directory, **data)

            await page.expose_function('testHistoryRequest', history_request)
            await page.add_init_script('''(() => {
              const service = ''' + json.dumps({'url': f"http://127.0.0.1:{service['port']}",
                                                'token': service['token'], 'project': str(project)}) + ''';
              window.CodexNative={request(raw){const q=JSON.parse(raw);
                if(q.action==='projects'){window.codexNativeResult(q.id,{projects:[service.project]});return}
                if(['appearance','selectProject'].includes(q.action)){window.codexNativeResult(q.id,{});return}
                const request=q.action.startsWith('history')?window.testHistoryRequest(raw):fetch(service.url+'/bridge',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+service.token},body:raw}).then(r=>r.json());
                request.then(value=>window.codexNativeResult(q.id,value)).catch(()=>window.codexNativeResult(q.id,{error:'Test transport failed'}));
              }};
              window.addEventListener('DOMContentLoaded',()=>{(async()=>{
                const response=await fetch(service.url+'/events',{headers:{Authorization:'Bearer '+service.token}}),reader=response.body.getReader();
                const decode=new TextDecoder();let buffer='';while(true){const v=await reader.read();if(v.done)break;buffer+=decode.decode(v.value,{stream:true});let end;while((end=buffer.indexOf('\\n\\n'))>=0){const line=buffer.slice(0,end);buffer=buffer.slice(end+2);if(line.startsWith('data: '))window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:JSON.parse(line.slice(6))}));}}
              })().catch(()=>{});});
            })();''')
            await page.route('**/_ui/index.html', lambda route: route.fulfill(
                path=str(ROOT / 'app/src/main/assets/codex.html'), content_type='text/html; charset=utf-8'))
            encoded = base64.urlsafe_b64encode(str(project).encode()).decode().rstrip('=')
            await page.goto(origin + '/_ui/index.html#d=' + encoded)
            await page.wait_for_function('S.live && S.provs.length')
            await page.click('#cModel'); models = await page.evaluate('allModels()')
            index = next(i for i, m in enumerate(models) if m['p'] == 'history-probe' and m['m'] == 'history-probe')
            await page.locator('#sheet .opt').nth(index).click()
            await page.click('#cEnv'); await page.click('#sa')
            for selector, value in [('#sn', 'Test SSH'), ('#sh', '127.0.0.1'), ('#sp', str(server.get_port())),
                                    ('#su', 'mobile'), ('#sd', '/root'), ('#sw', PASSWORD)]:
                await page.fill(selector, value)
            await page.click('#ss'); await page.locator('#st').wait_for(timeout=30000); await page.click('#st')
            await page.wait_for_function('S.connections.length===1', timeout=30000)

            async def send(text):
                await page.fill('#inp', text); await page.click('#send')
                approvals = 0
                for _ in range(360):
                    if await page.locator('[data-r="once"]').count():
                        await page.locator('[data-r="once"]').first.click(); approvals += 1
                    status = await page.evaluate('({running:S.running,sending:S.sending,retry:S.status.type===\'retry\',rate:/429|rate.?limit/i.test(S.status.message||\'\')})')
                    if status['retry'] and status['rate']:
                        await page.click('#send'); raise RuntimeError('Provider rate limit; stopped')
                    if not status['running'] and not status['sending']:
                        break
                    await asyncio.sleep(.5)
                assert not await page.evaluate('S.running || S.sending'), 'Agent did not finish'
                report['lastAgent']=await page.evaluate("({tools:[...S.msgs.values()].flatMap(m=>[...m.parts.values()]).filter(p=>p.type==='tool').map(p=>({name:p.tool,status:p.state.status})),errorNames:[...S.msgs.values()].map(m=>m.info.error?.name).filter(Boolean),hasClientError:!!S.lastErr})")
                report['lastAgent']['approvals']=approvals
                assert approvals >= 1, 'Write happened without approval'
                await page.evaluate('persistHistory(S.dir,S.sid)')

            await send('Read /root/test-2.txt using mobile_ssh_ssh_read_file. Use mobile_ssh_ssh_write_file to append exactly REMOTE_ONE followed by a newline. '
                       'Keep all existing lines unchanged. Do not use shell commands or local tools. Reply SSH_ONE_DONE.')
            assert remote_file.read_text('utf-8') == remote_original + 'REMOTE_ONE\n'
            check('real_agent_ssh_edit_committed_from_tool_patch')
            await page.click('#cEnv'); await page.click('[data-connection=""]')
            await send('First read local.txt. Use the write tool to replace LOCAL_OLD with LOCAL_NEW in that same file. '
                       'Keep every other line exactly unchanged. Do not create another file. Reply LOCAL_DONE.')
            assert local_file.read_text('utf-8').endswith('LOCAL_NEW\n')
            tools = await page.evaluate("[...S.msgs.values()].flatMap(m=>[...m.parts.values()]).filter(p=>p.type==='tool').map(p=>({name:p.tool,status:p.state.status}))")
            assert any(t['name'] == 'write' and t['status'] == 'completed' for t in tools)
            check('real_local_write_uses_approved_diff_not_whole_file')
            await page.click('#cEnv'); await page.locator('[data-connection]').filter(has_text='Test SSH').click()
            await send('Read /root/test-2.txt using mobile_ssh_ssh_read_file. Use mobile_ssh_ssh_write_file to append exactly REMOTE_TWO followed by a newline. '
                       'Keep all existing lines unchanged. Do not use shell commands. Reply SSH_TWO_DONE.')
            assert remote_file.read_text('utf-8') == remote_original + 'REMOTE_ONE\nREMOTE_TWO\n'
            sid = await page.evaluate('S.sid'); branch = 'refs/codex/changes/' + sid
            assert git(project, 'rev-list', '--count', branch) == '3'
            entries = (await core.request('historyList', project, sessionID=sid))['turns']
            assert len(entries) == 3
            for entry in entries:
                shown = await core.request('historyShow', project, sessionID=sid, commit=entry['commit'])
                paths = git(project, 'ls-tree', '-r', '--name-only', entry['commit']).splitlines()
                assert paths and all(p == 'turn.json' or p.startswith('patches/') for p in paths)
                for file in shown['files']:
                    assert len(file['patch'].splitlines()) < 15
                    assert file['additions'] == 1
                    assert file['deletions'] == (1 if file['source'] == 'local' else 0)
                    assert 'CONTEXT_0\n' not in file['patch']
            latest = await core.request('historyShow', project, sessionID=sid, commit=entries[0]['commit'])
            assert '+REMOTE_TWO\n' in latest['files'][0]['patch'] and '+REMOTE_ONE\n' not in latest['files'][0]['patch']
            assert git(project, 'rev-parse', 'HEAD') == initial_head
            assert hashlib.sha256((project / '.git/index').read_bytes()).hexdigest() == original_index
            assert (project / 'user.txt').read_text('utf-8') == 'USER_STAGED_CONTENT\n'
            assert 'error' in await core.request('historyShow', project, sessionID=sid, commit=initial_head)
            check('three_turn_commits_changed_hunks_only_user_head_and_index_untouched')
            await page.click('#commit'); await page.locator('#sheet .change-card').first.wait_for()
            assert await page.locator('#sheet .change-turn').count() == 3
            assert await page.locator('#sheet .change-card[open]').count() == 0
            assert 'local.txt' in await page.locator('#sheet').inner_text()
            assert '/root/test-2.txt' in await page.locator('#sheet').inner_text()
            await page.screenshot(path=str(task / 'changes-collapsed.png'))
            row = page.locator('#sheet .change-card').filter(has_text='local.txt')
            assert '+1 −1' in await row.locator('.counts').inner_text()
            await row.locator('summary').click()
            await page.wait_for_function("[...document.querySelectorAll('#sheet .change-preview')].some(e=>e.textContent.includes('+LOCAL_NEW'))")
            await page.screenshot(path=str(task / 'changes-expanded.png'))
            check('changes_shows_local_and_ssh_edits_compact_counts_expand_inline')
            await page.reload(); await page.wait_for_function('S.live && S.provs.length')
            await page.evaluate('id=>openSession(id)', sid)
            await page.evaluate('persistHistory(S.dir,S.sid)')
            local_card=page.locator('#main details.change-card').filter(has_text='local.txt').first
            assert '+1 −1' in await local_card.locator('.counts').inner_text()
            assert await local_card.locator('.change-preview').inner_text() == ''
            await page.click('#commit'); await page.locator('#sheet .change-card').first.wait_for()
            assert await page.locator('#sheet .change-turn').count() == 3
            assert git(project, 'rev-list', '--count', branch) == '3'
            check('history_survives_reload_without_duplicate_commits')
            assert not errors; report['pageErrors'] = errors
            await browser.close()
        report['pass'] = True
    except Exception as error:
        import traceback
        report['pass'] = False; report['failure'] = type(error).__name__
        report['failureAt'] = [f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(error.__traceback__)]
        if isinstance(error, RuntimeError) and 'rate limit' in str(error).lower():
            report['rateLimit'] = True
    finally:
        server.close(); await server.wait_closed()
        for process in reversed(processes):
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill()
        if output: output.close()
        if core: await core.close()
        provider.close()
    (task / 'evidence.json').write_text(json.dumps(report, indent=2), 'utf-8')
    (LOCAL / 'change-history-last.txt').write_text(str(task), 'utf-8')
    print(json.dumps(report)); return report.get('pass', False)


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
