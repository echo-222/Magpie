"""Thin FastAPI layer + one static dev page.

The HTTP contract is what the capture/front-end teammate will talk to later
(POST /materials with a CapturePayload + file).  Nothing here knows about analysis
details; that lives in ingest.py / recompose.py.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .config import get_settings
from .db import Database
from .ingest import Ingestor
from .llm import get_llm
from .models import CapturePayload, Human, Source
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
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
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


def _pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
