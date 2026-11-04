"""One-off: catch the exception inside addFiles on the live page."""
import json
import urllib.request
import websocket

url = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))[0]['webSocketDebuggerUrl']
ws = websocket.create_connection(url, timeout=10, suppress_origin=True)
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

print('listeners:', ev("JSON.stringify({file: !!document.getElementById('filePicker').onchange, photo: !!document.getElementById('photoPicker').onchange})"))
print('S.view:', ev("S.view"))
print('direct addFiles probe:', ev("""(async () => {
  try {
    const f = new File(['hello world'], 'probe.txt', {type: 'text/plain'});
    await addFiles([f]);
    return {ok: true, att: S.attachments.length, cards: document.querySelectorAll('#attachments .attachment').length};
  } catch (e) {
    return {ok: false, name: e && e.name, msg: e && e.message, stack: e && e.stack && e.stack.split('\\n').slice(0, 4).join(' | ')};
  }
})()"""))
print('errors log:', ev("JSON.stringify(S.errors.slice(-3))"))
ws.close()
