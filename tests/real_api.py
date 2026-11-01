"""Playwright against a running opencode; only the asset HTML is intercepted.

API requests and SSE are always served by opencode. No mock, provider HTTP calls,
request headers, credentials, or full provider errors are printed or recorded.
"""
import argparse
import asyncio
import base64
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import async_playwright


async def run(args):
    root = Path(__file__).resolve().parents[1]
    project = str(Path(args.project).resolve())
    encoded = base64.urlsafe_b64encode(project.encode()).decode().rstrip("=")
    result = {"mode": args.mode, "server": args.server, "checks": []}
    proof = 'proof-' + uuid4().hex[:8] + '.txt'
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 800})
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(type(e).__name__))
        page.on("response", lambda r: requests.append({
            "path": urlparse(r.url).path, "method": r.request.method,
            "status": r.status,
        }))
        await page.add_init_script("""(() => {
          window.realEvents = [];
          const Original = window.EventSource;
          window.EventSource = class extends Original {
            constructor(...args) {
              super(...args);
              this.addEventListener('message', e => {
                try {
                  const v = JSON.parse(e.data), p = v.properties || {};
                  window.realEvents.push({type:v.type,keys:Object.keys(p),
                    sessionID:p.sessionID || p.info?.sessionID || p.part?.sessionID,
                    status:p.status?.type,errorName:p.error?.name,
                    retryIndicators:['401','403','429','500','502','503','504',
                      'timeout','timed out','certificate','dns','fetch failed','overloaded']
                      .filter(term=>String(p.status?.message||'').toLowerCase().includes(term))});
                } catch (_) {}
              });
            }
          };
        })();""")
        await page.route("**/_ui/index.html", lambda route: route.fulfill(
            path=str(root / "app/src/main/assets/codex.html"),
            content_type="text/html; charset=utf-8"))
        await page.goto(args.server + "/_ui/index.html#d=" + encoded)
        await page.wait_for_function("S.spec && S.live && S.provs.length > 0")
        health = await page.evaluate("S.health")
        assert health["version"] == "1.19.3" and health["healthy"]
        result["checks"].append("health_1.19.3")
        models = await page.evaluate("allModels()")
        model = next((m for m in models if m["p"] == args.provider
                      and m["m"] == args.model), None)
        assert model is not None, "Requested model is absent from /config/providers"
        result["model"] = {"providerID": model["p"], "modelID": model["m"]}
        await page.click("#cModel")
        await page.locator("#sheet .opt").nth(models.index(model)).click()

        async def send(text):
            await page.fill("#inp", text)
            async with page.expect_response(lambda r:
                    r.request.method == "POST" and
                    urlparse(r.url).path.endswith("/prompt_async")) as response:
                await page.click("#send")
            assert (await response.value).status == 204

        try:
            await send("Reply with exactly CODEX_REAL_OK. Do not use tools.")
            await page.wait_for_function("S.status.type==='retry'" if args.mode=='retry' else "!S.running && (document.querySelector('.msg.ai') || S.lastErr)",
                                         timeout=args.timeout * 1000)
            text = await page.locator(".msg.ai").all_text_contents()
            result["replyContainsMarker"] = any("CODEX_REAL_OK" in t for t in text)
            result["sessionID"] = await page.evaluate("S.sid")
            result["workingVisible"] = await page.locator(".work").count() > 0
            result["errorNames"] = await page.evaluate("[...S.msgs.values()].map(m=>m.info.error?.name).filter(Boolean)")
            if args.mode == 'retry':
                assert 'Retry' in await page.locator('.work').inner_text()
                result['retryAttempt'] = await page.evaluate('S.status.attempt')
                async with page.expect_response(lambda r: urlparse(r.url).path.endswith('/abort')) as aborted:
                    await page.click('#send')
                assert (await aborted.value).status == 200
                await page.wait_for_function('!S.running', timeout=15000)
                await page.evaluate('id=>openSession(id)', result['sessionID'])
                assert not await page.evaluate('S.running')
                result['workingVisible'] = await page.locator('.work').count() > 0
                result['checks'].append('real_retry_label_abort_and_reopen')
            else:
                assert result["replyContainsMarker"], "No model answer marker"
                result["checks"].append("real_prompt_answer_and_idle")

            if args.mode == "suite":
                await page.evaluate("""async () => {
                  const s=await api('/session',{method:'POST',body:{title:'API tool verification',
                    permission:[{permission:'bash',pattern:'*',action:'ask'},
                                {permission:'edit',pattern:'*',action:'ask'}]}});
                  S.sessions.unshift(s); await openSession(s.id);
                }""")
                await send("Use the bash tool to run echo CODEX_COMMAND_OK. Then use the write tool "
                           f"to create {proof} in the current project directory containing exactly "
                           "CODEX_DIFF_OK followed by a newline. Do not use shell redirection to write it. "
                           "Do not read or change any other files. Finally reply CODEX_TOOL_OK.")
                approvals = 0
                for _ in range(args.timeout * 2):
                    if await page.locator('[data-r="once"]').count():
                        await page.locator('[data-r="once"]').first.click()
                        approvals += 1
                    if await page.evaluate("!S.running"):
                        break
                    await asyncio.sleep(0.5)
                assert not await page.evaluate("S.running"), "Tool prompt remained busy"
                assert approvals >= 1, "No real permission was requested"
                assert "CODEX_DIFF_OK" in (Path(project) / proof).read_text()
                assert await page.locator(".card").filter(has_text="CODEX_COMMAND_OK").count()
                result["approvals"] = approvals
                result["checks"].append("real_tool_command_and_approve")
                await page.click("#commit")
                await page.locator("#sheet .opt").filter(has_text=proof).click()
                assert "CODEX_DIFF_OK" in await page.locator("#sheet").inner_text()
                result["checks"].append("real_tool_patch_rendered")
                await page.click("#dim", position={"x": 380, "y": 30})

                await send("Use the bash tool to run echo CODEX_DENY_CHECK. Do not use any other "
                           "tools. If permission is rejected, stop and reply DENIED.")
                await page.locator('[data-r="reject"]').first.wait_for(timeout=args.timeout * 1000)
                await page.locator('[data-r="reject"]').first.click()
                await page.wait_for_function("!S.running", timeout=args.timeout * 1000)
                assert await page.locator("[data-pid]").count() == 0
                result["checks"].append("real_permission_deny")

                # Resume must use the server's status, including an aborted session.
                sid = await page.evaluate("S.sid")
                await page.evaluate("async () => { await api('/session/'+S.sid+'/abort',{method:'POST'}); await openSession(S.sid); }")
                assert not await page.evaluate("S.running")
                await page.click("#menu")
                await page.locator('[data-a="new"]').click()
                assert not await page.evaluate("S.running || S.perms.size || S.pendingUser || S.lastErr")
                await page.evaluate("id=>openSession(id)", sid)
                result["checks"].append("abort_reopen_and_new_thread_state")
                first_sid = result['sessionID']
                await page.evaluate("async ([a,b])=>Promise.all([openSession(a),openSession(b)])", [first_sid, sid])
                assert await page.evaluate("[...S.msgs.values()].every(m=>m.info.sessionID===S.sid)")
                result["checks"].append("concurrent_session_switch")
                await page.evaluate("S.model={p:'codex-missing-provider',m:'missing-model'}")
                await send("Reply OK.")
                await page.wait_for_function("!S.running && (S.lastErr || [...S.msgs.values()].some(m=>m.info.error))", timeout=30000)
                assert await page.locator('.work').count() == 0
                result["checks"].append("real_session_error_stops_working")

            elif args.mode == "provider":
                await page.click("#gear")
                await page.click("#pv")
                await page.fill("#pi", "agentrouter")
                await page.fill("#pu", "https://agentrouter.org/v1")
                await page.fill("#pm", args.router_model)
                # Real user keys are entered only by the user; keep the field empty.
                await page.click("#ps")
                await page.wait_for_function("S.provs.some(p=>p.id==='agentrouter')", timeout=60000)
                assert args.router_model in await page.evaluate("S.provs.find(p=>p.id==='agentrouter').models.map(m=>m.id)")
                result['configuredProvider'] = await page.evaluate("(()=>{const p=S.provs.find(p=>p.id==='agentrouter');return {id:p.id,models:p.models.map(m=>m.id)}})()")
                result["checks"].append("agentrouter_in_real_config_providers_without_key")
                # Exercise PUT/DELETE on an isolated probe identity. Secret only in memory.
                import secrets
                probe = "codex-api-probe-" + secrets.token_hex(5)
                key = secrets.token_urlsafe(24)
                try:
                    assert await page.evaluate("async ({id,key})=>api('/auth/'+id,{method:'PUT',body:{type:'api',key}})",
                                               {"id": probe, "key": key}) is True
                    result["checks"].append("real_put_auth_api_shape")
                finally:
                    await page.evaluate("async id=>api('/auth/'+id,{method:'DELETE'})", probe)
                assert await page.evaluate("localStorage.getItem('oc_provider_key')") is None

        except Exception as exc:
            result["failure"] = type(exc).__name__
            result["errorNames"] = await page.evaluate("[...S.msgs.values()].map(m=>m.info.error?.name).filter(Boolean)")
            result["workingVisible"] = await page.locator(".work").count() > 0
            if await page.evaluate("S.sid && S.running"):
                await page.evaluate("async ()=>api('/session/'+S.sid+'/abort',{method:'POST'})")
        events = await page.evaluate("window.realEvents")
        result["events"] = dict(Counter(e["type"] for e in events))
        result["eventShapes"] = {e["type"]: e["keys"] for e in events}
        result["sessionErrors"] = [e.get("errorName") for e in events if e["type"] == "session.error"]
        result["statuses"] = list(dict.fromkeys(e["status"] for e in events if e.get("status")))
        result["retryIndicators"] = list(dict.fromkeys(term for e in events for term in e.get("retryIndicators", [])))
        result["requests"] = requests
        result["pageErrors"] = errors
        result["pass"] = "failure" not in result and not errors
        await browser.close()
    if args.report:
        Path(args.report).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("requests", "eventShapes")}, ensure_ascii=True))
    return result["pass"]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--provider", default="opencode")
    p.add_argument("--model", default="big-pickle")
    p.add_argument("--mode", choices=["baseline", "suite", "provider", "retry"], default="baseline")
    p.add_argument("--router-model", default="gpt-5.6-sol")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--report")
    raise SystemExit(0 if asyncio.run(run(p.parse_args())) else 1)
