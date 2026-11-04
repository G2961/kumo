"""One-off: measure sent-photo cards in the feed, clean probe cards from composer."""
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

print('probe cards removed:', ev("(()=>{S.attachments=[];renderAttachments();return document.querySelectorAll('#attachments .attachment').length})()"))
print('feed cards:', ev("""(()=>{const out=[];document.querySelectorAll('.msg .attachments .attachment').forEach(n=>{
  const r=n.getBoundingClientRect();const img=n.querySelector('img');
  out.push({w:Math.round(r.width),h:Math.round(r.height),img:!!img,imgW:img?Math.round(img.getBoundingClientRect().width):0,
            name:(n.querySelector('.att-name')||{textContent:''}).textContent.slice(0,20)});});
  return out;})()"""))
ws.close()
