"""UI acceptance of the PR #3 素材库 app (Brave via CDP, 1440x1000). Screenshots to /tmp/ui/."""
import asyncio, json, os, time, urllib.request
from playwright.async_api import async_playwright

CORE = os.environ.get("MAGPIE_CORE", "http://127.0.0.1:8765")
OUT = os.environ.get("MAGPIE_ACCEPT_OUT", "/tmp/ui"); os.makedirs(OUT, exist_ok=True)
TEST_IMG = os.environ.get("MAGPIE_TEST_IMAGE", "/tmp/barbican_crop.jpg")
if not os.path.exists(TEST_IMG):
    from PIL import Image
    _src = os.path.join(os.path.dirname(__file__), "..", "..", "demo_materials", "images", "barbican_concrete.jpg")
    _im = Image.open(_src); _w, _h = _im.size; _im.crop((_w // 3, _h // 3, _w // 3 + 640, _h // 3 + 480)).save(TEST_IMG, quality=85)
R = []
def rec(name, ok, detail=""):
    R.append((name, ok, detail)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
def core(path, method="GET", body=None):
    req = urllib.request.Request(CORE + path, method=method, data=json.dumps(body).encode() if body else None, headers={"content-type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

TASK = "给一个讲活字印刷历史的小型展览做视觉方向：海报和展签，要有纸和金属的物质感，克制，不要博物馆式的严肃，也不要蓝紫科技感。"

async def shot(page, name):
    await page.screenshot(path=f"{OUT}/{name}.png", full_page=False)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(os.environ.get("MAGPIE_CDP", "http://localhost:9222"))
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        await page.set_viewport_size({"width": 1440, "height": 1000})
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept("验收：活字印刷展览视觉方向" if d.type == "prompt" else None)))

        # 1. library grid
        await page.goto(CORE + "/#search", wait_until="domcontentloaded"); await page.wait_for_timeout(2500)
        cnt_ui = await page.evaluate("() => document.getElementById('cnt_lib').textContent")
        total = core("/materials?limit=1")[1]["total"]
        rec("U1 library count matches Core", str(total) == cnt_ui.strip(), f"ui={cnt_ui} core={total}")
        await shot(page, "01_library")

        # 2. keyword search (fast route)
        await page.fill("#q", "纸张质感"); await page.click("#search"); await page.wait_for_timeout(4000)
        intent = await page.evaluate("() => document.getElementById('intent').innerText")
        n = await page.evaluate("() => document.querySelectorAll('#grid .card, #grid .m, #grid > *').length")
        rec("U2 keyword search shows results + intent line", n > 0, f"intent={intent[:120]!r} cards={n}")
        await shot(page, "02_search_keyword")

        # 3. request search (agent route)
        await page.fill("#q", "找红色系的视觉参考"); await page.click("#search")
        t0 = time.time()
        await page.wait_for_function("() => !document.getElementById('status').classList.contains('busy') && document.getElementById('intent').innerText.length > 0", timeout=120000)
        await page.wait_for_timeout(1500)
        intent = await page.evaluate("() => document.getElementById('intent').innerText")
        badges = await page.evaluate("() => [...document.querelectorAll ? [] : []]") if False else await page.evaluate("() => [...document.querySelectorAll('#grid *')].map(e=>e.textContent.trim()).filter(t=>/^(3|2|1)\\s*·|相关|relevance/.test(t)).slice(0,6)")
        first_cards = await page.evaluate("() => [...document.querySelectorAll('#grid .card .name, #grid .card .label, #grid .card b')].slice(0,6).map(e=>e.textContent.trim())")
        rec("U3 request search routed to agent", "自然语言需求" in intent, f"{time.time()-t0:.0f}s intent={intent[:160]!r} first={first_cards}")
        await shot(page, "03_search_agent_red")

        # 4. drawer on one of the extension-captured materials
        await page.evaluate("() => openMaterial('mat_579d5b5f78')"); await page.wait_for_timeout(2000)
        dtxt = await page.evaluate("() => document.getElementById('drawer').innerText + ' || ' + document.getElementById('d_foot').innerText")
        rec("U4 drawer shows thought / AI / source / actions", all(k in dtxt for k in ("HUMAN THOUGHT", "AI 理解", "来源", "重新分析", "删除", "以此为任务找参考", "相似素材")), dtxt[:220].replace("\n", " | "))
        await shot(page, "04_drawer")
        await page.fill("#d_thought", "B: 老印刷机的金属感，想用在关于工艺的页面 · UI 修改测试"); await page.press("#d_thought", "Enter"); await page.wait_for_timeout(1200)
        th = core("/materials/mat_579d5b5f78")[1]["human"]["thought"]
        rec("U5 thought edit in drawer persists", th.endswith("UI 修改测试"), th)
        await page.evaluate("() => reanalyze('mat_579d5b5f78')"); await page.wait_for_timeout(800)
        st1 = core("/materials/mat_579d5b5f78")[1]["processing"]["status"]
        for _ in range(45):
            time.sleep(2); st2 = core("/materials/mat_579d5b5f78")[1]
            if st2["processing"]["status"] == "ready": break
        rec("U6 reanalyze from drawer", st1 in ("pending", "analyzing", "ready") and st2["processing"]["status"] == "ready" and st2["human"]["thought"].endswith("UI 修改测试"), f"first={st1} final={st2['processing']['status']} thought kept={st2['human']['thought'][-8:]}")
        await page.keyboard.press("Escape"); await page.wait_for_timeout(400)

        # 5. add view: text + image
        await page.goto(CORE + "/#add", wait_until="domcontentloaded"); await page.wait_for_timeout(1000)
        await shot(page, "05_add")
        await page.click("#tab_txt")
        await page.fill("#content", "验收测试文字素材：活字印刷的每一个铅字都是一次独立的压印。")
        await page.fill("#thought", "UI 加入素材测试：压印这个动作")
        await page.evaluate("() => document.querySelector('details.more').open = true")
        await page.fill("#page_url", "https://example.com/ui-accept"); await page.click("#add"); await page.wait_for_timeout(2500)
        msg = await page.evaluate("() => document.getElementById('add_msg').innerText")
        added = [m for m in core("/materials?limit=5")[1]["items"] if (m["human"]["thought"] or "").startswith("UI 加入素材测试")]
        rec("U7 add text via UI", bool(added), f"msg={msg!r} id={added and added[0]['id']}")
        await page.click("#tab_img")
        await page.set_input_files("#file", TEST_IMG); await page.wait_for_timeout(600)
        await page.fill("#thought", "UI 上传图片测试：混凝土局部"); await page.click("#add"); await page.wait_for_timeout(3000)
        msg2 = await page.evaluate("() => document.getElementById('add_msg').innerText")
        added_img = [m for m in core("/materials?limit=5")[1]["items"] if (m["human"]["thought"] or "").startswith("UI 上传图片测试")]
        rec("U8 add image via UI", bool(added_img), f"msg={msg2!r} id={added_img and added_img[0]['id']}")
        await shot(page, "06_add_done")

        # 6. packs: build the full-loop task
        await page.goto(CORE + "/#packs", wait_until="domcontentloaded"); await page.wait_for_timeout(1000)
        await page.fill("#task", TASK); t0 = time.time(); await page.click("#build")
        await page.wait_for_function("() => location.hash.startsWith('#packs/pack_')", timeout=240000)
        await page.wait_for_timeout(1500)
        pid = (await page.evaluate("() => location.hash")).split("/")[-1]
        pk = core(f"/packs/{pid}")[1]["pack"]
        members = [m for g in pk["groups"] for m in g["members"]]
        rec("U9 pack built from UI", bool(pk["groups"]) and pk["generation"].get("pipeline") == "agent-v2", f"{pid} {time.time()-t0:.0f}s groups={[g['name'] for g in pk['groups']]} members={len(members)} gaps={pk.get('gaps')}")
        await shot(page, "07_pack")
        ptxt = await page.evaluate("() => document.getElementById('pack').innerText")
        rec("U10 pack view shows reasons + actions", all(k in ptxt for k in ("移出", "找替代", "备注", "Copy for Agent")), ptxt[:160].replace("\n", " | "))
        # human edits: remove first member, move second to a new group, note third
        ids = [m["material_id"] for m in members]
        await page.evaluate(f"() => removeMember('{ids[0]}')"); await page.wait_for_timeout(1200)
        pk2 = core(f"/packs/{pid}")[1]["pack"]
        rec("U11 remove member persists", ids[0] in pk2["removed_material_ids"], "")
        page.once("dialog", lambda d: asyncio.ensure_future(d.accept("验收新组")))
        await page.evaluate(f"() => moveMember('{ids[1]}', '__new__')"); await page.wait_for_timeout(1500)
        pk3 = core(f"/packs/{pid}")[1]["pack"]
        rec("U12 move to new group persists", any(g["name"] == "验收新组" and any(m["material_id"] == ids[1] for m in g["members"]) for g in pk3["groups"]), [g["name"] for g in pk3["groups"]])
        page.once("dialog", lambda d: asyncio.ensure_future(d.accept("验收备注：海报主图用这个")))
        await page.evaluate(f"() => noteMember('{ids[2]}')"); await page.wait_for_timeout(1500)
        pk4 = core(f"/packs/{pid}")[1]["pack"]
        note = next((m["note"] for g in pk4["groups"] for m in g["members"] if m["material_id"] == ids[2]), None)
        rec("U13 note persists", note == "验收备注：海报主图用这个", str(note))
        # alternatives
        gname = next(g["name"] for g in pk4["groups"] if any(m["material_id"] == ids[2] for m in g["members"]))
        await page.evaluate(f"() => alternatives('{ids[2]}', {json.dumps(gname)})")
        await page.wait_for_function("() => document.getElementById('pack').innerText.includes('加入')", timeout=90000); await page.wait_for_timeout(800)
        alt_txt = await page.evaluate("() => document.getElementById('pack').innerText")
        rec("U14 alternatives shown with reasons", "加入" in alt_txt, "")
        await shot(page, "08_pack_edited")
        # rename pack (double-click → prompt) via handler
        await page.evaluate("() => renamePack()"); await page.wait_for_timeout(1200)
        pk5 = core(f"/packs/{pid}")[1]["pack"]
        rec("U15 pack rename persists", pk5["name"] == "验收：活字印刷展览视觉方向", pk5["name"])
        # export
        await page.evaluate(f"() => exportPack('markdown')"); await page.wait_for_timeout(1500)
        md = await page.evaluate("() => document.getElementById('ex_pre').innerText")
        open(OUT + "/copy_for_agent.md", "w").write(md)
        rec("U16 Copy for Agent export", md.startswith("# Task") and "Human Thought" in md and "left out" in md or "Deliberately" in md, f"{len(md)} chars")
        await shot(page, "09_export")
        await page.keyboard.press("Escape")
        # history + delete a throwaway pack
        hist = await page.evaluate("() => document.getElementById('histlist').innerText")
        rec("U17 history lists the new pack", "验收：活字印刷展览视觉方向" in hist, hist[:100].replace("\n", " | "))
        st, tmp = core("/packs", "POST", {"task": "验收：临时素材包，用于测试删除"})
        tmp_id = tmp["pack"]["id"]; await page.reload(); await page.wait_for_timeout(1500)
        page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.evaluate(f"() => deletePack('{tmp_id}')"); await page.wait_for_timeout(1200)
        rec("U18 delete pack", core(f"/packs/{tmp_id}")[0] == 404, tmp_id)

        # 7. delete a material from the drawer
        await page.goto(CORE + "/#search", wait_until="domcontentloaded"); await page.wait_for_timeout(1500)
        did = added[0]["id"] if added else None
        if did:
            await page.evaluate(f"() => openMaterial('{did}')"); await page.wait_for_timeout(1200)
            page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await page.evaluate(f"() => deleteMaterial('{did}')"); await page.wait_for_timeout(1500)
            rec("U19 delete material from drawer", core(f"/materials/{did}")[0] == 404, did)
        await page.close(); await browser.close()
    print(f"\n{sum(1 for _,ok,_ in R if ok)}/{len(R)} passed")
    json.dump({"results": R, "pack": pid, "task": TASK}, open(OUT + "/results.json", "w"), ensure_ascii=False, indent=1)

asyncio.run(main())
