"""One-off: measure scroll jank in the live feed during real swipes."""
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

print('arm:', ev('''(() => {
 window.__loaf=[];window.__gaps={n:0,max:0};
 try{window.__po=new PerformanceObserver(l=>{for(const e of l.getEntries())
  window.__loaf.push({dur:Math.round(e.duration),scripts:(e.scripts||[]).slice(0,3).map(s=>({who:String(s.invoker||s.name).slice(0,60),dur:Math.round(s.duration)}))})});
  window.__po.observe({type:'long-animation-frame',buffered:false})}catch(e){__loaf.push({err:'no LoAF'})}
 let last=performance.now();
 const tick=()=>{if(!window.__raf)return;const now=performance.now(),d=now-last;last=now;
  if(d>40){__gaps.n++;__gaps.max=Math.max(__gaps.max,Math.round(d))}requestAnimationFrame(tick)};
 window.__raf=true;requestAnimationFrame(tick);
 return 'armed';})()'''))
ws.close()
