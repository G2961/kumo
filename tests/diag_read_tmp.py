"""One-off: read the npx diagnostic session transcript."""
import json
import urllib.request
import websocket

url = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))[0]['webSocketDebuggerUrl']
ws = websocket.create_connection(url, timeout=60, suppress_origin=True)
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

sid = open(r'D:\smokecoding\codex-mobile\opencode-android-new\tests\diag_session_id.txt').read().strip()
js = ("api('/session/" + sid + "/message').then(v=>{"
      "const arr=Array.isArray(v)?v:Object.values(v||{});"
      "return arr.map(m=>((m.parts||[]).filter(p=>p.type==='text').map(p=>p.text).join(' ')).slice(0,600)).join('  ||  ')"
      "}).catch(e=>'ERR '+e.message)")
print('diag all:', ev(js))
ws.close()
