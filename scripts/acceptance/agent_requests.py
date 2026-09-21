"""Retrieval agent acceptance: 5 real requests via /search (agent route) and /packs; prints a compact record."""
import json, os, time, urllib.parse, urllib.request
from collections import Counter

CORE = os.environ.get("MAGPIE_CORE", "http://127.0.0.1:8765")
def get(path):
    with urllib.request.urlopen(CORE + path, timeout=300) as r: return json.load(r)
def post(path, body):
    req = urllib.request.Request(CORE + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r: return json.load(r)
_mats = {}
def lab(mid):
    if mid not in _mats: _mats[mid] = get(f"/materials/{mid}")
    m = _mats[mid]; return (m["source"].get("page_title") or m["original"].get("filename") or (m["original"].get("content") or "")[:28]), m

REQUESTS = [
    ("R1 明确颜色", "找红色系的视觉参考", False),
    ("R2 抽象气质", "找克制、有物质感、不要典型 AI 科技感的参考", True),
    ("R3 文案/文字", "给一个手作陶器品牌的首页写文案，帮我找语气和态度上的参考", True),
    ("R4 模糊任务", "我要做一个网页，帮我找一些有用的参考", True),
    ("R5 带排除条件", "找有历史感的排版和字体参考，但不要西方的东西", True),
]
out = {}
for name, q, build in REQUESTS:
    print(f"\n==================== {name}: {q}")
    t = time.time()
    s = get("/search?q=" + urllib.parse.quote(q) + "&limit=12")
    dt = time.time() - t
    it = s["intent"]; print(f"intent: mode={it.get('mode')} reason={it.get('reason')!r} sort={s.get('sort')} route={s['route']}  ({dt:.0f}s)")
    if s.get("task"):
        b = s["task"]; print(f"brief: purpose={b.get('purpose')!r} desired={b.get('desired_qualities')} avoid={b.get('avoid')} facets={b.get('facets')} queries={b.get('search_queries')}")
    print(f"trace: {s.get('trace', {}).get('steps') and [(x['step'], x.get('s'), x.get('candidates') or x.get('kept') or x.get('judged')) for x in s['trace']['steps']]}")
    hits = s["hits"]; print(f"hits shown: {len(hits)} | dropped(0): {len(s.get('dropped') or [])} | relevance dist: {Counter(h.get('relevance') for h in hits)}")
    for d in (s.get("dropped") or [])[:6]:
        print(f"  [0] {lab(d['material_id'])[0][:40]:<40} dropped | {(d.get('why') or '')[:90]}")
    for h in hits[:12]:
        l, m = lab(h["material_id"])
        print(f"  [{h.get('relevance')}] {l[:40]:<40} {m['modality']:<5} aspect={h.get('aspect')!r} | {(h.get('why') or '')[:100]}")
    rec = {"search": {"intent": it, "brief": s.get("task"), "hits": [(h["material_id"], h.get("relevance"), h.get("aspect"), h.get("why")) for h in hits], "dropped": [(d["material_id"], d.get("why")) for d in (s.get("dropped") or [])], "seconds": round(dt)}}
    if build:
        t = time.time(); p = post("/packs", {"task": q})["pack"]; dt = time.time() - t
        print(f"--- pack {p['id']} in {dt:.0f}s | timing {p['generation']['timing_s']} | fallback {p['generation']['fallback']}")
        print(f"direction: {p.get('human_direction')}")
        for g in p["groups"]:
            print(f"  ## {g['name']} — {g.get('purpose')}")
            for m in g["members"]:
                l, mm = lab(m["material_id"]); print(f"     [{m.get('relevance')}] {l[:36]:<36} {mm['modality']:<5} role={m.get('role')} | {(m.get('reason') or '')[:96]}")
        print(f"  excluded: {len(p['excluded'])} | judged dist: {Counter(c.get('relevance') for c in p['candidates'])}")
        for e in p["excluded"][:4]: print(f"     - {lab(e['material_id'])[0][:30]}: {e['reason'][:80]}")
        print(f"  gaps: {p.get('gaps')}")
        rec["pack"] = p["id"]
    out[name] = rec
dest = os.environ.get("MAGPIE_ACCEPT_OUT", "/tmp") + "/agent_requests.json"
json.dump(out, open(dest, "w"), ensure_ascii=False, indent=1)
print("\nsaved", dest)
