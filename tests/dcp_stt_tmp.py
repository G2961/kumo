"""One-off: live end-to-end STT chain test (fake key -> record -> 401 proves the route)."""
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

print('save fake groq key:', ev("nativeCall('sttSave',{mode:'groq',key:'gsk_fake_test',model:'whisper-large-v3-turbo',base:''}).then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)"))
print('sttGet:', ev("nativeCall('sttGet').then(v=>JSON.stringify(v)).catch(e=>'ERR '+e.message)"))
print('mic hidden:', ev("document.getElementById('mic').hidden"))
print('tap mic (start recording):', ev("(()=>{document.getElementById('mic').click();return document.getElementById('mic').classList.contains('listening')})()"))
time.sleep(3)  # record ~3s of room noise
print('tap mic (stop + send):', ev("(()=>{document.getElementById('mic').classList.remove('listening');CodexNative.request(JSON.stringify({action:'sttRecord',data:{on:false}}));return 'sent'})()"))
time.sleep(6)  # POST round trip
print('errors after 401 expected:', ev("JSON.stringify(S.errors.slice(-2))"))
print('voice error surfaced:', ev("!!window.__voiceError"))
ws.close()
