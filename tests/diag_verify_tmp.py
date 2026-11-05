"""One-off: continue the diag session to verify the fixed npx (json.dumps prompt)."""
import json
import time
import urllib.request
import websocket

url = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))[0]['webSocketDebuggerUrl']
ws = websocket.create_connection(url, timeout=120, suppress_origin=True)
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
prompt = json.dumps('npx-обёртку в песочнице только что обновили. Выполни: npx -y serve --version и npx --version. Покажи вывод каждой команды одной строкой кода.', ensure_ascii=False)
js = ("api('/session/" + SID + "/prompt_async',{method:'POST',body:{parts:[{type:'text',text:" + prompt + "}],agent:'build'}})"
      ".then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)")
print('continue:', ev(js))
time.sleep(100)
js2 = ("api('/session/" + SID + "/message').then(v=>{"
       "const arr=Array.isArray(v)?v:Object.values(v||{});"
       "return arr.slice(-2).map(m=>((m.parts||[]).map(p=>{"
       "if(p.type==='text')return 'TEXT: '+(p.text||'').slice(0,500);"
       "if(p.type==='tool'){const st=p.state||{};return 'TOOL '+p.tool+' ['+(st.status||'?')+'] '+String((st.input||{}).command||'').slice(0,70)+' => '+String(st.output||'').slice(0,250)}"
       "return null}).filter(Boolean).join(' | ')).slice(0,900)).join('  ||  ')"
       "}).catch(e=>'ERR '+e.message)")
print('npx verify:', ev(js2))
ws.close()
