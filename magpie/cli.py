"""Command line for the dev loop: serve / import / list / search / pack / export / doctor."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import get_settings
from .db import Database
from .ingest import Ingestor
from .llm import get_llm
from .models import CapturePayload, Human, Source
from .retrieval import Retriever


def _open() -> tuple[Database, Ingestor, Retriever]:
    s = get_settings()
    s.ensure_dirs()
    db = Database(s.db_path)
    llm = get_llm(s)
    return db, Ingestor(db=db, settings=s, llm=llm), Retriever(db=db, llm=llm)


# ------------------------------------------------------------------------ commands


def cmd_serve(args):
    import uvicorn

    uvicorn.run("magpie.api:app", host=args.host, port=args.port, reload=args.reload)


def cmd_import(args):
    """Import a manifest of local materials (images/texts + Human Thought + source)."""
    manifest_path = Path(args.manifest)
    base = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest["items"] if isinstance(manifest, dict) else manifest
    db, ing, _ = _open()
    t0 = time.time()
    n_new = n_dup = n_fail = 0
    for i, item in enumerate(items, 1):
        payload = CapturePayload(
            modality=item["modality"],
            content=item.get("content"),
            source=Source(**(item.get("source") or {})),
            human=Human(thought=item.get("thought")),
        )
        label = item.get("key") or item.get("file") or f"item{i}"
        t1 = time.time()
        try:
            if payload.modality == "image":
                p = base / item["file"]
                m, created = ing.create_image(p.read_bytes(), p.name, payload)
            else:
                if not payload.content and item.get("file"):
                    payload.content = (base / item["file"]).read_text(encoding="utf-8")
                m, created = ing.create_text(payload)
            if created or m.processing.status != "ready" or args.reanalyze:
                m = ing.analyze(m.id)
            status = m.processing.status
            if not created and not args.reanalyze:
                n_dup += 1
                tag = "skip (exists)"
            elif status == "ready":
                n_new += 1
                tag = "ok"
            else:
                n_fail += 1
                tag = f"FAILED: {m.processing.error}"
            print(f"[{i:>2}/{len(items)}] {label:<28} {m.id}  {tag}  ({time.time() - t1:.1f}s)")
            if args.verbose and status == "ready":
                print(f"      human: {m.human.thought}")
                print(f"      ai   : {m.analysis.summary}")
                print(f"      tags : {m.ai_line()}")
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            print(f"[{i:>2}/{len(items)}] {label:<28} ERROR {e}")
    print(f"\nimported={n_new} skipped={n_dup} failed={n_fail} total_in_db={db.count_materials()} ({time.time() - t0:.0f}s)")


def cmd_list(args):
    db, _, _ = _open()
    for m in db.list_materials(limit=args.limit):
        print(f"{m.id}  {m.modality:<5} {m.processing.status:<8} {m.short_label()[:36]:<36} | H: {(m.human.thought or '')[:40]:<40} | AI: {m.ai_line()[:60]}")
    print(f"total: {db.count_materials()}")


def cmd_search(args):
    _, _, ret = _open()
    for h in ret.search(args.query, limit=args.limit):
        m = h.material
        print(f"{h.score:.3f} [{h.via:<13}] {m.id} {m.modality:<5} {m.short_label()[:30]:<30} | H: {(m.human.thought or '')[:36]:<36} | AI: {m.ai_line()[:50]}")


def cmd_doctor(args):
    from .config import mask_key

    s = get_settings()
    print(f"provider   : {s.llm_provider}  local endpoint={s.llm_base_url}")
    try:
        chat = s.chat_endpoint()
        fb = s.chat_fallback_endpoint()
        print(f"chat       : {chat.name} {chat.model} @ {chat.host}  key={mask_key(chat.api_key) if chat.name != 'local' else '-'}"
              + (f"  (thinking={s.deepseek_thinking})" if chat.name == "deepseek" and s.deepseek_thinking else "")
              + (f"  fallback -> local {fb.model}" if fb else ""))
    except RuntimeError as e:
        print(f"chat       : CONFIG ERROR {e}")
    print(f"vision     : {s.vision_endpoint().model} @ {s.vision_endpoint().host}\nembed      : {s.embed_endpoint().model} @ {s.embed_endpoint().host}")
    print(f"data dir   : {s.data_dir.resolve()}")
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401

        print("ocr        : rapidocr available")
    except Exception:  # noqa: BLE001
        print("ocr        : rapidocr NOT installed -> vision-model visible_text fallback (pip install -e '.[ocr]')")
    if s.llm_provider != "fake":
        llm = get_llm(s)
        t = time.time()
        try:
            v = llm.embed(["ping"])[0]
            print(f"embed call : ok dim={len(v)} ({time.time() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"embed call : FAILED {e}")
        t = time.time()
        try:
            out = llm.chat_json("Reply with JSON.", 'Return {"ok": true}', purpose="doctor")
            print(f"chat call  : ok {out} via {getattr(llm, 'last_chat_model', '?')} ({time.time() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"chat call  : FAILED {e}")
        if s.chat_provider == "local":
            _ollama_context_check(s)
    db = Database(s.db_path)
    print(f"materials  : {db.count_materials()}  embeddings: {db.embedding_stats()}")


def _ollama_context_check(s) -> None:
    """Ollama only: warn when the loaded chat model runs with a context too small for recomposition."""
    if "11434" not in s.llm_base_url:
        return
    try:
        import json as _json
        import urllib.request

        base = s.llm_base_url.split("/v1")[0]
        with urllib.request.urlopen(base + "/api/ps", timeout=5) as r:
            models = _json.load(r).get("models", [])
    except Exception:  # noqa: BLE001
        return
    for m in models:
        if m.get("name", "").split(":")[0] in s.chat_model.split(":")[0] or m.get("name") == s.chat_model:
            ctx = m.get("context_length") or 0
            flag = "ok" if ctx >= 8192 else "TOO SMALL -> run scripts/setup_ollama_models.sh and set MAGPIE_CHAT_MODEL=qwen3:8b-16k"
            print(f"context    : {m.get('name')} num_ctx={ctx} {flag}")


def cmd_pack(args):
    from .recompose import Recomposer
    from .export import pack_to_markdown

    db, _, ret = _open()
    rc = Recomposer(db=db, retriever=ret, llm=ret.llm)
    t = time.time()
    pack = rc.build(args.task, limit=args.candidates)
    print(f"pack {pack.id} built in {time.time() - t:.1f}s  (candidates={len(pack.candidates)})\n")
    print(pack_to_markdown(pack, db))


def cmd_export(args):
    from .export import pack_to_json, pack_to_markdown

    db, _, _ = _open()
    pack = db.get_pack(args.pack_id)
    if not pack:
        sys.exit(f"pack not found: {args.pack_id}")
    out = pack_to_markdown(pack, db) if args.format == "markdown" else json.dumps(pack_to_json(pack, db), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(out)


def cmd_packs(args):
    db, _, _ = _open()
    for p in db.list_packs():
        print(f"{p.id}  {p.updated_at}  groups={len(p.groups)} members={len(p.member_ids())} edits={len(p.human_edits)}  {p.name[:60]}")


# ------------------------------------------------------------------------ parser


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="magpie", description="Magpie MVP core")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run API + dev UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("import", help="import a manifest.json of local materials")
    p.add_argument("manifest")
    p.add_argument("--reanalyze", action="store_true", help="re-run analysis for existing materials")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("list", help="list materials")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("search", help="hybrid search over the library")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("pack", help="Task -> Material Pack")
    p.add_argument("task")
    p.add_argument("--candidates", type=int, default=12)
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("packs", help="list packs")
    p.set_defaults(fn=cmd_packs)

    p = sub.add_parser("export", help="Copy for Agent: export a pack")
    p.add_argument("pack_id")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("doctor", help="check models / OCR / db")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
