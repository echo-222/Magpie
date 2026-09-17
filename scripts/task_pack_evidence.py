"""Run real Task -> Material Pack validations and save the evidence (spec §13 item 7).

Usage:
  python scripts/task_pack_evidence.py                      # the three default tasks
  python scripts/task_pack_evidence.py "任务描述" ...          # your own tasks
  python scripts/task_pack_evidence.py --pack pack_xxx      # export an existing (human-edited) pack

Writes docs/phase1_evidence/<slug>.md (Copy-for-Agent Markdown), .json (JSON twin) and
appends a compact summary to docs/phase1_evidence/SUMMARY.md.  Requires real models
(MAGPIE_LLM_PROVIDER must not be "fake").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from magpie.config import get_settings  # noqa: E402
from magpie.db import Database  # noqa: E402
from magpie.export import pack_to_json, pack_to_markdown  # noqa: E402
from magpie.llm import get_llm  # noqa: E402
from magpie.recompose import Recomposer  # noqa: E402
from magpie.retrieval import Retriever  # noqa: E402

OUT = ROOT / "docs" / "phase1_evidence"

DEFAULT_TASKS = [
    "我要做一个克制、有物质感、不要典型 AI 蓝紫色的网站首页。",
    "给一个手工陶器小品牌做 Instagram 的视觉方向和文案语气：安静、有手感、接受不完美，不要网红滤镜和营销腔。",
    "为一本讲中文字体历史的小册子做封面和内页排版，要有纸本书的物质感，参考老版本的版面知识。",
]


def slug(s: str, n: int) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s)[:24].strip("_")
    return f"task{n}_{s}"


def summarize(pack, db) -> str:
    lines = [f"### {pack.name}", "", f"- pack: `{pack.id}` · candidates {len(pack.candidates)} · members {len(pack.member_ids())} · human edits {len(pack.human_edits)} · fallback {pack.generation.get('fallback')}",
             f"- timing (s): {pack.generation.get('timing_s')}", f"- models: {pack.generation.get('chat_model')} / {pack.generation.get('embed_model')}",
             f"- search queries: {pack.task.search_queries}", ""]
    via = {c.material_id: c.via for c in pack.candidates}
    for g in pack.groups:
        if not g.members:
            continue
        lines.append(f"**{g.name}** — {g.purpose or ''}")
        for m in g.members:
            mat = db.get_material(m.material_id)
            label = mat.source.page_title or mat.original.filename or mat.id
            lines.append(f"- {label} · role: {m.role or '-'} · via {via.get(m.material_id, 'human')}" + (" · added by human" if m.added_by == "human" else ""))
            lines.append(f"  - reason: {m.reason}")
        lines.append("")
    if pack.excluded:
        lines.append("Excluded by Magpie: " + "; ".join(f"{(db.get_material(e['material_id']).source.page_title or e['material_id'])} ({e['reason']})" for e in pack.excluded))
    if pack.removed_material_ids:
        lines.append("Removed by human: " + ", ".join(pack.removed_material_ids))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*")
    ap.add_argument("--pack", action="append", default=[], help="export an existing pack id instead of building")
    ap.add_argument("--start", type=int, default=1, help="numbering offset for output files")
    args = ap.parse_args()

    s = get_settings()
    if s.llm_provider.lower() == "fake":
        sys.exit("evidence runs must use real models (MAGPIE_LLM_PROVIDER != fake)")
    db = Database(s.db_path)
    llm = get_llm(s)
    rc = Recomposer(db=db, retriever=Retriever(db=db, llm=llm), llm=llm)
    OUT.mkdir(parents=True, exist_ok=True)
    summary = OUT / "SUMMARY.md"
    if not summary.exists():
        summary.write_text("# Phase 1 evidence: real Task -> Material Pack runs\n\nLibrary: %d materials. Models: %s / %s / %s.\n\n" % (db.count_materials(), s.chat_model, s.vision_model, s.embed_model), encoding="utf-8")

    packs = []
    for pid in args.pack:
        p = db.get_pack(pid)
        if not p:
            sys.exit(f"pack not found: {pid}")
        packs.append(p)
    for t in args.tasks or ([] if args.pack else DEFAULT_TASKS):
        t0 = time.time()
        print(f"building pack for: {t}")
        p = rc.build(t)
        print(f"  -> {p.id} in {time.time() - t0:.0f}s, {len(p.member_ids())} members in {len(p.groups)} groups (fallback={p.generation.get('fallback')})")
        packs.append(p)

    for i, p in enumerate(packs, args.start):
        base = OUT / slug(p.task.raw_request, i)
        base.with_suffix(".md").write_text(pack_to_markdown(p, db), encoding="utf-8")
        base.with_suffix(".json").write_text(json.dumps(pack_to_json(p, db), ensure_ascii=False, indent=2), encoding="utf-8")
        with summary.open("a", encoding="utf-8") as f:
            f.write(f"## Task {i}: {p.task.raw_request}\n\n{summarize(p, db)}\nFiles: `{base.name}.md`, `{base.name}.json`\n\n")
        print(f"  wrote {base.name}.md/.json")


if __name__ == "__main__":
    main()
