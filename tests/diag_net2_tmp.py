"""One-off: abort stale turn, fresh session, net diagnostics, read tools."""
import json
import time
import urllib.request
import websocket

url = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))[0]['webSocketDebuggerUrl']
ws = websocket.create_connection(url, timeout=150, suppress_origin=True)
mid = [0]

def ev(expr):
    mid[0] += 1
    ws.send(json.dumps({'id': mid[0], 'method': 'Runtime.evaluate',
                        'params': {'expression': expr, 'returnByValue': True, 'awaitPromise': True}}))
    while True:
        m = json.loads(ws.recv())
        if m.get('id') == mid[0]:
            r = m.get('result', {}).get('result', {})
            return r.get('value', r.get('description', r))

SID = 'ses_e547e4b2dffe9RxgEmRw1PwSeK'
print('abort stale:', ev("api('/session/" + SID + "/abort',{method:'POST',directory:S.dir}).then(v=>'OK').catch(e=>'ERR '+e.message)"))
time.sleep(2)
prompt = json.dumps('Покажи вывод целиком, все строки: curl -sv --max-time 8 https://api.groq.com/openai/v1/models 2>&1 | tail -10; затем echo PROXY=$HTTPS_PROXY; echo NOPROXY=$NO_PROXY', ensure_ascii=False)
js = ("(async()=>{const s=await api('/session',{method:'POST',body:{},directory:S.dir});"
      "if(!s||!s.id)return 'SESSION FAIL';"
      "await api('/session/'+s.id+'/prompt_async',{method:'POST',body:{parts:[{type:'text',text:" + prompt + "}],agent:'build'},directory:S.dir});"
      "return s.id;})()")
sid = ev(js)
print('new session:', sid)
if sid and not sid.startswith('SESSION FAIL'):
    time.sleep(110)
    js2 = ("api('/session/" + str(sid) + "/message',{directory:S.dir}).then(v=>{"
           "const arr=Array.isArray(v)?v:Object.values(v||{});"
           "return arr.map(m=>((m.parts||[]).map(p=>{"
           "if(p.type==='text')return 'TEXT: '+(p.text||'').slice(0,700);"
           "if(p.type==='tool'){const st=p.state||{};return 'TOOL ['+(st.status||'?')+'] '+String((st.input||{}).command||'').slice(0,100)+' => '+String(st.output||'').slice(0,650)}"
           "return null}).filter(Boolean).join(' | ')).slice(0,1800)).join('  ||  ')"
           "}).catch(e=>'ERR '+e.message)")
    print('tools:', ev(js2))
ws.close()
