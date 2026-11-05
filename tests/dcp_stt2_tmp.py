"""One-off: capture voice callbacks during a native sttRecord start/stop cycle."""
import json
import time
import urllib.request
import websocket

url = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))[0]['webSocketDebuggerUrl']
ws = websocket.create_connection(url, timeout=30, suppress_origin=True)
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
print('record on:', ev("nativeCall('sttRecord',{on:true}).then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)"))
time.sleep(4)
print('record off:', ev("nativeCall('sttRecord',{on:false}).then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)"))
time.sleep(12)
print('voiceError captured:', ev("JSON.stringify(window.__ve)"))
print('voiceResult captured:', ev("JSON.stringify(window.__vr)"))
print('S.errors:', ev("JSON.stringify(S.errors.slice(-3))"))
ws.close()
