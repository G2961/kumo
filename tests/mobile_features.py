"""Real opencode + compiled SSH/MCP service + a real local SSH/SFTP server.

Android's JS bridge uses HTTP here; the SSH/MCP implementation is the same Java
source shipped in the APK. Android Keystore/SAF still require a phone check.
No provider headers, credentials, complete provider errors, or file bodies logged.
"""
import asyncio,base64,codecs,json,os,re,struct,subprocess,zlib
from urllib.parse import urlsplit
from pathlib import Path
from uuid import uuid4
import asyncssh
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT.parent/'.local'
TASK=LOCAL/('mobile-integration-'+uuid4().hex[:8])
TASK.mkdir()
SSHROOT=TASK/'server-files';SSHROOT.mkdir()
PASSWORD=os.urandom(20).hex()
PASSPHRASE=os.urandom(20).hex()
USERKEY=asyncssh.generate_private_key('ssh-ed25519')

class Server(asyncssh.SSHServer):
    def begin_auth(self,user):return True
    def password_auth_supported(self):return True
    def public_key_auth_supported(self):return True
    def validate_password(self,user,password):return user=='mobile' and password==PASSWORD
    def validate_public_key(self,user,key):return user=='mobile' and key==USERKEY.convert_to_public()

async def shell(process):
    child=await asyncio.create_subprocess_exec('C:/Program Files/Git/bin/bash.exe','-c',process.command,cwd=SSHROOT,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def pipe(source,target):
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        while chunk:=await source.read(4096):target.write(decoder.decode(chunk))
        rest=decoder.decode(b'',True)
        if rest:target.write(rest)
    try:await asyncio.gather(pipe(child.stdout,process.stdout),pipe(child.stderr,process.stderr));process.exit(await child.wait())
    except Exception:child.kill();await child.wait()

async def wait_file(path,process):
    for _ in range(120):
        if path.exists():return json.loads(path.read_text('utf-8-sig'))
        if process.poll() is not None:raise RuntimeError('Service stopped during startup')
        await asyncio.sleep(.25)
    raise TimeoutError('Service startup')

def png():
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*16)*16))+chunk(b'IEND',b'')

async def main():
    result={'checks':[]};processes=[]
    def checkpoint(name):
        result['checks'].append(name);print('PASS '+name,flush=True)
    ssh_server=await asyncssh.create_server(Server,'127.0.0.1',0,server_host_keys=[asyncssh.generate_private_key('ssh-rsa',key_size=2048)],process_factory=shell,sftp_factory=lambda ch:asyncssh.SFTPServer(ch,chroot=str(SSHROOT)))
    ssh_port=ssh_server.get_port()
    try:
        libs=LOCAL/'ssh-test-libs';classes=LOCAL/'ssh-test-classes'
        cp=str(classes)+';'+str(libs/'*')
        subprocess.run(['javac','--release','17','-cp',str(libs/'jsch.jar')+';'+str(libs/'json.jar')+';'+str(libs/'bcprov.jar'),'-d',str(classes),str(ROOT/'app/src/main/java/space/g2961/opencode/RemoteSsh.java'),str(ROOT/'app/src/main/java/space/g2961/opencode/SshToolsServer.java'),str(ROOT/'tests/MobileSshHarness.java')],check=True,capture_output=True)
        ready=TASK/'ssh-service.json'
        jvm=subprocess.Popen(['java','-cp',cp,'MobileSshHarness',str(ready)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);processes.append(jvm)
        service=await wait_file(ready,jvm)
        import urllib.request
        # Capability/variant definitions come from the actual opencode model registry.
        with urllib.request.urlopen('http://127.0.0.1:58074/provider',timeout=15) as r:providers=json.load(r)['all']
        known=next(m for p in providers for m in p.get('models',{}).values() if m['id']=='gpt-5.6-sol' and m.get('capabilities',{}).get('reasoning') and m.get('api',{}).get('npm')=='@ai-sdk/openai')
        model={'reasoning':known['capabilities']['reasoning'],'attachment':known['capabilities']['attachment'],'variants':known.get('variants',{}),'modalities':{'input':[k for k,v in known['capabilities']['input'].items() if v],'output':['text']}}
        env=dict(os.environ);env['OPENCODE_ENABLE_EXA']='true'
        env['OPENCODE_CONFIG_CONTENT']=json.dumps({'provider':{'AgentRouter-gpt-responses-gateway':{'models':{'gpt-5.6-sol':model}}},'mcp':{'mobile_ssh':{'type':'remote','url':service['url'],'oauth':False,'timeout':300000,'headers':{'Authorization':'Bearer '+service['token']}}},'permission':{'mobile_ssh_ssh_exec':'ask','mobile_ssh_ssh_write_file':'ask'}})
        import socket
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        out=open(TASK/'opencode.log','w',encoding='utf-8')
        opencode=subprocess.Popen(['D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe','serve','--hostname','127.0.0.1','--port',str(port)],env=env,cwd=ROOT,stdout=out,stderr=out);processes.append(opencode)
        origin=f'http://127.0.0.1:{port}'
        for _ in range(80):
            try:
                with urllib.request.urlopen(origin+'/global/health',timeout=2) as r:health=json.load(r)
                break
            except Exception:await asyncio.sleep(.25)
        assert health['healthy'] and health['version']=='1.19.3'
        key_file=TASK/'client-key.pem';key_file.write_bytes(USERKEY.export_private_key('openssh',passphrase=PASSPHRASE))
        project1=TASK/'project-one';project2=TASK/'project-two';project1.mkdir();project2.mkdir()
        note=TASK/'note.txt';note.write_text('ATTACHMENT_FILE_OK',encoding='utf-8')
        image=TASK/'image.png';image.write_bytes(png())
        async with async_playwright() as pw:
            browser=await pw.chromium.launch();page=await browser.new_page(viewport={'width':424,'height':900})
            # The intercepted HTML has no network address classification. Chromium
            # requires this permission for the desktop bridge's loopback HTTP/SSE.
            await page.context.grant_permissions(['local-network-access'],origin=origin)
            errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
            bridge_http=[]
            network_errors=[]
            def console_error(message):
                if message.type=='error':
                    value=message.text
                    network_errors.append({'headers':re.findall(r'Access-Control-[A-Za-z-]+',value),'indicators':[s for s in ['CORS','preflight','local network','Local Network','Failed to fetch','ERR_FAILED','Content Security Policy','denied','not allowed'] if s in value],**({'corsReason':re.sub(r'https?://[^\s\"\']+','<url>',value.split('CORS policy:',1)[1])} if 'CORS policy:' in value else {})})
            page.on('console',console_error)
            page.on('response',lambda r:bridge_http.append({'path':urlsplit(r.url).path,'status':r.status}) if urlsplit(r.url).path in ['/bridge','/events'] else None)
            page.on('requestfailed',lambda r:bridge_http.append({'path':urlsplit(r.url).path,'failure':r.failure}) if urlsplit(r.url).path in ['/bridge','/events'] else None)
            await page.route('**/_ui/index.html',lambda route:route.fulfill(path=str(ROOT/'app/src/main/assets/codex.html'),content_type='text/html; charset=utf-8'))
            encoded=base64.urlsafe_b64encode(str(project1).encode()).decode().rstrip('=')
            await page.goto(origin+'/_ui/index.html#d='+encoded)
            await page.wait_for_function('S.spec && S.live && S.provs.length')
            if '--ssh-only' in __import__('sys').argv:
                await page.click('#cModel'); models=await page.evaluate('allModels()')
                index=next(i for i,m in enumerate(models) if m['p']=='AgentRouter-gpt-responses-gateway' and m['m']=='gpt-5.6-sol')
                await page.locator('#sheet .opt').nth(index).click()
            else:
                await page.click('#menu');await page.locator('[data-a="add"]').click();await page.fill('#projectPath',str(project2));await page.click('#projectAdd')
                await page.wait_for_function('d=>S.dir===d',arg=str(project2));await page.reload();await page.wait_for_function('S.live && S.projects.length>=2')
                paths=await page.evaluate('S.projects.map(p=>p.worktree)');assert str(project1) in paths and str(project2) in paths
                checkpoint('multiple_projects_persist_after_reload')
                await page.click('#gear');assert 'Server check' not in await page.locator('#sheet').inner_text()
                await page.click('#ap');await page.click('[data-theme="dark"]');assert await page.evaluate("getComputedStyle(document.body).backgroundColor")=='rgb(0, 0, 0)'
                await page.locator('#fontScale').evaluate("e=>{e.value='125';e.dispatchEvent(new Event('input'))}")
                assert await page.evaluate("getComputedStyle(document.body).fontSize")=='18.75px'
                await page.reload();await page.wait_for_function('S.live && S.provs.length');assert await page.evaluate("document.documentElement.dataset.theme")=='dark'
                checkpoint('dark_theme_and_font_scale_persist')
                await page.evaluate("lset('oc_theme','light');lset('oc_font_scale',1);appearance()")
                await page.click('#cModel');models=await page.evaluate('allModels()');index=next(i for i,m in enumerate(models) if m['p']=='AgentRouter-gpt-responses-gateway' and m['m']=='gpt-5.6-sol');await page.locator('#sheet .opt').nth(index).click()
                await page.click('#cEff');await page.locator('#sheet .opt').nth(1).click()
                variant=await page.evaluate('S.efforts[modelKey()]');assert variant in known['variants']
                await page.set_input_files('#filePicker',str(note));await page.wait_for_function('S.attachments.length===1')
                await page.set_input_files('#photoPicker',str(image));await page.wait_for_function('S.attachments.length===2')
                await page.fill('#inp','Reply with exactly ATTACHMENT_OK. Do not use tools.')
                async with page.expect_response(lambda r:r.url.split('?')[0].endswith('/prompt_async')) as sent:await page.click('#send')
                response=await sent.value;assert response.status==204
                body=response.request.post_data_json;assert body['variant']==variant and [p['mime'] for p in body['parts'] if p['type']=='file']==['text/plain','image/png']
                await page.wait_for_function("!S.running && document.querySelector('.msg.ai')",timeout=90000);assert 'ATTACHMENT_OK' in await page.locator('#main').inner_text()
                checkpoint('real_prompt_with_files_image_and_reasoning_variant')
            # Desktop bridge transports requests to the actual compiled native service.
            await page.evaluate('''({url,token})=>{
              window.CodexNative={request(raw){const request=JSON.parse(raw);if(['appearance','projects','selectProject'].includes(request.action)){window.codexNativeResult(request.id,{});return}
                fetch(url+'/bridge',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:raw}).then(r=>r.json()).then(value=>window.codexNativeResult(request.id,value)).catch(()=>window.codexNativeResult(request.id,{error:'Test bridge transport failed.'}));}};
              (async()=>{const r=await fetch(url+'/events',{headers:{Authorization:'Bearer '+token}}),reader=r.body.getReader();let buffer='';const decode=new TextDecoder();while(true){const v=await reader.read();if(v.done)break;buffer+=decode.decode(v.value,{stream:true});let end;while((end=buffer.indexOf('\\n\\n'))>=0){const line=buffer.slice(0,end);buffer=buffer.slice(end+2);if(line.startsWith('data: '))window.dispatchEvent(new CustomEvent('codex-remote-event',{detail:JSON.parse(line.slice(6))}));}}})();
            }''',{'url':f"http://127.0.0.1:{service['port']}",'token':service['token']})
            result['phase']='bridge_ready';assert await page.evaluate("async()=>{try{return Array.isArray((await nativeCall('connections')).connections)}catch{return false}}")
            result['phase']='open_ssh_selector';await page.click('#cEnv')
            result['phase']='add_ssh_connection';await page.click('#sa')
            result['phase']='fill_password_connection';await page.fill('#sn','Password SSH');await page.fill('#sh','127.0.0.1');await page.fill('#sp',str(ssh_port));await page.fill('#su','mobile');await page.fill('#sd','.');await page.fill('#sw',PASSWORD)
            result['phase']='save_password_connection';await page.click('#ss')
            result['phase']='wait_host_key';await page.locator('#st').wait_for(timeout=30000)
            result['phase']='confirm_host_key';await page.click('#st')
            result['phase']='wait_password_auth';await page.wait_for_function('S.connections.length===1',timeout=30000)
            checkpoint('real_ssh_password_and_host_key_confirmation')
            result['phase']='key_authentication'
            await page.click('#cEnv');await page.click('#sa');await page.fill('#sn','Key SSH');await page.fill('#sh','127.0.0.1');await page.fill('#sp',str(ssh_port));await page.fill('#su','mobile');await page.fill('#sd','.');await page.select_option('#sm','key');await page.set_input_files('#keyPicker',str(key_file));await page.wait_for_function("document.querySelector('#sk').value.includes('PRIVATE KEY')");await page.fill('#sx',PASSPHRASE);await page.click('#ss');await page.locator('#st').wait_for(timeout=30000);await page.click('#st');await page.wait_for_function('S.connections.length===2',timeout=30000)
            checkpoint('real_ssh_encrypted_ed25519_key_authentication')
            result['phase']='agent_ssh'
            await page.fill('#inp',"Use the SSH tool to run printf 'SSH_STREAM_START\\n'; sleep 3; printf 'SSH_STREAM_END\\n'. Then create remote-proof.txt containing exactly REMOTE_PATCH_OK followed by a newline using ssh_write_file. Read that file back with ssh_read_file. Do not read or change anything else. Finally reply REMOTE_DONE.")
            await page.click('#send');approvals=0;stream_seen=False
            for _ in range(240):
                if await page.locator('[data-r="once"]').count():await page.locator('[data-r="once"]').first.click();approvals+=1
                if await page.evaluate("[...S.remoteRuns.values()].some(r=>r.stdout.includes('SSH_STREAM_START')&&!r.stdout.includes('SSH_STREAM_END')&&r.status==='running')"):
                    assert await page.locator('.card').filter(has_text='SSH_STREAM_START').count();stream_seen=True
                if not await page.evaluate('S.running || S.sending'):break
                await asyncio.sleep(.5)
            result['agentState']=await page.evaluate("({running:S.running,sending:S.sending,permissions:S.perms.size,tools:[...S.msgs.values()].flatMap(m=>[...m.parts.values()]).filter(p=>p.type==='tool').map(p=>({name:p.tool,status:p.state.status}))})")
            result['agentState'].update(approvals=approvals,streamSeen=stream_seen)
            assert not await page.evaluate('S.running') and approvals>=2
            assert (SSHROOT/'remote-proof.txt').read_text()=='REMOTE_PATCH_OK\n'
            assert stream_seen and 'REMOTE_DONE' in await page.locator('#main').inner_text()
            assert await page.locator('.dl.a').filter(has_text='REMOTE_PATCH_OK').count()
            for name in ['real_agent_ssh_commands_stream_before_completion','real_agent_ssh_read_write_and_patch','remote_actions_require_approval']:checkpoint(name)
            result['phase']='websearch'
            await page.evaluate('S.connectionID=null;chips();S.webSearch=true;chips()')
            await page.fill('#inp','Use websearch to find the official OpenCode documentation website. Do not use any other tool. Reply with that URL.')
            await page.click('#send');await page.wait_for_function('!S.running && !S.sending',timeout=120000)
            tools=await page.evaluate("[...S.msgs.values()].flatMap(m=>[...m.parts.values()]).filter(p=>p.type==='tool').map(p=>({name:p.tool,status:p.state.status}))")
            assert any(t['name']=='websearch' and t['status']=='completed' for t in tools)
            checkpoint('real_opencode_websearch_completed')
            await page.click('#gear');await page.screenshot(path=str(TASK/'settings.png'))
            await page.click('#ap');await page.click('[data-theme="dark"]');await page.screenshot(path=str(TASK/'appearance-dark.png'))
            assert not errors;result['pageErrors']=errors;result['pass']=True
            await browser.close()
    except Exception as e:
        import traceback
        result['pass']=False;result['failure']=type(e).__name__
        frames=traceback.extract_tb(e.__traceback__)
        result['failureAt']=[f'{f.name}:{f.lineno}' for f in frames[:3]+frames[-2:]]
        if 'bridge_http' in locals():result['bridgeHTTP']=bridge_http
        if 'network_errors' in locals():result['networkErrors']=network_errors
    finally:
        ssh_server.close();await ssh_server.wait_closed()
        for p in reversed(processes):p.terminate()
        for p in reversed(processes):
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill()
    (ROOT/'tests/mobile-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    (LOCAL/'mobile-last-task.txt').write_text(str(TASK))
    print(json.dumps(result));return result.get('pass',False)

if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
