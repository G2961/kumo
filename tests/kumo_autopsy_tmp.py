"""Autopsy probe: prove WHY kumo animations stutter in the app but not in the demo.

Measures, on the current codex.html (no code changes):
1. CSS animation restart on tail re-insertion (what renderMsgs does per streaming delta).
2. Dead transitions when phase class (wait->work) lands right after re-insertion.
3. Same measurements on the Claude demo file for reference.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

APP = 'file:///D:/smokecoding/codex-mobile/opencode-android-new/app/src/main/assets/codex.html'
DEMO = str(Path(r'D:\smokecoding\codex-mobile\new_chat_screen\Kumo_ прогон агента v9.html').read_text(encoding='utf-8'))

MEASURE = '''async () => {
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const anims=el=>el.getAnimations({subtree:true}).filter(a=>a.animationName!==undefined)
   .map(a=>({name:a.animationName, ct:Math.round(a.currentTime||0)}));
 const waai=el=>el.getAnimations().filter(a=>a.animationName===undefined).map(a=>({ct:Math.round(a.currentTime||0),kf:(a.effect&&a.effect.getKeyframes?a.effect.getKeyframes():[]).map(k=>k.transform)}));
 const out={};
 // --- setup: thread open, tail mounted, maskot alive in it ---
 S.sid='t1';S.running=true;renderMsgs();kwGo();S.running=true;
 await sleep(1500);
 const kumo=document.getElementById('kwKumo');
 out.before_stream=anims(kumo).filter(a=>a.name).slice(0,3);
 // --- stream: several renderMsgs() the way deltas hit (tail must NOT restart) ---
 for(let i=0;i<4;i++){renderMsgs();await sleep(140)}
 out.after_stream=anims(kumo).filter(a=>a.name).slice(0,3);
 // --- phase change wait->work: demo does it via flip (WAAI spring with size scale) ---
 out.pre_phase={state:kwState,phase:kwPhase,cls:kwKumo.className};
 kwToolRun({tool:'read',state:{status:'running',input:{filePath:'/x/player.js'}}});
 out.post_phase_sync={cls:kwKumo.className,waai:waai(kumo).map(a=>a.kf).slice(0,1)};
 await sleep(120);
 out.phase_flip=waai(kumo).slice(0,1);
 return out;
}'''

DEMO_MEASURE = '''async () => {
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const anims=el=>el.getAnimations({subtree:true}).filter(a=>a.animationName!==undefined)
   .map(a=>({name:a.animationName, ct:Math.round(a.currentTime||0)}));
 const out={};
 document.getElementById('inp').value='test';
 go();
 await sleep(1500);
 const kumo=document.getElementById('kumo');
 out.before=anims(kumo).filter(a=>a.name).slice(0,6);
 // demo never re-inserts the node; simulate a text tick instead (say() mutates inner text only)
 await sleep(300);
 out.after_no_reinsert=anims(kumo).filter(a=>a.name).slice(0,6);
 // phase class change on stable node: direct (no flip) vs via flip() like the demo does
 const trans=()=>kumo.getAnimations().filter(a=>a instanceof CSSTransition).map(a=>({p:a.transitionProperty,ct:Math.round(a.currentTime||0)}));
 const waai=()=>kumo.getAnimations().filter(a=>!(a instanceof CSSAnimation)&&!(a instanceof CSSTransition)).map(a=>({ct:Math.round(a.currentTime||0),effect:a.effect&&a.effect.getKeyframes?a.effect.getKeyframes().map(k=>k.transform):null}));
 kumo.className='kumo work';
 await sleep(120);
 out.direct_class_change={trans:trans(),waai:waai()};
 await sleep(1600);
 flip(()=>{kumo.className='kumo wait'});
 await sleep(120);
 out.via_flip={trans:trans(),waai:waai().slice(0,2)};
 return out;
}'''

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome')
        page = await browser.new_page(viewport={'width': 424, 'height': 900}, color_scheme='dark')
        await page.goto(APP)
        await page.wait_for_timeout(700)
        app_res = await page.evaluate(MEASURE)
        print('=== APP codex.html ===')
        print(json.dumps(app_res, ensure_ascii=False, indent=1))
        await page.goto(Path(r'D:\smokecoding\codex-mobile\new_chat_screen\Kumo_ прогон агента v9.html').as_uri())
        await page.wait_for_timeout(700)
        demo_res = await page.evaluate(DEMO_MEASURE)
        print('=== DEMO (Claude v9) ===')
        print(json.dumps(demo_res, ensure_ascii=False, indent=1))
        await browser.close()

asyncio.run(main())
