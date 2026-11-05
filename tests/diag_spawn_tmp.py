"""One-off: spawn a diagnostic session so the agent reports sandbox state of npx/tmp."""
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

r = ev("""(async () => {
  const s = await api('/session', {method:'POST', body:{}});
  await api('/session/'+s.id+'/prompt_async', {method:'POST', body:{
    parts:[{type:'text',text:'Диагностика песочницы. Выполни ровно эти команды и для каждой покажи вывод и код выхода одной строкой-строкой кода: 1) npx --version 2) npx -y serve --version 3) echo TMPDIR=$TMPDIR 4) ls -ld /tmp 5) touch /tmp/kumo_probe && echo tmp-ok 6) touch $TMPDIR/kumo_probe && echo tmpdir-ok 7) npm config get cache. Ничего не чини, только собери факты.'}],
    agent:'build'}});
  return s.id;
})()""")
print('diag session:', r)
ws.close()
with open(r'D:\smokecoding\codex-mobile\opencode-android-new\tests\diag_session_id.txt', 'w') as f:
    f.write(r)
