"""One-off: screenshot composer cards vs sent feed cards, measure paddings."""
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

print('page state:', ev("JSON.stringify({sid: S.sid, att: S.attachments.length, msgs: S.msgs.size})"))
# small red/green photos as pending attachments
PX = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAF0lEQVR4nGP8z8Dwn4EIwESMolGFIyQFAMyoAQGtjFNJAAAAAElFTkSuQmCC"
print('composer probe:', ev(f"""(() => {{
  S.attachments=[{{type:'file',mime:'image/jpeg',filename:'IMG_1.jpg',url:'{PX}',size:1000}},
                 {{type:'file',mime:'image/jpeg',filename:'IMG_2.jpg',url:'{PX}',size:1000}}];
  renderAttachments();
  const card=document.querySelector('#attachments .attachment');
  const cs=getComputedStyle(card);
  const box=card.getBoundingClientRect();
  const foot=document.querySelector('footer').getBoundingClientRect();
  return {{card:{{w:Math.round(box.width),h:Math.round(box.height),pad:cs.padding,gap:getComputedStyle(document.getElementById('attachments')).gap}},
          footH:Math.round(foot.height)}};
}})()"""))
ws.close()
