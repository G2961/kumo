#!/usr/bin/env python3
"""Run with Python; behavioral checks use Node when it is installed."""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).with_name("stage-native-ui.py")
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "app/src/main/assets/codex.html"
RESOURCES = ROOT / "iOS/Sources/AppHarness-Resources"


def load_stage():
    spec = importlib.util.spec_from_file_location("stage_native_ui", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def section(html, start, end):
    return html.split(start, 1)[1].split(end, 1)[0]


class StageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage = load_stage()
        cls.source_bytes = SOURCE.read_bytes()
        cls.source = cls.source_bytes.decode("utf-8").replace("\r\n", "\n")
        cls.html = cls.stage.patch_html(cls.source)

    def test_native_transport_and_boot(self):
        api = section(self.html, "async function api(", "const norm=")
        self.assertLess(api.index("window.KumoNativeBackend"), api.index("new URL"))
        self.assertIn("nativeCall('backendRequest'", api)
        boot = section(self.html, "async function boot()", "const normPath=")
        self.assertIn("S.spec=await api('/doc')", boot)
        self.assertNotIn("fetch(", boot)
        need = section(self.html, "const NEED=[", "];\n")
        self.assertNotIn("/event", need)
        self.assertEqual(SOURCE.read_bytes(), self.source_bytes)

    def test_manual_models_and_honest_capabilities(self):
        form = section(self.html, "async function providerForm(", "/* ---------- SSH")
        self.assertIn("nativeCall('providerModels',{baseURL:url,key,providerID:existing||undefined})", form)
        self.assertIn('id="pmManual" ${window.KumoNativeBackend?', form)
        self.assertNotIn("Anthropic", form)
        self.assertIn("Keys stay in Keychain", form)
        self.assertIn("api('/auth/'+encodeURIComponent(id),{method:'PUT',body:{type:'api',key}})", form)
        system = section(self.html, "  body.system=", "  // prompt_async")
        self.assertIn("list, read, glob, grep, write and edit", system)
        self.assertIn("Communication:", system)
        for claim in ("bun", "npm", "npx", "curl", ".github-token", "$TMPDIR", "Use the websearch tool"):
            self.assertNotIn(claim, system)
        self.assertIn("Native backend", self.html)
        self.assertIn("not supported", self.html)
        self.assertIn("Project files and change history remain available", self.html)

    def test_provider_api_format_is_untouched(self):
        for start, end in (
            ('<label class="fld" for="pf">', '</select>'),
            (" $('#providerPreset').onchange=", " $('#sheet').onclick="),
        ):
            self.assertEqual(section(self.html, start, end), section(self.source, start, end))

    def test_native_context_field_and_search_menu(self):
        form = section(self.html, "async function providerForm(", "/* ---------- SSH")
        self.assertIn('${window.KumoNativeBackend?`<label class="fld" for="pContext">', form)
        self.assertIn("Context window for models without metadata", form)
        self.assertIn('id="pContext" type="text"', form)
        self.assertNotIn('id="pContext" type="text" value=', form)
        self.assertIn("nativeProviderLimits(modelID", form)
        self.assertIn('id="attachSearch" ${window.KumoNativeBackend?', self.html)
        self.assertIn("Not supported", self.html)
        self.assertEqual(SOURCE.read_bytes(), self.source_bytes)

    def test_render_events_and_history_are_untouched(self):
        for start, end in (
            ("function onEvent(ev)", "function resetThread("),
            ("function formatDiff(", "function configuredRules("),
            ("function renderMsgs(", "async function forkSession("),
        ):
            self.assertEqual(section(self.html, start, end), section(self.source, start, end))
        # Do not invent /doc routes or change custom-scheme asset resolution.
        self.assertEqual(self.html.count("/_ui/"), self.source.count("/_ui/"))
        self.assertNotIn("x-native-capabilities", self.html)

    def test_exact_patches_fail_closed(self):
        for old in ("async function api(path,o={}){", "function connectSSE(){", "await (await fetch('/doc')).json()", 'id="pmManual" hidden', "let fetched=await fetchProviderModels(url.href,key);"):
            with self.subTest(old=old), self.assertRaisesRegex(ValueError, "patch"):
                self.stage.patch_html(self.source.replace(old, old + " changed", 1))
        with self.assertRaises(ValueError):
            self.stage.patch_html(self.source + "\n" + self.stage.API_OLD)
        with self.assertRaises(ValueError):
            self.stage.patch_html(self.html)

    def test_cli_is_reproducible_and_writes_html_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "app/src/main/assets/codex.html"
            source.parent.mkdir(parents=True)
            source.write_bytes(self.source_bytes)
            resources = root / "iOS/Sources/AppHarness-Resources"
            resources.mkdir(parents=True)
            marker = resources / "xterm.js"
            marker.write_bytes(b"parent-owned resource")
            args = [sys.executable, str(SCRIPT), "--root", str(root)]
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            subprocess.run(args, check=True, capture_output=True, timeout=10)
            output = resources / "codex.html"
            self.assertEqual(output.read_bytes(), self.html.encode("utf-8"))
            stamp = output.stat().st_mtime_ns
            subprocess.run(args, check=True, capture_output=True, timeout=10)
            self.assertEqual(output.stat().st_mtime_ns, stamp)
            subprocess.run(args + ["--check"], check=True, capture_output=True, timeout=10)
            output.write_bytes(b"stale")
            result = subprocess.run(args + ["--check"], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"stale")
            subprocess.run([sys.executable, str(SCRIPT), "--source", str(source), "--resources", str(resources)], check=True, capture_output=True, timeout=10)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual(marker.read_bytes(), b"parent-owned resource")
            self.assertEqual(sorted(p.name for p in resources.iterdir()), ["codex.html", "xterm.js"])
            # Explicit CLI options cannot turn staging into an Android asset write.
            result = subprocess.run(args + ["--resources", str(source.parent)], capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            output.write_bytes(b"keep on patch failure")
            source.write_text("not the canonical UI", encoding="utf-8")
            result = subprocess.run(args, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"keep on patch failure")

    @unittest.skipUnless(shutil.which("node"), "Node is not installed; Python patch checks still run")
    def test_javascript_behavior(self):
        # Delimit whole declarations, including their actual bridge and lifecycle code.
        pieces = {
            "redact": "const redact=" + section(self.html, "const redact=", "const esc="),
            "api": "async function api(" + section(self.html, "async function api(", "const norm="),
            "spec": "const norm=" + section(self.html, "const norm=", "/* ---------- boot"),
            "boot": "async function boot()" + section(self.html, "async function boot()", "const normPath="),
            "events": "let lastEv=0;" + section(self.html, "let lastEv=0;", "function ensureMsg("),
            "bridge": "const nativePending=" + section(self.html, "const nativePending=", "function b64e("),
            "search": "function toggleSearch()" + section(self.html, "function toggleSearch()", "function attachmentHtml("),
            "providers": "let providerCatalog=" + section(self.html, "let providerCatalog=", "/* ---------- SSH"),
            "poll": "setInterval(()=>{\n if(!S.sid" + section(self.html, "setInterval(()=>{\n if(!S.sid", "/* ---------- drawer"),
        }
        # Include the complete inline scripts in the syntax gate (without executing UI).
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", self.html)
        payload = json.dumps({"pieces": pieces, "scripts": scripts})
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "test.cjs"
            harness.write_text(NODE_HARNESS, encoding="utf-8")
            result = subprocess.run(["node", str(harness)], input=payload, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("native UI behavior passed", result.stdout)


NODE_HARNESS = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
for(const script of input.scripts)new vm.Script(script);
class Events {
 constructor(){this.listeners=new Map()}
 addEventListener(name,fn){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(fn)}
 removeEventListener(name,fn){this.listeners.get(name)?.delete(fn)}
 dispatchEvent(e){for(const fn of [...(this.listeners.get(e.type)||[])])fn(e)}
 count(name){return this.listeners.get(name)?.size||0}
}
const window=new Events(),document=new Events(),elements=new Map(),calls=[],seen=[],pending=[],logs=[];
window.KumoNativeBackend=true;document.hidden=false;
const S={dir:'/workspace',sid:'session',live:false,view:0,provs:[],sessions:[],projects:[],errors:[],efforts:{},agent:'build',webSearch:false};
let reply=()=>null,syncs=0,renders=0,interval,browserFetches=0,eventSources=0;
function element(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',hidden:false,open:false,disabled:false,focus:()=>{},classList:{contains:()=>false},querySelector:()=>null});return elements.get(id)}
window.webkit={messageHandlers:{codexNative:{postMessage:r=>{
 calls.push(JSON.parse(JSON.stringify(r)));
 if(r.action==='backendSubscribe'){pending.push(calls.at(-1));return}
 try{const result=reply(r);Promise.resolve(result).then(value=>window.codexNativeResult(r.id,value),e=>window.codexNativeResult(r.id,{error:e}))}
 catch(e){window.codexNativeResult(r.id,{error:e})}
}}}};
const context=vm.createContext({window,document,S,console:{log:(...args)=>logs.push(args.join(' ')),warn:(...args)=>logs.push(args.join(' ')),error:(...args)=>logs.push(args.join(' '))},URL,location:{origin:'null'},crypto:require('node:crypto').webcrypto,
 setTimeout,clearTimeout,setInterval:fn=>{interval=fn},
 fetch:()=>{browserFetches++;throw Error('Unexpected browser fetch')},EventSource:class{constructor(){eventSources++;throw Error('Unexpected EventSource')}},
 err:s=>S.errors.push(s),onEvent:e=>seen.push(e),syncSession:()=>{syncs++;return Promise.resolve()},
 $:element,esc:s=>String(s??''),sheet:html=>{element('#sheet').innerHTML=html},I:{},
 lset:()=>{},closeAll:()=>{},settingsSheet:()=>{},chips:()=>{},render:()=>{renders++},renderDrawer:()=>{},
 loadProviders:async()=>{S.provs=[{id:'configured'}]},loadProjects:async()=>{},loadSessions:async()=>{},loadConnections:async()=>{},
 modelSupportsXhigh:()=>false,defaultReasoningVariants:()=>({}),selectedConnection:()=>null
});
for(const key of ['redact','bridge','api','spec','events','boot','providers','search'])vm.runInContext(input.pieces[key],context);
vm.runInContext(input.pieces.poll,context);
const run=code=>vm.runInContext(code,context),tick=()=>new Promise(r=>setImmediate(r));
function settle(index,value){window.codexNativeResult(pending[index].id,value)}
(async()=>{
 // The real WK shim -> nativeCall -> codexNativeResult must preserve Any payloads.
 for(const value of [null,true,42,'text',['model'],{all:[],connected:[],default:{}}]){
  reply=()=>value;assert.deepEqual(await run("api('/session')"),value);
 }
 reply=()=>({ok:true});
 await run("api('/session?directory=other',{method:'POST',directory:'/explicit',body:{parts:[]}})");
 assert.deepEqual(calls.at(-1).data,{path:'/session?directory=other',method:'POST',directory:'/explicit',body:{parts:[]}});
 await run("api('/global/config',{directory:null})");assert.equal(calls.at(-1).data.directory,null);
 await run("api('/auth/test',{method:'PUT',body:{type:'api',key:'sk-test-secret'}})");
 assert.equal(calls.at(-1).action,'backendRequest');
 for(const error of ['Readable failure',{message:'Readable failure'},{data:{message:'Readable failure'}},{unexpected:true}]){
  reply=()=>({error});await assert.rejects(run("api('/session')"),e=>e.message.includes('GET /session -> ')&&!e.message.includes('[object Object]'));
 }
 reply=()=>({error:{message:'sk-test-secret failure'}});
 await assert.rejects(run("api('/session')"),e=>!e.message.includes('sk-test-secret'));
 await assert.rejects(run("api('/auth/test',{method:'PUT'})"),e=>e.message==='PUT /auth/test -> native backend error');
 reply=()=>({all:[{id:'configured',models:{}}],connected:['configured'],default:{}});
 assert.equal((await run('catalog()'))[0].id,'configured');
 // Subscribe listener is synchronous, events before acknowledgement are not lost.
 run('connectSSE()');assert.equal(window.count('kumoBackendEvent'),1);assert.equal(S.live,false);
 const event={type:'message.part.delta',properties:{sessionID:'session',messageID:'msg',partID:'p',delta:'hello'}};
 window.dispatchEvent({type:'kumoBackendEvent',detail:event});assert.equal(seen.length,1);assert.equal(seen[0],event);
 settle(0,{subscribed:true});await tick();assert.equal(S.live,true);assert.equal(syncs,1);
 assert.deepEqual(pending[0].data,{});
 run('connectSSE();connectSSE()');assert.equal(window.count('kumoBackendEvent'),1);
 settle(1,{subscribed:true});await tick();assert.equal(S.live,false);assert.equal(syncs,1);
 settle(2,{error:{message:'subscribe failed'}});await tick();assert.equal(S.live,false);
 run('recycleSSE()');assert.equal(window.count('kumoBackendEvent'),1);
 window.dispatchEvent({type:'kumoBackendEvent',detail:event});assert.equal(seen.length,2);
 settle(3,{subscribed:true});await tick();assert.equal(S.live,true);assert.equal(syncs,2);
 // No native heartbeat / EventSource assumptions when the page resumes.
 run('lastEv=1');S.running=true;interval();assert.equal(pending.length,4);
 document.dispatchEvent({type:'visibilitychange'});assert.equal(pending.length,4);
 window.dispatchEvent({type:'pagehide'});assert.equal(window.count('kumoBackendEvent'),0);assert.equal(S.live,false);
 window.dispatchEvent({type:'pageshow',persisted:true});assert.equal(window.count('kumoBackendEvent'),1);
 settle(4,{subscribed:true});await tick();
 // Bad acknowledgements fail, and resuming retries even without an open session.
 run('connectSSE()');settle(5,{subscribed:false});await tick();assert.equal(S.live,false);
 S.sid=null;document.dispatchEvent({type:'visibilitychange'});assert.equal(pending.length,7);
 settle(6,{subscribed:true});await tick();
 // Boot uses /doc through the bridge at a file URL origin, preserving the render flow.
 S.dir='';S.spec=null;
 reply=r=>r.data.path==='/doc'?{paths:{'/session':{get:{},post:{}}}}:r.data.path==='/path'?{directory:'/native'}:{healthy:true};
 await run('boot()');assert.equal(S.dir,'/native');assert.ok(S.spec.paths);assert.equal(renders,1);
 assert.ok(calls.some(c=>c.action==='backendRequest'&&c.data.path==='/doc'));
 settle(7,{subscribed:true});await tick();
 // The actual provider form: manual IDs are visible before saving and bridge failure
 // falls back to those IDs, without fetching CORS or sending a browser secret header.
 reply=r=>r.action==='providerModels'?{error:'No model catalog'}:r.data.path==='/provider'?{all:[],connected:[],default:{}}:{};
 run('providerCatalog=null;providerMeta={}');await run('providerForm()');
 assert.match(element('#sheet').innerHTML,/<details id="pmManual" >/);
 assert.match(element('#sheet').innerHTML,/<input class="in" id="pContext" type="text"/);
 element('#pi').value='custom';element('#pu').value='https://provider.example/v1';element('#pf').value='@ai-sdk/openai-compatible';
 element('#pk').value='sk-test-secret';element('#pm').value='manual-one, manual-two';element('#pnn').value='Custom';
 const writes=()=>calls.filter(c=>c.action==='backendRequest'&&['PUT','PATCH'].includes(c.data.method)).length;
 const beforeMissing=writes();element('#pContext').value='';element('#pmManual').open=false;
 await element('#sheet').onclick({target:{id:'ps'}});
 assert.equal(writes(),beforeMissing);assert.match(element('#pe').textContent,/manual-one.*context/i);
 assert.equal(element('#pmManual').hidden,false);assert.equal(element('#pmManual').open,true);
 assert.equal(element('#ps').disabled,false);assert.equal(element('#pk').value,'sk-test-secret');
 for(const invalid of ['0','8192junk','1.5','100000000']){
  element('#pContext').value=invalid;await element('#sheet').onclick({target:{id:'ps'}});
  assert.equal(writes(),beforeMissing);assert.match(element('#pe').textContent,/manual-one.*context/i);
 }
 element('#pContext').value='32768';
 await element('#sheet').onclick({target:{id:'ps'}});
 const modelRequest=calls.findLast(c=>c.action==='providerModels');assert.deepEqual(modelRequest.data,{baseURL:'https://provider.example/v1',key:'sk-test-secret'});
 const saved=calls.findLast(c=>c.action==='backendRequest'&&c.data.path==='/global/config'&&c.data.method==='PATCH');
 assert.deepEqual(Object.keys(saved.data.body.provider.custom.models),['manual-one','manual-two']);
 for(const model of Object.values(saved.data.body.provider.custom.models))assert.equal(model.limit.context,32768);
 assert.ok(calls.some(c=>c.data.path==='/auth/custom'&&c.data.method==='PUT'&&c.data.body.key==='sk-test-secret'));
 settle(8,{subscribed:true});await tick();
 // Limits are per-model metadata first; the explicit user fallback fills gaps only.
 const limits=input.pieces.providers.match(/ function nativeProviderLimits\(modelID,saved,fetched,fallback\)\{[\s\S]*?\n \}/)[0];run(limits);
 const limit=(saved,fetched,fallback)=>run('nativeProviderLimits('+JSON.stringify('model-x')+','+JSON.stringify(saved)+','+JSON.stringify(fetched)+','+JSON.stringify(fallback)+')');
 for(const value of ['', '0','-1','1.5','8192junk','1e4','0x1000','Infinity','NaN','100000000','100000001']){
  assert.throws(()=>limit(null,null,value),/model-x.*context/i);
 }
 assert.equal(limit(null,null,' 99999999 ').context,99999999);
 assert.equal(limit(null,null,'1').context,1);
 assert.equal(limit({context:64000,output:4096,input:63000},{context:128000,output:8192},'junk').context,64000);
 assert.equal(limit({context:64000,output:4096,input:63000},{context:128000,output:8192},'junk').output,4096);
 assert.equal(limit({context:64000,input:63000},null,'').input,63000);
 assert.equal(limit({output:4096},{context:128000},'').context,128000);
 assert.equal(limit({context:null},{context:128000},'').context,128000);
 assert.equal(limit({output:4096},null,'32768').output,4096);
 for(const output of [0,-1,1.5,32768,32769,'4096'])assert.throws(()=>limit({output},null,'32768'),/model-x.*output/i);
 for(const context of [0,-1,1.5,100000000,'8192'])assert.throws(()=>limit({context},null,'32768'),/model-x.*context/i);
 // Real save with heterogeneous saved/fetched metadata and a gap: preserve own
 // provider limits (never borrow another provider's matching model ID).
 const savedLimits={saved:{limit:{context:64000,output:4096,input:63000}},missing:{limit:{output:1024}}};
 const fetchedModels=[{id:'saved',context_length:128000,output:8192},{id:'fetched',context_length:96000,output:2048},{id:'missing'}];
 reply=r=>r.action==='providerModels'?{data:fetchedModels}:r.data.path==='/global/config'&&r.data.method==='GET'?{provider:{custom:{models:savedLimits}}}:r.data.path==='/provider'?{all:[{id:'other',models:{missing:{id:'missing',limit:{context:77777}}}}],connected:[],default:{}}:{};
 run('providerCatalog=null');await run('providerForm()');
 element('#pm').value='';element('#pContext').value='16384';element('#pk').value='';
 await element('#sheet').onclick({target:{id:'ps'}});
 const heterogeneous=calls.findLast(c=>c.data.path==='/global/config'&&c.data.method==='PATCH').data.body.provider.custom.models;
 assert.deepEqual(heterogeneous.saved.limit,{context:64000,output:4096,input:63000});
 assert.deepEqual(heterogeneous.fetched.limit,{context:96000,output:2048});
 assert.deepEqual(heterogeneous.missing.limit,{context:16384,output:1024});
 settle(9,{subscribed:true});await tick();
 // Invalid output must stop the actual save, not merely fail a helper test.
 fetchedModels[1].output=96000;await run('providerForm()');
 const beforeOutput=writes();await element('#sheet').onclick({target:{id:'ps'}});
 assert.equal(writes(),beforeOutput);assert.match(element('#pe').textContent,/fetched.*output/i);
 // A later edit with no provider metadata needs no fallback when saved limits exist.
 const persisted=heterogeneous;
 reply=r=>r.action==='providerModels'?{data:['saved','fetched','missing']}:r.data.path==='/global/config'&&r.data.method==='GET'?{provider:{custom:{models:persisted}}}:{};
 await run('providerForm()');element('#pContext').value='';
 await element('#sheet').onclick({target:{id:'ps'}});
 const edited=calls.findLast(c=>c.data.path==='/global/config'&&c.data.method==='PATCH').data.body.provider.custom.models;
 for(const id of Object.keys(persisted))assert.deepEqual(edited[id].limit,persisted[id].limit);
 settle(10,{subscribed:true});await tick();
 // Native search cannot be enabled, even with old On state in local storage.
 S.webSearch=true;element('#plus').onclick();
 assert.match(element('#sheet').innerHTML,/<button class="opt" id="attachSearch" disabled[^>]*>Web search<span class="m">Not supported/);
 run('toggleSearch()');assert.equal(S.webSearch,true);assert.match(element('#sheet').innerHTML,/not supported/);
 window.KumoNativeBackend=false;await run('providerForm()');
 assert.doesNotMatch(element('#sheet').innerHTML,/id="pContext"/);
 element('#plus').onclick();assert.match(element('#sheet').innerHTML,/<button class="opt" id="attachSearch" >Web search<span class="m">On/);
 window.KumoNativeBackend=true;
 // Test the actual nested helper independently with returned model shapes and failure.
 const helper=input.pieces.providers.match(/ async function fetchProviderModels\(url,key\)\{[\s\S]*?\n \}/)[0];
 run('const fetchProviderModels=(function(existing){'+helper+';return fetchProviderModels})()');
 for(const value of [{data:[{id:'model',context_length:8192}]},{models:['model']},['model']]){
  reply=()=>value;const models=await run("fetchProviderModels('https://provider.example/v1','sk-test-secret')");assert.equal(models[0].id,'model');
 }
 reply=()=>({data:[{id:'model',limit:{context:32768,output:4096}}]});
 const metadata=await run("fetchProviderModels('https://provider.example/v1','')");
 assert.equal(metadata[0].context,32768);assert.equal(metadata[0].output,4096);
 for(const value of [null,{data:[]},{data:[{}]}]){
  reply=()=>value;assert.equal(await run("fetchProviderModels('https://provider.example/v1','sk-test-secret')"),null);
 }
 reply=()=>({error:{message:'failed'}});
 await assert.rejects(run("fetchProviderModels('https://provider.example/v1','sk-test-secret')"),e=>e.message==='failed');

 // HTTPS validation happens before any providerModels action.
 await run('providerForm()');element('#pu').value='http://provider.example/v1';
 const count=calls.filter(c=>c.action==='providerModels').length;await element('#sheet').onclick({target:{id:'ps'}});
 assert.equal(calls.filter(c=>c.action==='providerModels').length,count);assert.match(element('#pe').textContent,/HTTPS/);
 // Save failures expose the native reason, keep the exact entered key, and never
 // leave Fetching… behind. The same failed discovery can still save manual IDs.
 const enteredKey='  sk-test-secret  ',format='@ai-sdk/openai-compatible';
 const providerErrors=[
  'HTTP 401: The API key was rejected by the provider.',
  {message:'HTTP 404: Model discovery endpoint /v1/models was not found.'},
  {data:{message:'NSURLErrorDomain (-1003): A server with the specified hostname could not be found.'}}
 ];
 function fillProvider(manual=''){
  element('#pi').value='agentrouter';element('#pnn').value='AgentRouter';element('#pu').value='https://agentrouter.org/v1';
  element('#pf').value=format;element('#pk').value=enteredKey;element('#pm').value=manual;element('#pContext').value='32768';
 }
 function assertNoKeyShown(){
  for(const text of [element('#pe').textContent,element('#pmStatus').textContent,...S.errors,...logs])assert.ok(!text.includes('sk-test-secret'),text);
 }
 for(const nativeError of providerErrors){
  const message=typeof nativeError==='string'?nativeError:nativeError.data?.message||nativeError.message;
  reply=r=>r.action==='providerModels'?{error:nativeError}:r.data.path==='/provider'?{all:[],connected:[],default:{}}:{};
  run('providerCatalog=null;providerMeta={}');await run('providerForm()');fillProvider();
  const before=writes();await element('#sheet').onclick({target:{id:'ps'}});
  assert.equal(writes(),before);assert.equal(element('#pe').textContent,message);
  assert.equal(element('#pmStatus').textContent,'Model list unavailable: '+message+'. Manual IDs can still be used.');
  assert.equal(element('#pk').value,enteredKey);assert.equal(element('#ps').disabled,false);assert.equal(element('#pf').value,format);assertNoKeyShown();
  element('#pm').value='manual-only';await element('#sheet').onclick({target:{id:'ps'}});
  assert.equal(writes(),before+2);
  const provider=calls.findLast(c=>c.data.path==='/global/config'&&c.data.method==='PATCH').data.body.provider.agentrouter;
  assert.deepEqual(Object.keys(provider.models),['manual-only']);assert.equal(provider.models['manual-only'].limit.context,32768);assert.equal(provider.npm,format);
  assert.equal(element('#pk').value,'');assert.match(element('#pmStatus').textContent,/Manual IDs can still be used/);assertNoKeyShown();
  settle(pending.length-1,{subscribed:true});await tick();
 }
 for(const result of [null,{data:[]},{error:{message:'HTTP 401: Authorization: Bearer sk-test-secret rejected'}}]){
  reply=r=>r.action==='providerModels'?result:r.data.path==='/provider'?{all:[],connected:[],default:{}}:{};
  run('providerCatalog=null');await run('providerForm()');fillProvider();
  const before=writes();await element('#sheet').onclick({target:{id:'ps'}});
  assert.equal(writes(),before);assert.equal(element('#pk').value,enteredKey);assert.equal(element('#ps').disabled,false);
  assert.doesNotMatch(element('#pe').textContent,/Check the key/);assert.match(element('#pmStatus').textContent,/^Model list unavailable: .* Manual IDs can still be used\.$/);assertNoKeyShown();
 }
 // Manual IDs win even when discovery succeeds, retaining fetched metadata only
 // for selected IDs. A retry must not retain the previous modelFetchError.
 reply=r=>r.action==='providerModels'?{data:[{id:'manual-only',context:96000,output:4096},{id:'not-selected',context:64000}]}:{};
 fillProvider('manual-only, undiscovered');await element('#sheet').onclick({target:{id:'ps'}});
 const manualProvider=calls.findLast(c=>c.data.path==='/global/config'&&c.data.method==='PATCH').data.body.provider.agentrouter;
 assert.deepEqual(Object.keys(manualProvider.models),['manual-only','undiscovered']);assert.deepEqual(manualProvider.models['manual-only'].limit,{context:96000,output:4096});
 assert.equal(manualProvider.models.undiscovered.limit.context,32768);assert.equal(manualProvider.npm,format);
 assert.doesNotMatch(element('#pmStatus').textContent,/unavailable|Fetching/);settle(pending.length-1,{subscribed:true});await tick();
 // Configured IDs need neither /models nor a catalog entry when saved limits exist.
 const configuredModel={limit:{context:64000,output:4096}};
 reply=r=>r.action==='providerModels'?{error:providerErrors[0]}:r.data.path==='/provider'?{all:[],connected:[],default:{}}:r.data.path==='/global/config'&&r.data.method==='GET'?{provider:{agentrouter:{models:{configured:configuredModel}}}}:{};
 run('providerCatalog=null');await run('providerForm()');fillProvider();element('#pContext').value='';
 await element('#sheet').onclick({target:{id:'ps'}});
 const configuredProvider=calls.findLast(c=>c.data.path==='/global/config'&&c.data.method==='PATCH').data.body.provider.agentrouter;
 assert.deepEqual(Object.keys(configuredProvider.models),['configured']);assert.deepEqual(configuredProvider.models.configured.limit,configuredModel.limit);
 settle(pending.length-1,{subscribed:true});await tick();
 // Config route details are backend-sanitized, but the UI still redacts auth tokens.
 const validation='Validation failed: provider.agentrouter.models.manual-only.limit.context must be positive.';
 for(const path of ['/config','/global/config'])for(const method of ['GET','PATCH']){
  reply=()=>({error:{data:{message:validation+' Authorization: Bearer sk-test-secret'}}});
  await assert.rejects(run('api('+JSON.stringify(path)+',{method:'+JSON.stringify(method)+'})'),e=>e.message===method+' '+path+' -> '+validation+' Authorization: [redacted]');
 }
 for(const failingMethod of ['GET','PATCH']){
  reply=r=>r.action==='providerModels'?{data:[{id:'manual-only',context:32768}]}:r.data.path==='/global/config'&&r.data.method===failingMethod?{error:{message:validation+' Authorization: Bearer sk-test-secret'}}:{};
  await run('providerForm()');fillProvider('manual-only');await element('#sheet').onclick({target:{id:'ps'}});
  assert.equal(element('#pe').textContent,failingMethod+' /global/config -> '+validation+' Authorization: [redacted]');
  assert.equal(element('#pk').value,enteredKey);assert.equal(element('#ps').disabled,false);assert.equal(element('#pf').value,format);assertNoKeyShown();
 }
 // Validation before discovery must not clear an entered key either.
 fillProvider();element('#pu').value='http://agentrouter.org/v1';await element('#sheet').onclick({target:{id:'ps'}});
 assert.match(element('#pe').textContent,/HTTPS/);assert.equal(element('#pk').value,enteredKey);assertNoKeyShown();
 // Saved credentials are requested only by an edit's original provider ID; a
 // matching ID typed into a new form must not opt into saved-key discovery.
 run("providerMeta={agentrouter:{baseURL:'https://agentrouter.org/v1',models:[]}};providerCatalog=null");
 reply=r=>r.action==='providerModels'?{data:[{id:'discovered',context:32768}]}:r.data.path==='/provider'?{all:[],connected:[],default:{}}:{};
 await run('providerForm()');fillProvider();element('#pk').value='';
 await element('#sheet').onclick({target:{id:'ps'}});
 settle(pending.length-1,{subscribed:true});await tick();
 assert.deepEqual(calls.findLast(c=>c.action==='providerModels').data,{baseURL:'https://agentrouter.org/v1',key:''});
 await run("providerForm('agentrouter')");fillProvider();element('#pk').value='';element('#pi').value='renamed';
 const beforeEdit=writes();await element('#sheet').onclick({target:{id:'ps'}});
 settle(pending.length-1,{subscribed:true});await tick();
 assert.deepEqual(calls.findLast(c=>c.action==='providerModels').data,{baseURL:'https://agentrouter.org/v1',key:'',providerID:'agentrouter'});
 assert.equal(writes(),beforeEdit+1);assert.equal(element('#pk').value,'');
 for(const nativeError of providerErrors){
  reply=r=>r.action==='providerModels'?{error:nativeError}:r.data.path==='/provider'?{all:[],connected:[],default:{}}:{};
  await run("providerForm('agentrouter')");fillProvider();element('#pk').value='';
  const before=writes();await element('#sheet').onclick({target:{id:'ps'}});
  const message=typeof nativeError==='string'?nativeError:nativeError.data?.message||nativeError.message;
  assert.equal(writes(),before);assert.equal(element('#pe').textContent,message);assert.equal(element('#pk').value,'');
  assert.deepEqual(calls.findLast(c=>c.action==='providerModels').data,{baseURL:'https://agentrouter.org/v1',key:'',providerID:'agentrouter'});
  element('#pm').value='manual-only';await element('#sheet').onclick({target:{id:'ps'}});
  assert.equal(writes(),before+1);
  const provider=calls.findLast(c=>c.data.path==='/global/config'&&c.data.method==='PATCH').data.body.provider.agentrouter;
  assert.deepEqual(Object.keys(provider.models),['manual-only']);assert.match(element('#pmStatus').textContent,/Manual IDs can still be used/);assertNoKeyShown();
  settle(pending.length-1,{subscribed:true});await tick();
 }
 window.dispatchEvent({type:'pagehide'});
 // A missing or throwing transport fails immediately, not after the bridge timeout.
 const transport=window.CodexNative;
 window.CodexNative={request:()=>{throw Error('Transport failed')}};
 await assert.rejects(run("api('/session')"),/Transport failed/);
 assert.equal(run('nativePending.size'),0);
 delete window.CodexNative;
 await assert.rejects(run("api('/session')"),/requires the native app bridge/);
 window.CodexNative=transport;
 assert.equal(browserFetches,0);assert.equal(eventSources,0);
 console.log('native UI behavior passed');
})().catch(e=>{console.error(e);process.exitCode=1});
"""

if __name__ == "__main__":
    unittest.main()
