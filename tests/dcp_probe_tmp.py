"""One-off: live DCP diagnosis of the photo attach chain inside the running WebView."""
import json
import urllib.request
import websocket  # pip-installed into the api-test venv

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

print('1) input files after real picker pick:', ev("document.getElementById('photoPicker').files.length"))
print('2) attachments now:', ev("S.attachments.length"))
print('3) addFiles alive? synthetic File through the real onchange:')
print(ev("""(() => {
  const dt = new DataTransfer();
  dt.items.add(new File(['hello world'], 'probe.txt', {type: 'text/plain'}));
  const inp = document.getElementById('filePicker');
  inp.files = dt.files;
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  return {attAfter: S.attachments.length, cards: document.querySelectorAll('#attachments .attachment').length};
})()"""))
print('4) photoPicker accept/multiple:', ev("JSON.stringify({acc: document.getElementById('photoPicker').accept, mult: document.getElementById('photoPicker').multiple})"))
ws.close()
