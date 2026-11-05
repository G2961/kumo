"""One-off: full voice cycle on the live page with hooks; then check crashes."""
import json
import time
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

print('hooks:', ev("(()=>{window.__ve=[];window.__vr=[];window.__voiceError=m=>__ve.push(String(m));window.__voiceResult=m=>__vr.push(String(m));return 'ok'})()"))
print('mic visible:', ev("!document.getElementById('mic').hidden"))
print('tap 1 (record on):', ev("(()=>{document.getElementById('mic').click();return document.getElementById('mic').classList.contains('listening')})()"))
time.sleep(4)
print('tap 2 (stop):', ev("(()=>{document.getElementById('mic').classList.remove('listening');CodexNative.request(JSON.stringify({action:'sttRecord',data:{on:false}}));return 'stopped'})()"))
time.sleep(15)
print('voiceError:', ev("JSON.stringify(window.__ve)"))
print('voiceResult:', ev("JSON.stringify(window.__vr)"))
print('inp value:', ev("JSON.stringify(document.getElementById('inp').value.slice(0,60))"))
ws.close()
