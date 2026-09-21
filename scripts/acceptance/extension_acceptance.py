"""Extension acceptance (A-J) against Brave/Chromium 153 with the unpacked extension and the real Core.
Drives the content-script UI with real mouse events; verifies every save through the Core HTTP API."""
import asyncio, json, os, sys, time, urllib.request, subprocess
from playwright.async_api import async_playwright

CORE = os.environ.get("MAGPIE_CORE", "http://127.0.0.1:8765")
WIKI = "https://en.wikipedia.org/wiki/Letterpress_printing"
WECHAT = "https://mp.weixin.qq.com/s/BGXeAXrEo6CM5-gtdQOuYA"
RESULTS = []
CREATED = []  # material ids created by this run (for later inspection / cleanup notes)


def rec(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def core(path, method="GET", body=None):
    req = urllib.request.Request(CORE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return None, str(e)


def newest_materials(n=5):
    st, d = core(f"/materials?limit={n}&sort=newest")
    return d.get("items", []) if st == 200 else []


def wait_ready(mid, limit=90):
    t0 = time.time()
    while time.time() - t0 < limit:
        st, m = core(f"/materials/{mid}")
        if st == 200 and m["processing"]["status"] in ("ready", "failed"):
            return m
        time.sleep(2)
    return core(f"/materials/{mid}")[1]


HOVER_JS = """(sel) => { const h = document.getElementById('magpie-hover-host'); if (!h) return null;
  const el = h.shadowRoot.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect();
  return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height, hidden: el.hidden, text: el.textContent.trim().slice(0,160), cls: el.className}; }"""
OVERLAY_JS = """(sel) => { const h = document.getElementById('magpie-capture-host'); if (!h) return null;
  const el = h.shadowRoot.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect();
  return {x: r.x + r.width/2, y: r.y + r.height/2, hidden: el.hidden, text: el.textContent.trim().slice(0,200), cls: el.className, disabled: el.disabled}; }"""


async def hover_el(page, sel):
    return await page.evaluate(HOVER_JS, sel)


async def overlay_el(page, sel):
    return await page.evaluate(OVERLAY_JS, sel)


async def wait_for(fn, pred, timeout=8000, step=150):
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout:
        v = await fn()
        if pred(v):
            return v
        await asyncio.sleep(step / 1000)
    return await fn()


async def big_image(page, min_side=120, idx=0):
    return await page.evaluate("""([ms, idx]) => { const imgs=[...document.images].filter(i=>{const r=i.getBoundingClientRect(); return r.width>=ms&&r.height>=ms&&i.currentSrc&&!i.currentSrc.startsWith('data:')});
      const i = imgs[idx]; if(!i) return null; i.scrollIntoView({block:'center'}); const r=i.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2,src:i.currentSrc,w:r.width,h:r.height}; }""", [min_side, idx])


async def open_arc(page, img):
    """hover image -> badge appears -> hover badge -> arc opens; returns dict of action centres"""
    await page.mouse.move(5, 5)
    await asyncio.sleep(0.5)  # let a previous anchor/toast hide timer run
    for attempt in range(3):
        await page.mouse.move(img["x"] - 30, img["y"] - 30)
        await page.mouse.move(img["x"] - 10, img["y"] - 10, steps=3)
        await page.mouse.move(img["x"], img["y"], steps=3)
        anchor = await wait_for(lambda: hover_el(page, ".anchor"), lambda v: v and not v["hidden"], 3000)
        if anchor and not anchor["hidden"]:
            break
        await page.mouse.move(5, 5)
        await asyncio.sleep(0.6)
    else:
        return None
    badge = await hover_el(page, ".badge")
    await page.mouse.move(badge["x"], badge["y"])
    await asyncio.sleep(0.45)  # arc transition
    acts = {}
    for a in ("save", "note", "library"):
        acts[a] = await hover_el(page, f'.act[data-act="{a}"]')
    return acts


async def type_toast_thought(page, text):
    """click the toast's thought input, type, Enter; returns True when the toast confirms"""
    for _ in range(3):
        inp = await hover_el(page, ".toast .tinput")
        if not inp:
            return False
        await page.mouse.click(inp["x"], inp["y"])
        await asyncio.sleep(0.2)
        active = await page.evaluate("() => { const h=document.getElementById('magpie-hover-host'); const a=h && h.shadowRoot.activeElement; return a ? a.className : ''; }")
        if active == "tinput":
            break
    await page.keyboard.type(text)
    await page.keyboard.press("Enter")
    done = await wait_for(lambda: hover_el(page, ".toast .tdone"), lambda v: v is not None, 10000)
    return bool(done)


async def click_shadow(page, pos):
    await page.mouse.move(pos["x"], pos["y"])
    await asyncio.sleep(0.12)
    await page.mouse.click(pos["x"], pos["y"])


async def select_text(page, min_len=40):
    """select the first long paragraph with a real drag so the pill logic (mouseup) fires"""
    rect = await page.evaluate("""(minLen) => { const p=[...document.querySelectorAll('p')].find(p=>p.innerText.trim().length>minLen && p.getBoundingClientRect().width>200);
        if(!p) return null; p.scrollIntoView({block:'center'}); const range=document.createRange(); range.selectNodeContents(p); const rects=range.getClientRects();
        const first=rects[0], last=rects[rects.length-1]; return {x1:first.x+2,y1:first.y+first.height/2,x2:last.x+last.width-2,y2:last.y+last.height/2,text:p.innerText.trim().slice(0,80)}; }""", min_len)
    if not rect:
        return None
    await page.mouse.move(rect["x1"], rect["y1"])
    await page.mouse.down()
    await page.mouse.move((rect["x1"] + rect["x2"]) / 2, (rect["y1"] + rect["y2"]) / 2, steps=5)
    await page.mouse.move(rect["x2"], rect["y2"], steps=5)
    await page.mouse.up()
    await asyncio.sleep(0.3)
    sel = await page.evaluate("() => window.getSelection().toString().trim()")
    return sel


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(os.environ.get("MAGPIE_CDP", "http://localhost:9222"))
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.set_viewport_size({"width": 1400, "height": 900})
        ext_ids = {sw.url.split("/")[2] for sw in ctx.service_workers} | set()
        print("service workers:", [sw.url for sw in ctx.service_workers])

        # ------------------------------------------------------------ I. Wikipedia page
        await page.goto(WIKI, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        before = {m["id"] for m in newest_materials(50)}

        # ---- A. image hover -> quick save
        img = await big_image(page)
        acts = await open_arc(page, img)
        rec("A0 hover badge + arc appear", bool(acts and acts["save"] and not acts["save"]["hidden"]), f"img={img['src'][:70]} acts={ {k: bool(v) for k,v in (acts or {}).items()} }")
        await click_shadow(page, acts["save"])
        toast = await wait_for(lambda: hover_el(page, ".toast"), lambda v: v and not v["hidden"] and "已保存" in v["text"], 20000)
        rec("A1 quick save toast", bool(toast and "已保存 ✓ 正在后台分析" in toast["text"]), toast and toast["text"])
        has_input = await hover_el(page, ".toast .tinput")
        rec("A2 toast offers thought input + undo", bool(has_input) and "撤销" in (toast or {}).get("text", ""), "")
        await asyncio.sleep(1.0)
        a_mat = next((m for m in newest_materials(5) if m["id"] not in before and m["modality"] == "image"), None)
        rec("A3 material exists in Core", bool(a_mat), a_mat and f"{a_mat['id']} resource_url={a_mat['source']['resource_url'][:70]} thought={a_mat['human']['thought']!r}")
        if a_mat:
            CREATED.append(a_mat["id"])
            rec("A4 source fields", a_mat["source"]["page_url"] == WIKI and a_mat["source"]["page_title"].startswith("Letterpress") and bool(a_mat["source"]["captured_at"]), json.dumps(a_mat["source"], ensure_ascii=False)[:200])
            # type the thought into the toast input (补一句 Thought)
            done = await type_toast_thought(page, "A: 铅字排版的手工感，测试补一句 Thought")
            await asyncio.sleep(0.5)
            st, m2 = core(f"/materials/{a_mat['id']}")
            rec("A5 toast thought PATCHed to Core", bool(done) and m2["human"]["thought"] == "A: 铅字排版的手工感，测试补一句 Thought", m2["human"]["thought"])
            m2 = wait_ready(a_mat["id"])
            rec("A6 analysis ready", m2["processing"]["status"] == "ready", f"{m2['processing']} summary={str(m2['analysis']['summary'])[:60]} size={m2['objective_metadata'].get('width')}x{m2['objective_metadata'].get('height')}")
        await page.mouse.move(5, 5); await asyncio.sleep(0.5)
        # dismiss toast
        x = await hover_el(page, ".toast .tx")
        if x: await click_shadow(page, x)

        # ---- F. duplicate: quick save the same image again
        acts = await open_arc(page, img)
        await click_shadow(page, acts["save"])
        toast = await wait_for(lambda: hover_el(page, ".toast"), lambda v: v and not v["hidden"] and ("已在素材库" in v["text"] or "已保存" in v["text"]), 20000)
        rec("F1 duplicate image -> 已在素材库里", bool(toast and "已在素材库里" in toast["text"]), toast and toast["text"])
        rec("F2 no second material created", len([m for m in newest_materials(5) if m["id"] not in before and m["modality"] == "image"]) == 1, "")
        x = await hover_el(page, ".toast .tx")
        if x: await click_shadow(page, x)
        await page.mouse.move(5, 5); await asyncio.sleep(0.4)

        # ---- B. second image hover -> 加批注保存 (overlay with thought)
        img2 = await big_image(page, idx=1)
        acts = await open_arc(page, img2)
        rec("B0 arc on second image", bool(acts and acts["note"]), img2 and img2["src"][:70])
        await click_shadow(page, acts["note"])
        inp = await wait_for(lambda: overlay_el(page, ".thought"), lambda v: v is not None, 6000)
        rec("B1 overlay opened with thought input", bool(inp), "")
        await page.mouse.click(inp["x"], inp["y"])
        await page.keyboard.type("B: 老印刷机的金属感，想用在关于工艺的页面")
        await page.keyboard.press("Enter")
        status = await wait_for(lambda: overlay_el(page, ".status"), lambda v: v and not v["hidden"] and ("已保存" in v["text"] or "已在" in v["text"] or "失败" in v["text"] or "无法" in v["text"]), 30000)
        rec("B2 overlay save status", bool(status and "已保存" in status["text"]), status and status["text"])
        b_mat = next((m for m in newest_materials(5) if m["id"] not in before and m["modality"] == "image" and (m["human"]["thought"] or "").startswith("B:")), None)
        rec("B3 thought persisted in Core", bool(b_mat), b_mat and f"{b_mat['id']} {b_mat['human']['thought']!r}")
        if b_mat: CREATED.append(b_mat["id"])
        await page.keyboard.press("Escape"); await asyncio.sleep(0.4)

        # ---- C. select text -> pill -> quick save
        sel = await select_text(page)
        pill = await wait_for(lambda: hover_el(page, ".pill"), lambda v: v and not v["hidden"], 4000)
        rec("C0 selection pill appears", bool(pill and not pill["hidden"]), f"selected={sel[:60]!r}")
        save_b = await hover_el(page, '.pact[data-act="save"]')
        await click_shadow(page, save_b)
        toast = await wait_for(lambda: hover_el(page, ".toast"), lambda v: v and not v["hidden"] and ("已保存" in v["text"] or "已在素材库" in v["text"]), 20000)
        rec("C1 text quick save toast", bool(toast and "已保存" in toast["text"]), toast and toast["text"])
        c_mat = next((m for m in newest_materials(5) if m["id"] not in before and m["modality"] == "text"), None)
        rec("C2 text material in Core matches selection", bool(c_mat) and c_mat["original"]["content"].strip()[:40] == sel[:40], c_mat and f"{c_mat['id']} {c_mat['original']['content'][:60]!r}")
        if c_mat: CREATED.append(c_mat["id"])

        # ---- G. undo from the toast (deletes c_mat)
        undo = await page.evaluate("""() => { const h=document.getElementById('magpie-hover-host'); const b=[...h.shadowRoot.querySelectorAll('.toast .btn')].find(b=>b.textContent.trim()==='撤销'); if(!b) return null; const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; }""")
        rec("G0 undo button present", bool(undo), "")
        if undo:
            await click_shadow(page, undo)
            toast = await wait_for(lambda: hover_el(page, ".toast"), lambda v: v and "已撤销" in v["text"], 10000)
            st, _ = core(f"/materials/{c_mat['id']}")
            rec("G1 undo deletes the material (Core 404)", bool(toast and "已撤销" in toast["text"]) and st == 404, f"toast={toast and toast['text']} core={st}")
        await page.mouse.move(5, 5); await page.mouse.click(5, 300); await asyncio.sleep(0.4)

        # ---- D. select text -> 加批注保存 -> overlay -> thought -> Enter
        sel2 = await select_text(page, min_len=120)
        pill = await wait_for(lambda: hover_el(page, ".pill"), lambda v: v and not v["hidden"], 4000)
        note_b = await hover_el(page, '.pact[data-act="note"]')
        await click_shadow(page, note_b)
        inp = await wait_for(lambda: overlay_el(page, ".thought"), lambda v: v is not None, 6000)
        rec("D0 text overlay opened", bool(inp), f"selected={sel2[:50]!r}")
        await page.mouse.click(inp["x"], inp["y"])
        await page.keyboard.type("D: 这一段讲压印的动作，是物质感的来源")
        await page.keyboard.press("Enter")
        status = await wait_for(lambda: overlay_el(page, ".status"), lambda v: v and not v["hidden"] and ("已保存" in v["text"] or "已在" in v["text"] or "失败" in v["text"]), 30000)
        rec("D1 text+thought saved", bool(status and "已保存" in status["text"]), status and status["text"])
        d_mat = next((m for m in newest_materials(5) if m["id"] not in before and m["modality"] == "text" and (m["human"]["thought"] or "").startswith("D:")), None)
        rec("D2 thought + content in Core", bool(d_mat) and d_mat["original"]["content"].strip()[:30] == sel2[:30], d_mat and f"{d_mat['id']} {d_mat['human']['thought']!r}")
        if d_mat: CREATED.append(d_mat["id"])
        # second Enter must not re-submit (form locked)
        await page.keyboard.press("Enter"); await asyncio.sleep(1.0)
        rec("D3 second Enter does not duplicate", len([m for m in newest_materials(8) if m["id"] not in before and m["modality"] == "text"]) == 1, "")
        await page.keyboard.press("Escape"); await asyncio.sleep(0.4)

        # ---- E. shortcut fallback: the command handler path (captureInTab) with a selection, then pick mode
        sw = next((s for s in ctx.service_workers if "background" in s.url), None)
        rec("E0 extension service worker reachable", sw is not None, sw and sw.url)
        sel3 = await select_text(page, min_len=60)
        await page.mouse.move(5, 5)
        if sw:
            await sw.evaluate("async () => { const [tab] = await chrome.tabs.query({active: true, currentWindow: true}); await __magpie.captureInTab(tab, {}); }")
            inp = await wait_for(lambda: overlay_el(page, ".thought"), lambda v: v is not None, 6000)
            prev = await overlay_el(page, ".card")
            rec("E1 shortcut path with selection opens overlay for the text", bool(inp) and bool(prev and sel3[:20] in prev["text"]), prev and prev["text"][:100])
            await page.keyboard.press("Escape"); await asyncio.sleep(0.4)
            await page.evaluate("() => window.getSelection().removeAllRanges()")
            await sw.evaluate("async () => { const [tab] = await chrome.tabs.query({active: true, currentWindow: true}); await __magpie.captureInTab(tab, {}); }")
            await asyncio.sleep(0.6)
            hint = await overlay_el(page, ".banner")
            rec("E2 shortcut path without selection enters pick mode", bool(hint) and "点击要收集的图片" in hint["text"], hint and hint["text"][:100])
            # pick the first image by clicking it
            await page.mouse.move(img["x"], img["y"]); await asyncio.sleep(0.3); await page.mouse.click(img["x"], img["y"])
            inp = await wait_for(lambda: overlay_el(page, ".thought"), lambda v: v is not None, 6000)
            rec("E3 pick mode -> click image -> overlay", bool(inp), "")
            await page.keyboard.press("Escape"); await asyncio.sleep(0.4)

        # ---- popup
        if sw:
            ext_id = sw.url.split("/")[2]
            pop = await ctx.new_page()
            await pop.goto(f"chrome-extension://{ext_id}/popup.html", wait_until="domcontentloaded")
            await pop.wait_for_timeout(1500)
            txt = await pop.evaluate("() => document.body.innerText")
            rec("P1 popup shows Core connected", "已连接" in txt, txt.replace("\n", " | ")[:200])
            await pop.close()

        # ---- H. Core down
        subprocess.run(["sh", "-c", "kill $(lsof -tnP -iTCP:8765 -sTCP:LISTEN) 2>/dev/null; sleep 1"])
        st, _ = core("/health")
        rec("H0 Core stopped", st is None, str(_)[:60])
        await page.bring_to_front()
        acts = await open_arc(page, img2)
        await click_shadow(page, acts["save"])
        status = await wait_for(lambda: overlay_el(page, ".status"), lambda v: v and not v["hidden"] and v["text"], 15000)
        rec("H1 Core down -> explicit error with retry", bool(status) and ("Core" in status["text"] or "无法" in status["text"] or "连接" in status["text"]) and "重试" in status["text"], status and status["text"][:160])
        # restart Core (PR #3 worktree) and retry
        subprocess.Popen(["sh", "-c", os.environ.get("MAGPIE_RESTART_CMD", "cd \"$(git rev-parse --show-toplevel)\" && nohup .venv/bin/magpie serve --port 8765 > /tmp/magpie-serve.log 2>&1 &")])
        for _ in range(30):
            time.sleep(1)
            if core("/health")[0] == 200: break
        rec("H2 Core restarted", core("/health")[0] == 200, "")
        retry = await page.evaluate("""() => { const h=document.getElementById('magpie-capture-host'); if(!h) return null; const b=[...h.shadowRoot.querySelectorAll('button')].find(b=>b.textContent.trim()==='重试'); if(!b) return null; const r=b.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; }""")
        if retry:
            await click_shadow(page, retry)
            status = await wait_for(lambda: overlay_el(page, ".status"), lambda v: v and ("已保存" in v["text"] or "已在素材库" in v["text"]), 30000)
            rec("H3 retry after restart succeeds (duplicate of B expected)", bool(status and ("已在素材库" in status["text"] or "已保存" in status["text"])), status and status["text"][:120])
        await page.keyboard.press("Escape"); await asyncio.sleep(0.4)

        # ------------------------------------------------------------ J. WeChat article
        try:
            await page.goto(WECHAT, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            title = await page.title()
            body_len = await page.evaluate("() => (document.body.innerText||'').length")
            rec("J0 WeChat article loaded", "mp.weixin" in page.url and body_len > 500, f"title={title[:50]!r} chars={body_len}")
            before_w = {m["id"] for m in newest_materials(50)}
            sel = await select_text(page, min_len=30)
            pill = await wait_for(lambda: hover_el(page, ".pill"), lambda v: v and not v["hidden"], 5000)
            if pill and not pill["hidden"]:
                await click_shadow(page, await hover_el(page, '.pact[data-act="save"]'))
                toast = await wait_for(lambda: hover_el(page, ".toast"), lambda v: v and not v["hidden"] and ("已保存" in v["text"] or "已在素材库" in v["text"]), 20000)
                w_txt = next((m for m in newest_materials(5) if m["id"] not in before_w and m["modality"] == "text"), None)
                rec("J1 WeChat text saved", bool(w_txt) and bool(toast and "已保存" in toast["text"]), w_txt and f"{w_txt['id']} {w_txt['original']['content'][:50]!r} src={w_txt['source']['page_url'][:40]}")
                if w_txt: CREATED.append(w_txt["id"])
            else:
                rec("J1 WeChat text saved", False, f"pill missing; selection={sel[:40]!r}")
            x = await hover_el(page, ".toast .tx")
            if x: await click_shadow(page, x)
            wimg = await big_image(page, min_side=150)
            if wimg:
                acts = await open_arc(page, wimg)
                if acts and acts["save"]:
                    await click_shadow(page, acts["save"])
                    toast = await wait_for(lambda: hover_el(page, ".toast"), lambda v: v and not v["hidden"] and ("已保存" in v["text"] or "已在素材库" in v["text"]), 30000)
                    ov = await overlay_el(page, ".status")
                    w_img = next((m for m in newest_materials(5) if m["id"] not in before_w and m["modality"] == "image"), None)
                    rec("J2 WeChat image saved via background fetch", bool(w_img) and bool(toast and "已保存" in toast["text"]), f"toast={toast and toast['text']} overlay={ov and ov['text'][:80]} mat={w_img and (w_img['id'], w_img['original']['size'], w_img['source']['resource_url'][:60])}")
                    if w_img:
                        CREATED.append(w_img["id"])
                        m = wait_ready(w_img["id"])
                        rec("J3 WeChat image analysed (real bytes)", m["processing"]["status"] == "ready" and (m["objective_metadata"].get("width") or 0) > 100, f"{m['processing']['status']} {m['objective_metadata'].get('width')}x{m['objective_metadata'].get('height')} {m['objective_metadata'].get('format')}")
                else:
                    rec("J2 WeChat image saved via background fetch", False, "no arc on wechat image")
            else:
                rec("J2 WeChat image saved via background fetch", False, "no large image found on page")
        except Exception as e:
            rec("J0 WeChat article loaded", False, f"{type(e).__name__}: {str(e)[:120]}")

        await browser.close()

    print("\nCREATED:", CREATED)
    print(f"\n{sum(1 for _,ok,_ in RESULTS if ok)}/{len(RESULTS)} checks passed")
    json.dump({"results": RESULTS, "created": CREATED}, open(os.environ.get("MAGPIE_ACCEPT_OUT", "/tmp") + "/extension_acceptance_results.json", "w"), ensure_ascii=False, indent=1)

asyncio.run(main())
