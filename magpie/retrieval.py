"""Retrieval over Material Memory: Human Thought + machine understanding, hybrid.

Magpie-specific part: every material has TWO embeddings —
  * `combined` : Human Thought + machine understanding + text/OCR (what it is, and why it mattered)
  * `human`    : the Human Thought alone (why it mattered), when present
so that a task can hit either "what the thing is" or "what the person saw in it".
Full-text (SQLite FTS5) hits are fused in for exact keyword matches (names, OCR text).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .db import Database
from .llm import LLM
from .models import Material

W_COMBINED = 0.65
W_HUMAN = 0.35
FTS_BONUS = 0.08


def build_embedding_texts(m: Material) -> dict[str, str]:
    """What gets embedded. Kept explicit and stored next to the vector for transparency."""
    a = m.analysis
    parts: list[str] = []
    if m.human.thought:
        parts.append(f"Human thought: {m.human.thought}")
    if m.source.page_title:
        parts.append(f"Title: {m.source.page_title}")
    if a.summary:
        parts.append(f"Summary: {a.summary}")
    if a.subjects:
        parts.append("Subjects: " + ", ".join(a.subjects))
    if a.style:
        parts.append("Style: " + ", ".join(a.style))
    if a.keywords:
        parts.append("Keywords: " + ", ".join(a.keywords))
    if a.mood:
        parts.append("Mood: " + ", ".join(a.mood))
    palette = (m.objective_metadata.get("palette") or {}).get("descriptors") if m.modality == "image" else None
    if palette:
        names = [c.name for c in a.colors[:4] if c.name]
        parts.append("Palette: " + ", ".join(palette + names))
    if a.ocr:
        parts.append("Text in image: " + a.ocr[:500])
    if m.modality == "text" and m.original.content:
        parts.append("Text: " + m.original.content[:1500])
    texts = {"combined": "\n".join(parts) if parts else (m.original.content or m.id)}
    if m.human.thought:
        texts["human"] = m.human.thought
    return texts


@dataclass
class Hit:
    material_id: str
    score: float
    via: str  # human_thought | machine | fts
    signals: dict[str, float] = field(default_factory=dict)
    material: Material | None = None


@dataclass
class Retriever:
    db: Database
    llm: LLM

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(self.llm.embed([text])[0], dtype=np.float32)

    def search(
        self,
        queries: list[str] | str,
        limit: int = 12,
        exclude: set[str] | None = None,
        query_vectors: list[np.ndarray] | None = None,
    ) -> list[Hit]:
        """Hybrid search. `queries` may hold several phrasings (task query expansion);
        a material's score is its best score over all phrasings."""
        if isinstance(queries, str):
            queries = [queries]
        queries = [q for q in queries if q and q.strip()]
        exclude = exclude or set()
        vectors = list(query_vectors or [])
        if queries:
            vectors += [np.asarray(v, dtype=np.float32) for v in self.llm.embed(queries)]
        if not vectors:
            return []

        best: dict[str, Hit] = {}
        for qv in vectors:
            comb = dict(self.db.vector_search(qv, "combined", limit=200))
            hum = dict(self.db.vector_search(qv, "human", limit=200))
            for mid, s_comb in comb.items():
                if mid in exclude:
                    continue
                s_hum = hum.get(mid)
                if s_hum is None:
                    score, via = s_comb, "machine"
                else:
                    score = W_COMBINED * s_comb + W_HUMAN * s_hum
                    via = "human_thought" if s_hum >= s_comb else "machine"
                prev = best.get(mid)
                if prev is None or score > prev.score:
                    best[mid] = Hit(mid, score, via, {"combined": round(s_comb, 4), "human": round(s_hum, 4) if s_hum is not None else None})

        # exact keyword hits get a small bonus (names, OCR text, brand words)
        for q in queries:
            for mid, _s, via in self.db.fts_search(q, limit=50):
                if mid in exclude:
                    continue
                if mid in best:
                    best[mid].score += FTS_BONUS
                    best[mid].signals["fts"] = 1.0
                else:
                    best[mid] = Hit(mid, FTS_BONUS + 0.3, "fts", {"fts": 1.0})

        ranked = sorted(best.values(), key=lambda h: -h.score)
        # attach materials *before* cutting to `limit`: a vector whose material is gone must not
        # take a slot away from a real hit
        mats = self.db.get_materials([h.material_id for h in ranked])
        hits: list[Hit] = []
        for h in ranked:
            h.material = mats.get(h.material_id)
            if h.material is not None:
                hits.append(h)
            if len(hits) >= limit:
                break
        return hits

    def similar_to(self, material_id: str, limit: int = 8, exclude: set[str] | None = None) -> list[Hit]:
        """"More like this" seeded by a material's own combined embedding."""
        vec = self.db.get_embedding(material_id, "combined")
        if vec is None:
            return []
        ex = set(exclude or set()) | {material_id}
        return self.search([], limit=limit, exclude=ex, query_vectors=[vec])
