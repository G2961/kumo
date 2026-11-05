"""One-off: measure the lag of opening the preview panel (LoAF + frame gaps + iframe load)."""
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

JS = '''(async () => {
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const loaf=[];
 let po=null;
 try{po=new PerformanceObserver(l=>{for(const e of l.getEntries())loaf.push({dur:Math.round(e.duration),scripts:(e.scripts||[]).slice(0,3).map(s=>({who:String(s.invoker||s.name).slice(0,70),dur:Math.round(s.duration)}))})});
  po.observe({type:'long-animation-frame',buffered:true})}catch(e){}
 let last=performance.now(),gaps=0,maxGap=0,raf=true;
 const tick=()=>{if(!raf)return;const now=performance.now(),d=now-last;last=now;if(d>40){gaps++;maxGap=Math.max(maxGap,Math.round(d))}requestAnimationFrame(tick)};
 requestAnimationFrame(tick);
 const a=[...document.querySelectorAll('#feed .msg a[href]')].find(x=>/localhost|127\\.0\\.0\\.1/.test(x.getAttribute('href')||''));
 if(!a){raf=false;return 'NO LINK'}
 const href=a.getAttribute('href');
 const t0=performance.now();
 a.click();
 await sleep(600);
 const openDur=Math.round(performance.now()-t0);
 const iframe=document.getElementById('apvFrame');
 const loaded=await new Promise(res=>{let done=false;iframe.addEventListener('load',()=>{if(!done){done=true;res('loaded@'+Math.round(performance.now()-t0)+'ms')}});setTimeout(()=>{if(!done)res('no-load@'+Math.round(performance.now()-t0)+'ms')},3500)});
 raf=false;
 try{po.disconnect()}catch(e){}
 return {href,openDur,iframeLoad:loaded,frameDrops:gaps,maxGapMs:maxGap,longFrames:loaf.slice(0,6),
         panelH:Math.round(document.getElementById('apv').getBoundingClientRect().height),
         hidden:document.getElementById('apv').hidden};
})()'''

print('measure:', ev(JS))
ws.close()
