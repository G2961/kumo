"""One-off: restart runtime, then fresh network diagnostics in the sandbox."""
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

print('restartServer:', ev("nativeCall('restartServer').then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)"))
time.sleep(20)
print('health after restart:', ev("api('/global/health').then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)"))
SID = 'ses_e547e4b2dffe9RxgEmRw1PwSeK'
prompt = json.dumps('Новая сетевая диагностика, покажи вывод целиком: curl -sv --max-time 8 https://api.groq.com/openai/v1/models 2>&1 | tail -10; затем echo PROXY=$HTTPS_PROXY NO_PROXY=$NO_PROXY; затем curl -s --max-time 6 https://api.groq.com/openai/v1/models -o /dev/null -w "%{http_code}"', ensure_ascii=False)
js = ("api('/session/" + SID + "/prompt_async',{method:'POST',body:{parts:[{type:'text',text:" + prompt + "}],agent:'build'}})"
      ".then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)")
print('continue:', ev(js))
time.sleep(100)
js2 = ("api('/session/" + SID + "/message').then(v=>{"
       "const arr=Array.isArray(v)?v:Object.values(v||{});"
       "return arr.slice(-2).map(m=>((m.parts||[]).map(p=>{"
       "if(p.type==='text')return 'TEXT: '+(p.text||'').slice(0,900);"
       "if(p.type==='tool'){const st=p.state||{};return 'TOOL ['+(st.status||'?')+'] '+String((st.input||{}).command||'').slice(0,100)+' => '+String(st.output||'').slice(0,600)}"
       "return null}).filter(Boolean).join(' | ')).slice(0,2000)).join('  ||  ')"
       "}).catch(e=>'ERR '+e.message)")
print('curl trace:', ev(js2))
ws.close()
