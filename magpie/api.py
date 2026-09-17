"""Thin FastAPI layer + one static dev page.

The HTTP contract is what the capture/front-end teammate will talk to later
(POST /materials with a CapturePayload + file).  Nothing here knows about analysis
details; that lives in ingest.py / recompose.py.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ValidationError

from . import __version__
from . import pack as pack_ops
from .config import get_settings
from .db import Database
from .export import pack_to_json, pack_to_markdown
from .ingest import Ingestor
from .llm import get_llm
from .models import CapturePayload, Human, MaterialPack, Source
from .recompose import Recomposer
from .retrieval import Retriever

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class AppState:
    def __init__(self):
        self.settings = get_settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.llm = get_llm(self.settings)
        self.ingestor = Ingestor(db=self.db, settings=self.settings, llm=self.llm)
        self.retriever = Retriever(db=self.db, llm=self.llm)
        self.recomposer = Recomposer(db=self.db, retriever=self.retriever, llm=self.llm)
        self.analysis_lock = threading.Lock()  # one local model call at a time

    def analyze_in_background(self, material_id: str) -> None:
        with self.analysis_lock:
            try:
                self.ingestor.analyze(material_id)
            except Exception:  # noqa: BLE001
                log.exception("background analysis failed for %s", material_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.magpie = AppState()
    yield
    app.state.magpie.db.close()


app = FastAPI(title="Magpie MVP", version=__version__, lifespan=lifespan)

# Dev-stage CORS so a browser extension / local web page can call the Core directly.
# MAGPIE_CORS_ORIGINS: comma-separated origins, default "*" (no credentials are used anywhere).
_cors = [o.strip() for o in (get_settings().cors_origins or "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_methods=["*"], allow_headers=["*"], allow_credentials=False)


def state(request: Request) -> AppState:
    return request.app.state.magpie


# ------------------------------------------------------------------------ dev page


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health(request: Request):
    st = state(request)
    return {
        "version": __version__,
        "provider": st.llm.provider,
        "chat_provider": getattr(st.llm, "chat_provider", st.llm.provider),
        "chat_model": st.llm.chat_model,
        "vision_model": st.llm.vision_model,
        "embed_model": st.llm.embed_model,
        "materials": st.db.count_materials(),
        "embeddings": st.db.embedding_stats(),
        "data_dir": str(st.settings.data_dir),
    }


# ------------------------------------------------------------------------ materials


@app.post("/materials", status_code=201)
async def create_material(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    modality: str | None = Form(default=None),
    content: str | None = Form(default=None),
    thought: str | None = Form(default=None),
    page_url: str | None = Form(default=None),
    resource_url: str | None = Form(default=None),
    page_title: str | None = Form(default=None),
    captured_at: str | None = Form(default=None),
    sync: bool = Form(default=False),
):
    """Material input contract (spec §10).

    * multipart/form-data: `file` (image) or `content` (text) + `thought` + source fields
    * application/json:    CapturePayload body (text materials)
    Analysis runs in the background unless `sync=true`.
    """
    st = state(request)
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except ValueError as e:
                raise HTTPException(422, [{"loc": ["body"], "msg": f"invalid JSON: {e}", "type": "json_invalid"}]) from e
            if not isinstance(body, dict):
                raise HTTPException(422, [{"loc": ["body"], "msg": "body must be a JSON object (CapturePayload)", "type": "type_error"}])
            payload = CapturePayload.model_validate(body)
            sync = bool(body.get("sync", False))
            data, filename = None, None
        else:
            data = await file.read() if file is not None else None
            filename = file.filename if file is not None else None
            mod = modality or ("image" if data else "text")
            payload = CapturePayload(
                modality=mod,  # type: ignore[arg-type]
                content=content,
                source=Source(page_url=page_url or None, resource_url=resource_url or None, page_title=page_title or None, captured_at=captured_at or None),
                human=Human(thought=(thought or "").strip() or None),
            )
    except ValidationError as e:
        # same shape FastAPI uses for request validation errors
        raise HTTPException(422, [{"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in e.errors()]) from e

    try:
        if payload.modality == "image":
            if not data:
                raise HTTPException(400, "image material needs a file")
            m, created = st.ingestor.create_image(data, filename, payload)
        else:
            m, created = st.ingestor.create_text(payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if created or m.processing.status != "ready":
        if sync:
            with st.analysis_lock:
                m = st.ingestor.analyze(m.id)
        else:
            background.add_task(st.analyze_in_background, m.id)
    return JSONResponse(status_code=201 if created else 200, content={"created": created, "material": m.model_dump()})


@app.get("/materials")
def list_materials(request: Request, limit: int = 500, modality: str | None = None):
    st = state(request)
    return {"items": [m.model_dump() for m in st.db.list_materials(limit=limit, modality=modality)], "total": st.db.count_materials()}


@app.get("/materials/{material_id}")
def get_material(request: Request, material_id: str):
    m = state(request).db.get_material(material_id)
    if not m:
        raise HTTPException(404, "material not found")
    return m.model_dump()


@app.get("/materials/{material_id}/file")
def get_material_file(request: Request, material_id: str):
    st = state(request)
    m = st.db.get_material(material_id)
    if not m or not m.original.file_path:
        raise HTTPException(404, "no file")
    return FileResponse(st.settings.files_dir / m.original.file_path, media_type=m.original.mime_type or "application/octet-stream")


@app.get("/materials/{material_id}/thumb")
def get_material_thumb(request: Request, material_id: str):
    st = state(request)
    p = st.settings.thumbs_dir / f"{material_id}.jpg"
    if not p.exists():
        m = st.db.get_material(material_id)
        if m and m.original.file_path:
            return FileResponse(st.settings.files_dir / m.original.file_path)
        raise HTTPException(404, "no thumbnail")
    return FileResponse(p, media_type="image/jpeg")


class ThoughtBody(BaseModel):
    thought: str | None = None


@app.patch("/materials/{material_id}/thought")
def update_thought(request: Request, material_id: str, body: ThoughtBody):
    """Human Thought edits only touch the human column and the human embedding."""
    st = state(request)
    m = st.db.get_material(material_id)
    if not m:
        raise HTTPException(404, "material not found")
    st.db.update_human(material_id, Human(thought=(body.thought or "").strip() or None))
    if m.processing.status == "ready":
        st.ingestor.reembed_human(material_id)
    return st.db.get_material(material_id).model_dump()


@app.post("/materials/{material_id}/reanalyze")
def reanalyze(request: Request, material_id: str, background: BackgroundTasks):
    st = state(request)
    if not st.db.get_material(material_id):
        raise HTTPException(404, "material not found")
    background.add_task(st.analyze_in_background, material_id)
    return {"queued": material_id}


@app.delete("/materials/{material_id}")
def delete_material(request: Request, material_id: str):
    st = state(request)
    m = st.db.get_material(material_id)
    if not m:
        raise HTTPException(404, "material not found")
    st.db.delete_material(material_id)
    return {"deleted": material_id}


# ------------------------------------------------------------------------ retrieval (Gate B)


@app.get("/search")
def search(request: Request, q: str, limit: int = 12):
    st = state(request)
    hits = st.retriever.search(q, limit=limit)
    return {
        "query": q,
        "hits": [
            {"material_id": h.material_id, "score": round(h.score, 4), "via": h.via, "signals": h.signals, "material": h.material.model_dump()}
            for h in hits
        ],
    }


@app.get("/materials/{material_id}/similar")
def similar(request: Request, material_id: str, limit: int = 8):
    st = state(request)
    hits = st.retriever.similar_to(material_id, limit=limit)
    return {"hits": [{"material_id": h.material_id, "score": round(h.score, 4), "via": h.via, "material": h.material.model_dump()} for h in hits]}


# ------------------------------------------------------------------------ packs (Gate C)


class BuildPackBody(BaseModel):
    task: str
    candidates: int = 12


class MemberBody(BaseModel):
    material_id: str


class MoveBody(MemberBody):
    to_group: str
    position: int | None = None


class AddBody(MemberBody):
    group: str
    reason: str | None = None
    role: str | None = None


class NoteBody(MemberBody):
    note: str | None = None


class AlternativesBody(BaseModel):
    material_id: str | None = None
    group: str | None = None
    limit: int = 4


class RenameGroupBody(BaseModel):
    old: str
    new: str


def _pack_response(st: AppState, pack: MaterialPack, extra_ids: list[str] | None = None) -> dict:
    ids = pack.member_ids() + pack.removed_material_ids + list(extra_ids or [])
    mats = st.db.get_materials(ids)
    return {"pack": pack.model_dump(), "materials": {k: v.model_dump() for k, v in mats.items()}}


def _load_pack(st: AppState, pack_id: str) -> MaterialPack:
    pack = st.db.get_pack(pack_id)
    if not pack:
        raise HTTPException(404, "pack not found")
    return pack


@app.post("/packs", status_code=201)
def build_pack(request: Request, body: BuildPackBody):
    st = state(request)
    if not body.task.strip():
        raise HTTPException(400, "task text required")
    with st.analysis_lock:
        pack = st.recomposer.build(body.task, limit=body.candidates)
    return _pack_response(st, pack)


@app.get("/packs")
def list_packs(request: Request):
    st = state(request)
    return {
        "items": [
            {"id": p.id, "name": p.name, "updated_at": p.updated_at, "groups": len(p.groups), "members": len(p.member_ids()), "edits": len(p.human_edits)}
            for p in st.db.list_packs()
        ]
    }


@app.get("/packs/{pack_id}")
def get_pack(request: Request, pack_id: str):
    st = state(request)
    return _pack_response(st, _load_pack(st, pack_id))


@app.delete("/packs/{pack_id}")
def delete_pack(request: Request, pack_id: str):
    st = state(request)
    _load_pack(st, pack_id)
    st.db.delete_pack(pack_id)
    return {"deleted": pack_id}


def _edit(st: AppState, pack_id: str, fn, *args):
    pack = _load_pack(st, pack_id)
    try:
        pack = fn(st.db, pack, *args)
    except pack_ops.PackError as e:
        raise HTTPException(400, str(e)) from e
    return _pack_response(st, pack)


@app.post("/packs/{pack_id}/remove")
def pack_remove(request: Request, pack_id: str, body: MemberBody):
    return _edit(state(request), pack_id, pack_ops.remove_member, body.material_id)


@app.post("/packs/{pack_id}/move")
def pack_move(request: Request, pack_id: str, body: MoveBody):
    return _edit(state(request), pack_id, pack_ops.move_member, body.material_id, body.to_group, body.position)


@app.post("/packs/{pack_id}/add")
def pack_add(request: Request, pack_id: str, body: AddBody):
    return _edit(state(request), pack_id, pack_ops.add_member, body.material_id, body.group, body.reason, body.role)


@app.post("/packs/{pack_id}/note")
def pack_note(request: Request, pack_id: str, body: NoteBody):
    return _edit(state(request), pack_id, pack_ops.set_note, body.material_id, body.note)


@app.post("/packs/{pack_id}/rename-group")
def pack_rename_group(request: Request, pack_id: str, body: RenameGroupBody):
    return _edit(state(request), pack_id, pack_ops.rename_group, body.old, body.new)


@app.post("/packs/{pack_id}/alternatives")
def pack_alternatives(request: Request, pack_id: str, body: AlternativesBody):
    """Find more like this — task-aware, excludes current members and human-removed items."""
    st = state(request)
    pack = _load_pack(st, pack_id)
    with st.analysis_lock:
        suggestions = st.recomposer.alternatives(pack, body.material_id, body.group, limit=body.limit)
    return {
        "pack_id": pack.id,
        "suggestions": [{k: v for k, v in s.items() if k != "material"} for s in suggestions],
        "materials": {s["material_id"]: s["material"].model_dump() for s in suggestions},
    }


@app.get("/packs/{pack_id}/export")
def export_pack(request: Request, pack_id: str, format: str = "markdown"):
    st = state(request)
    pack = _load_pack(st, pack_id)
    if format == "json":
        return pack_to_json(pack, st.db)
    return PlainTextResponse(pack_to_markdown(pack, st.db), media_type="text/markdown; charset=utf-8")
