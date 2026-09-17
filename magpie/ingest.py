"""Material ingestion: capture payload (+ file) -> stored Material -> analysis -> embeddings.

Two steps on purpose (`create_*` then `analyze`) so the API can answer immediately and
analyse in the background while the CLI runs both synchronously.
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .analysis import image as img_an
from .analysis import text as txt_an
from .config import Settings, get_settings
from .db import Database
from .llm import LLM, get_llm
from .models import Analysis, CapturePayload, ColorSwatch, Material, Original, Processing, Provenance, now_iso
from .retrieval import build_embedding_texts

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


@dataclass
class Ingestor:
    db: Database
    settings: Settings
    llm: LLM

    @classmethod
    def default(cls, db: Database) -> "Ingestor":
        s = get_settings()
        s.ensure_dirs()
        return cls(db=db, settings=s, llm=get_llm(s))

    # ------------------------------------------------------------------ create
    def create_image(self, data: bytes, filename: str | None, payload: CapturePayload) -> tuple[Material, bool]:
        """Store the original file and the Material row. Returns (material, created)."""
        if not data:
            raise ValueError("image material needs a file")
        try:  # fail at save time, not minutes later in background analysis
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
                fmt = (probe.format or "").lower()
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"file is not a decodable image ({filename or 'upload'}); send JPEG/PNG/WebP/GIF bytes") from e
        sha = hashlib.sha256(data).hexdigest()
        existing = self.db.find_by_sha256(sha)
        if existing:
            return existing, False
        ext = (Path(filename).suffix.lower() if filename else "") or ""
        if ext not in IMAGE_EXT:
            ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif", "bmp": ".bmp", "tiff": ".tif"}.get(fmt, ".jpg")
        m = Material(modality="image", source=payload.source, human=payload.human)
        rel = f"{m.id}{ext}"
        self.settings.ensure_dirs()
        (self.settings.files_dir / rel).write_bytes(data)
        m.original = Original(
            file_path=rel,
            mime_type=mimetypes.types_map.get(ext, "image/jpeg"),
            size=len(data),
            sha256=sha,
            filename=filename or rel,
        )
        if not m.source.captured_at:
            m.source.captured_at = now_iso()
        m.provenance = Provenance(
            human_fields=["human.thought"] if payload.human.thought else [],
            file_fields=["original"],
            machine_fields=[],
        )
        self.db.insert_material(m)
        return m, True

    def create_text(self, payload: CapturePayload) -> tuple[Material, bool]:
        content = (payload.content or "").strip()
        if not content:
            raise ValueError("text material needs content")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = self.db.find_by_sha256(sha)
        if existing:
            return existing, False
        m = Material(modality="text", source=payload.source, human=payload.human)
        m.original = Original(content=content, mime_type="text/plain", size=len(content.encode("utf-8")), sha256=sha)
        if not m.source.captured_at:
            m.source.captured_at = now_iso()
        m.provenance = Provenance(
            human_fields=["human.thought"] if payload.human.thought else [],
            file_fields=["original"],
            machine_fields=[],
        )
        m.objective_metadata = txt_an.text_metadata(content)
        self.db.insert_material(m)
        return m, True

    # ------------------------------------------------------------------ analyse
    def analyze(self, material_id: str) -> Material:
        m = self.db.get_material(material_id)
        if m is None:
            raise KeyError(material_id)
        self.db.update_processing(m.id, Processing(status="analyzing", updated_at=now_iso()))
        try:
            if m.modality == "image":
                analysis, objective = self._analyze_image(m)
            else:
                analysis, objective = self._analyze_text(m)
            analysis.model = self.llm.vision_model if m.modality == "image" else self.llm.chat_model
            analysis.analyzed_at = now_iso()
            prov = Provenance(
                human_fields=["human.thought"] if m.human.thought else [],
                file_fields=["original", "objective_metadata"] + (["analysis.colors"] if m.modality == "image" else []),
                machine_fields=[
                    f"analysis.{f}" for f in ("summary", "subjects", "style", "keywords", "mood") if getattr(analysis, f)
                ]
                + (["analysis.ocr"] if analysis.ocr else []),
            )
            m.analysis, m.objective_metadata, m.provenance = analysis, objective, prov
            self._embed(m)
            self.db.update_analysis(m.id, analysis, objective, prov, Processing(status="ready", updated_at=now_iso()))
        except Exception as e:  # noqa: BLE001 - persist the failure, never lose the material
            log.exception("analysis failed for %s", material_id)
            self.db.update_processing(m.id, Processing(status="failed", error=str(e)[:500], updated_at=now_iso()))
        return self.db.get_material(material_id)  # type: ignore[return-value]

    def ingest_image(self, data: bytes, filename: str | None, payload: CapturePayload) -> Material:
        m, created = self.create_image(data, filename, payload)
        return self.analyze(m.id) if created or m.processing.status != "ready" else m

    def ingest_text(self, payload: CapturePayload) -> Material:
        m, created = self.create_text(payload)
        return self.analyze(m.id) if created or m.processing.status != "ready" else m

    # ------------------------------------------------------------------ internals
    def _analyze_image(self, m: Material) -> tuple[Analysis, dict]:
        path = self.settings.files_dir / (m.original.file_path or "")
        objective = img_an.file_metadata(path)
        img = img_an.load_image(path)
        swatches, palette = img_an.dominant_colors(img)
        objective["palette"] = palette
        img_an.make_thumbnail(img, self.settings.thumbs_dir / f"{m.id}.jpg")

        ocr_txt, ocr_engine = (None, None)
        if self.settings.ocr_mode != "off":
            ocr_txt, ocr_engine = img_an.ocr_text(path)

        desc = img_an.describe_image(self.llm, img_an.prepare_for_vision(img))
        ocr_final = ocr_txt if ocr_txt else (desc.get("visible_text") or None)
        if not ocr_txt and ocr_final:
            ocr_engine = "vision-model"
        analysis = Analysis(
            summary=desc.get("summary"),
            subjects=desc.get("subjects", []),
            ocr=ocr_final or None,
            colors=[ColorSwatch(**c.model_dump()) for c in swatches],
            style=desc.get("style", []),
            keywords=desc.get("keywords", []),
            mood=desc.get("mood", []),
            ocr_engine=ocr_engine if ocr_final else None,
        )
        return analysis, objective

    def _analyze_text(self, m: Material) -> tuple[Analysis, dict]:
        content = m.original.content or ""
        objective = dict(m.objective_metadata) or txt_an.text_metadata(content)
        res = txt_an.analyze_text(self.llm, content)
        analysis = Analysis(
            summary=res.get("summary"),
            subjects=res.get("topics", []),
            keywords=res.get("keywords", []),
            style=res.get("tone", []),
        )
        return analysis, objective

    def _embed(self, m: Material) -> None:
        texts = build_embedding_texts(m)
        kinds = list(texts.keys())
        vecs = self.llm.embed([texts[k] for k in kinds])
        for kind, vec in zip(kinds, vecs):
            self.db.upsert_embedding(m.id, kind, self.llm.embed_model, vec, texts[kind])
        if "human" not in texts:
            # thought removed/absent: make sure no stale human vector remains
            self.db.conn.execute("DELETE FROM material_embeddings WHERE material_id=? AND kind='human'", (m.id,))
            self.db.conn.commit()
            self.db._vec_cache.pop("human", None)

    def reembed_human(self, material_id: str) -> None:
        """Called after a Human Thought edit: only the vectors change, analysis is untouched."""
        m = self.db.get_material(material_id)
        if m:
            self._embed(m)
