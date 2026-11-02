"""Reproduce the phone's prompt failure: UnknownError at SessionPrompt.createUserMessage."""
import json, socket, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path
from uuid import uuid4

OPENCODE = 'D:/Dev/npm-global/node_modules/opencode-ai/bin/opencode.exe'
TASK = Path(__name__ and 'D:/smokecoding/codex-mobile/.local/err-probe-' + uuid4().hex[:6])
TASK.mkdir(parents=True)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


port = free_port()
origin = f'http://127.0.0.1:{port}'
# Provider configured exactly like the phone's form would: id 'agentrouter', model gpt-6.1-sol
config = {'provider': {'agentrouter': {
    'name': 'agentrouter', 'npm': '@ai-sdk/openai',
    'options': {'baseURL': 'https://agentrouter.org/v1'},
    'models': {'gpt-6.1-sol': {'name': 'GPT-6.1 Sol', 'reasoning': True, 'attachment': True,
                                'variants': {'low': {}, 'high': {}}}},
}}}
env = dict(__import__('os').environ)
env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
out = open(TASK / 'server.log', 'w', encoding='utf-8')
server = subprocess.Popen([OPENCODE, 'serve', '--hostname', '127.0.0.1', '--port', str(port)],
                          cwd=TASK, env=env, stdout=out, stderr=out)
try:
    for _ in range(80):
        try:
            with urllib.request.urlopen(origin + '/global/health', timeout=2) as r:
                h = json.load(r)
            if h.get('healthy'):
                break
        except Exception:
            time.sleep(.25)
    proj = TASK / 'proj'
    proj.mkdir()
    d = str(proj)


    def api(path, body=None, method='GET'):
        url = origin + path + '?' + urllib.parse.urlencode({'directory': d})
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
            return e.code, raw


    st, sess = api('/session', {'title': 'err probe'}, 'POST')
    sid = sess['id']
    print('session', st, sid)

    # Exactly the permission set the client sends for SSH connection + build agent
    permission = [
        {'permission': 'websearch', 'pattern': '*', 'action': 'deny'},
        {'permission': 'mobile_ssh_ssh_connections', 'pattern': '*', 'action': 'allow'},
        {'permission': 'mobile_ssh_ssh_exec', 'pattern': '*', 'action': 'ask'},
        {'permission': 'mobile_ssh_ssh_read_file', 'pattern': '*', 'action': 'allow'},
        {'permission': 'mobile_ssh_ssh_write_file', 'pattern': '*', 'action': 'ask'},
        {'permission': 'bash', 'pattern': '*', 'action': 'deny'},
        {'permission': 'read', 'pattern': '*', 'action': 'deny'},
        {'permission': 'write', 'pattern': '*', 'action': 'deny'},
        {'permission': 'edit', 'pattern': '*', 'action': 'deny'},
        {'permission': 'apply_patch', 'pattern': '*', 'action': 'deny'},
        {'permission': 'glob', 'pattern': '*', 'action': 'deny'},
        {'permission': 'grep', 'pattern': '*', 'action': 'deny'},
        {'permission': 'task', 'pattern': '*', 'action': 'deny'},
    ]
    st2, patch = api('/session/' + sid, {'permission': permission}, 'PATCH')
    print('permission patch', st2, str(patch)[:200])

    body = {
        'parts': [{'type': 'text', 'text': 'дарова'}],
        'agent': 'auto',
        'model': {'providerID': 'agentrouter', 'modelID': 'gpt-6.1-sol'},
        'system': 'Run commands and read or edit files on the selected SSH connection only. connection_id=test; session_id=' + sid + '; working directory=home. Use mobile_ssh tools and include this session_id in every call. Do not run local shell or file tools. Never expose credentials.',
    }
    st3, resp = api('/session/' + sid + '/prompt_async', body, 'POST')
    print('prompt_async', st3, str(resp)[:600])
    if st3 >= 400:
        print('REPRODUCED at prompt_async')
        sys.exit(0)
    time.sleep(6)
    st4, msgs = api('/session/' + sid + '/message')
    print('messages', st4, 'count', len(msgs or []))
    for m in (msgs or []):
        print('msg', m['info'].get('role'), str(m['info'].get('error'))[:400])
    print('--- server.log ---')
    print((TASK / 'server.log').read_text('utf-8', 'replace')[:2000])
finally:
    server.terminate()
    out.close()
