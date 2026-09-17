"""SQLite persistence for Materials, embeddings, full-text index and Packs.

Deliberately boring: one file, stdlib sqlite3, JSON columns.  Human-authored fields,
file-derived fields and model-derived fields live in separate columns so that
re-analysis can never overwrite a Human Thought (spec §6.3-B).

Vector search: embeddings are float32 blobs; similarity is brute-force cosine with
numpy over the whole library.  At MVP scale (≤ a few hundred materials) this is the
simplest correct "vector store"; swap for sqlite-vec / pgvector when the library grows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

import numpy as np

from .models import Analysis, Human, Material, MaterialPack, Original, Processing, Provenance, Source, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    id              TEXT PRIMARY KEY,
    modality        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    original_json   TEXT NOT NULL,
    source_json     TEXT NOT NULL,
    human_json      TEXT NOT NULL,
    objective_json  TEXT NOT NULL,
    analysis_json   TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    processing_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS material_embeddings (
    material_id TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'combined' (thought + machine) | 'human' (thought only)
    model       TEXT,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    text        TEXT,                   -- exactly what was embedded (transparency / debugging)
    PRIMARY KEY (material_id, kind)
);

CREATE TABLE IF NOT EXISTS packs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    doc         TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS materials_fts USING fts5(
    material_id UNINDEXED,
    thought,
    machine,
    tokenize = 'trigram'
);
"""


def _dumps(obj) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    return json.dumps(obj, ensure_ascii=False)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.executescript(FTS_SCHEMA)
            self.conn.commit()
        self._vec_cache: dict[str, tuple[list[str], np.ndarray]] = {}

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- materials
    def insert_material(self, m: Material) -> Material:
        with self._lock:
            self.conn.execute(
                """INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.id,
                    m.modality,
                    m.created_at,
                    m.updated_at,
                    _dumps(m.original),
                    _dumps(m.source),
                    _dumps(m.human),
                    _dumps(m.objective_metadata),
                    _dumps(m.analysis),
                    _dumps(m.provenance),
                    _dumps(m.processing),
                ),
            )
            self.conn.commit()
        self._refresh_fts(m)
        return m

    def _row_to_material(self, r: sqlite3.Row) -> Material:
        return Material(
            id=r["id"],
            modality=r["modality"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            original=Original(**json.loads(r["original_json"])),
            source=Source(**json.loads(r["source_json"])),
            human=Human(**json.loads(r["human_json"])),
            objective_metadata=json.loads(r["objective_json"]),
            analysis=Analysis(**json.loads(r["analysis_json"])),
            provenance=Provenance(**json.loads(r["provenance_json"])),
            processing=Processing(**json.loads(r["processing_json"])),
        )

    def get_material(self, material_id: str) -> Material | None:
        row = self.conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        return self._row_to_material(row) if row else None

    def get_materials(self, ids: Iterable[str]) -> dict[str, Material]:
        ids = list(ids)
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"SELECT * FROM materials WHERE id IN ({q})", ids).fetchall()
        return {r["id"]: self._row_to_material(r) for r in rows}

    def list_materials(self, limit: int = 500, offset: int = 0, modality: str | None = None) -> list[Material]:
        sql = "SELECT * FROM materials"
        args: list = []
        if modality:
            sql += " WHERE modality=?"
            args.append(modality)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [self._row_to_material(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_materials(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0])

    def find_by_sha256(self, sha256: str) -> Material | None:
        # original_json is small; a LIKE scan is fine at MVP scale.
        row = self.conn.execute(
            "SELECT * FROM materials WHERE original_json LIKE ?", (f'%"sha256": "{sha256}"%',)
        ).fetchone()
        return self._row_to_material(row) if row else None

    def update_human(self, material_id: str, human: Human) -> None:
        """Only the human column changes. Model output never goes through here."""
        with self._lock:
            self.conn.execute(
                "UPDATE materials SET human_json=?, updated_at=? WHERE id=?",
                (_dumps(human), now_iso(), material_id),
            )
            self.conn.commit()
        m = self.get_material(material_id)
        if m:
            self._refresh_fts(m)

    def update_analysis(
        self,
        material_id: str,
        analysis: Analysis,
        objective_metadata: dict,
        provenance: Provenance,
        processing: Processing,
    ) -> None:
        """Model/file derived columns only — never touches human_json / source_json."""
        with self._lock:
            self.conn.execute(
                """UPDATE materials SET analysis_json=?, objective_json=?, provenance_json=?,
                   processing_json=?, updated_at=? WHERE id=?""",
                (
                    _dumps(analysis),
                    _dumps(objective_metadata),
                    _dumps(provenance),
                    _dumps(processing),
                    now_iso(),
                    material_id,
                ),
            )
            self.conn.commit()
        m = self.get_material(material_id)
        if m:
            self._refresh_fts(m)

    def update_processing(self, material_id: str, processing: Processing) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE materials SET processing_json=?, updated_at=? WHERE id=?",
                (_dumps(processing), now_iso(), material_id),
            )
            self.conn.commit()

    def delete_material(self, material_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM materials WHERE id=?", (material_id,))
            self.conn.execute("DELETE FROM material_embeddings WHERE material_id=?", (material_id,))
            self.conn.execute("DELETE FROM materials_fts WHERE material_id=?", (material_id,))
            self.conn.commit()
        self._vec_cache.clear()

    # ----------------------------------------------------------------- full text
    def _refresh_fts(self, m: Material) -> None:
        machine_bits = [
            m.source.page_title or "",
            m.analysis.summary or "",
            " ".join(m.analysis.subjects),
            " ".join(m.analysis.style),
            " ".join(m.analysis.keywords),
            " ".join(m.analysis.mood),
            m.analysis.ocr or "",
            (m.original.content or "")[:4000],
        ]
        with self._lock:
            self.conn.execute("DELETE FROM materials_fts WHERE material_id=?", (m.id,))
            self.conn.execute(
                "INSERT INTO materials_fts(material_id, thought, machine) VALUES (?,?,?)",
                (m.id, m.human.thought or "", "\n".join(b for b in machine_bits if b)),
            )
            self.conn.commit()

    def fts_search(self, query: str, limit: int = 20) -> list[tuple[str, float, str]]:
        """Returns (material_id, score, via) with via in {"human_thought", "machine"}.

        Trigram FTS needs >= 3 chars; shorter queries fall back to LIKE.
        Scores are bm25-derived and only meant for fusion, not display.
        """
        query = (query or "").strip()
        if not query:
            return []
        results: dict[str, tuple[float, str]] = {}
        if len(query) >= 3:
            safe = '"' + query.replace('"', '""') + '"'
            try:
                rows = self.conn.execute(
                    """SELECT material_id, bm25(materials_fts, 0, 2.0, 1.0) AS rank,
                              highlight(materials_fts, 1, '[', ']') AS h_thought
                       FROM materials_fts WHERE materials_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (safe, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                via = "human_thought" if "[" in (r["h_thought"] or "") else "machine"
                results[r["material_id"]] = (-float(r["rank"]), via)
        else:
            like = f"%{query}%"
            rows = self.conn.execute(
                "SELECT material_id, thought, machine FROM materials_fts WHERE thought LIKE ? OR machine LIKE ? LIMIT ?",
                (like, like, limit),
            ).fetchall()
            for r in rows:
                via = "human_thought" if query in (r["thought"] or "") else "machine"
                results[r["material_id"]] = (1.0, via)
        return [(mid, s, via) for mid, (s, via) in results.items()]

    # ----------------------------------------------------------------- embeddings
    def upsert_embedding(self, material_id: str, kind: str, model: str, vector: list[float], text: str) -> None:
        arr = np.asarray(vector, dtype=np.float32)
        with self._lock:
            self.conn.execute(
                """INSERT INTO material_embeddings(material_id, kind, model, dim, vector, text)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(material_id, kind) DO UPDATE SET model=excluded.model, dim=excluded.dim,
                   vector=excluded.vector, text=excluded.text""",
                (material_id, kind, model, int(arr.shape[0]), arr.tobytes(), text),
            )
            self.conn.commit()
        self._vec_cache.pop(kind, None)

    def get_embedding(self, material_id: str, kind: str = "combined") -> np.ndarray | None:
        row = self.conn.execute(
            "SELECT vector, dim FROM material_embeddings WHERE material_id=? AND kind=?", (material_id, kind)
        ).fetchone()
        if not row:
            return None
        return np.frombuffer(row["vector"], dtype=np.float32, count=row["dim"])

    def embedding_matrix(self, kind: str) -> tuple[list[str], np.ndarray]:
        """All vectors of one kind as an L2-normalised matrix (cached until the next write)."""
        if kind in self._vec_cache:
            return self._vec_cache[kind]
        rows = self.conn.execute(
            "SELECT material_id, vector, dim FROM material_embeddings WHERE kind=?", (kind,)
        ).fetchall()
        ids = [r["material_id"] for r in rows]
        if not rows:
            mat = np.zeros((0, 0), dtype=np.float32)
        else:
            mat = np.stack([np.frombuffer(r["vector"], dtype=np.float32, count=r["dim"]) for r in rows])
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            mat = mat / norms
        self._vec_cache[kind] = (ids, mat)
        return ids, mat

    def vector_search(self, query_vec: np.ndarray, kind: str, limit: int = 20) -> list[tuple[str, float]]:
        ids, mat = self.embedding_matrix(kind)
        if not ids:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        n = np.linalg.norm(q)
        if n == 0:
            return []
        sims = mat @ (q / n)
        order = np.argsort(-sims)[:limit]
        return [(ids[i], float(sims[i])) for i in order]

    def embedding_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT material_id) AS n, MAX(dim) AS dim, MAX(model) AS model FROM material_embeddings"
        ).fetchone()
        return {"materials_with_embeddings": row["n"], "dim": row["dim"], "model": row["model"]}

    # ----------------------------------------------------------------- packs
    def save_pack(self, pack: MaterialPack) -> MaterialPack:
        pack.updated_at = now_iso()
        with self._lock:
            self.conn.execute(
                """INSERT INTO packs(id, name, created_at, updated_at, doc) VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at, doc=excluded.doc""",
                (pack.id, pack.name, pack.created_at, pack.updated_at, pack.model_dump_json()),
            )
            self.conn.commit()
        return pack

    def get_pack(self, pack_id: str) -> MaterialPack | None:
        row = self.conn.execute("SELECT doc FROM packs WHERE id=?", (pack_id,)).fetchone()
        return MaterialPack.model_validate_json(row["doc"]) if row else None

    def list_packs(self, limit: int = 100) -> list[MaterialPack]:
        rows = self.conn.execute("SELECT doc FROM packs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [MaterialPack.model_validate_json(r["doc"]) for r in rows]

    def delete_pack(self, pack_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM packs WHERE id=?", (pack_id,))
            self.conn.commit()
