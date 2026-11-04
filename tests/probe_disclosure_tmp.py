"""One-off probe: disclosure open animation keyframes + stuck attributes. Not a proof."""
import asyncio, sys
from playwright.async_api import async_playwright
sys.path.insert(0, 'tests')
from reasoning_activity_proof import A1, SID, U1, activity_segment, boot_page, launch_browser, message, seed, tool, user
from work_log_proof import BODY, open_work, work_id

OUTPUT = '\n'.join(f'line {i}: stdout' for i in range(40))


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw, r'C:/Program Files/Google/Chrome/Application/chrome.exe')
        context, page, errors, unexpected = await boot_page(browser, theme='dark')
        part = tool('local', 'list', {'path': 'src'}, OUTPUT)
        await seed(page, [user(), message(A1, 'assistant', [part], U1, 9000)])
        await open_work(page, work_id(A1, 'local'))
        row_sel = BODY + ' ' + activity_segment(A1, 'local')

        # Pure Chrome semantics of a 'nullpx' first keyframe:
        print('nullpx keyframe anim:', await page.evaluate('''()=>{
          const el=document.createElement('div');el.style.height='40px';el.style.position='absolute';
          document.body.appendChild(el);
          try{
            const m=el.animate([{height:'nullpx'},{height:'100px'}],{duration:200,fill:'forwards'});
            return new Promise(res=>setTimeout(()=>{
              const h1=el.getBoundingClientRect().height;
              setTimeout(()=>{const h2=el.getBoundingClientRect().height;m.cancel();el.remove();
                res({mid:h1,end:h2,threw:false})},210);
            },100));
          }catch(e){el.remove();return {threw:true,message:String(e)}}
        }'''))

        # Real open: is an animation created, and what are its keyframes?
        probe = await page.evaluate('''async sel=>{
          const row=document.querySelector(sel);
          const content=row.querySelector(':scope>.activity-body');
          const armed=new Promise(res=>{const orig=Element.prototype.animate;
            Element.prototype.animate=function(kf,opts){window.__kf=kf;window.__opts=opts;
              const m=orig.call(this,kf,opts);Element.prototype.animate=orig;res({kf,opts,element:this===content});return m};
          });
          const before={open:row.open,contentH:content.getBoundingClientRect().height,
            contentDisplay:getComputedStyle(content).display};
          row.querySelector(':scope>summary').click();
          const caught=await Promise.race([armed,new Promise(r=>setTimeout(()=>r(null),300))]);
          await new Promise(r=>setTimeout(r,50));
          const anims=content.getAnimations({subtree:true}).map(a=>({playState:a.playState,
            effectHeight:a.effect&&a.effect.getKeyframes?a.effect.getKeyframes().map(k=>k.height):null}));
          return {before,caught,anims,open:row.open,hold:row.hasAttribute('data-disclosure-hold'),
            target:content.hasAttribute('data-disclosure-target'),h:content.getBoundingClientRect().height};
        }''', row_sel)
        print('real open probe:', probe)
        await page.wait_for_timeout(700)
        print('after settle:', await page.evaluate('''sel=>{
          const row=document.querySelector(sel),content=row.querySelector(':scope>.activity-body');
          return {open:row.open,hold:row.hasAttribute('data-disclosure-hold'),
            target:content.hasAttribute('data-disclosure-target'),
            running:row.getAnimations({subtree:true}).filter(a=>a.playState==='running').length};
        }''', row_sel))

        # Second toggle round: close then open again (open with prior state? no state after finish)
        print('reopen probe:', await page.evaluate('''async sel=>{
          const row=document.querySelector(sel),content=row.querySelector(':scope>.activity-body');
          row.querySelector(':scope>summary').click();await new Promise(r=>setTimeout(r,400)); // close
          let caught=null;
          const orig=Element.prototype.animate;
          Element.prototype.animate=function(kf,opts){caught={kf,opts};Element.prototype.animate=orig;return orig.call(this,kf,opts)};
          row.querySelector(':scope>summary').click();await new Promise(r=>setTimeout(r,30));
          const anims=content.getAnimations({subtree:true}).map(a=>a.playState);
          await new Promise(r=>setTimeout(r,400));
          return {caught,animsMid:anims,open:row.open,
            hold:row.hasAttribute('data-disclosure-hold'),target:content.hasAttribute('data-disclosure-target')};
        }''', row_sel))
        await browser.close()

asyncio.run(main())
