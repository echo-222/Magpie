"""Magpie key objects (spec §3): Material, Task, Match, Material Pack, Agent Context.

These are the Magpie-specific product objects. `human.thought` is first-class and is
never overwritten by model output (provenance keeps human / file / model fields apart).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Modality = Literal["image", "text"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- Material


class Original(BaseModel):
    """The saved thing itself. Images are stored as files under data/files; text inline."""

    file_path: str | None = None  # relative to settings.files_dir
    content: str | None = None  # text materials
    mime_type: str | None = None
    size: int | None = None
    sha256: str | None = None
    filename: str | None = None


class Source(BaseModel):
    page_url: str | None = None
    resource_url: str | None = None
    page_title: str | None = None
    captured_at: str | None = None
    license: str | None = None  # kept for demo materials pulled from the open web


class Human(BaseModel):
    """Why did this matter to me when I saved it? First-class, never machine-edited."""

    thought: str | None = None


class ColorSwatch(BaseModel):
    hex: str
    ratio: float  # 0..1 share of sampled pixels
    name: str | None = None  # coarse hue family, e.g. "warm neutral", "blue"


class Analysis(BaseModel):
    """Machine understanding: What is this?"""

    summary: str | None = None
    subjects: list[str] = Field(default_factory=list)
    ocr: str | None = None
    colors: list[ColorSwatch] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    mood: list[str] = Field(default_factory=list)
    model: str | None = None
    ocr_engine: str | None = None
    analyzed_at: str | None = None


class Provenance(BaseModel):
    """Which top-level fields came from the human, from the file, from a model.

    (Spec calls the last one `model_fields`; that name is reserved by pydantic, so
    it is `machine_fields` here.)
    """

    human_fields: list[str] = Field(default_factory=list)
    file_fields: list[str] = Field(default_factory=list)
    machine_fields: list[str] = Field(default_factory=list)


class Processing(BaseModel):
    status: Literal["pending", "analyzing", "ready", "failed"] = "pending"
    error: str | None = None
    updated_at: str | None = None


class Material(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mat"))
    modality: Modality
    original: Original = Field(default_factory=Original)
    source: Source = Field(default_factory=Source)
    human: Human = Field(default_factory=Human)
    objective_metadata: dict[str, Any] = Field(default_factory=dict)
    analysis: Analysis = Field(default_factory=Analysis)
    provenance: Provenance = Field(default_factory=Provenance)
    processing: Processing = Field(default_factory=Processing)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    # convenience for UI / export -------------------------------------------------
    def short_label(self) -> str:
        if self.original.filename:
            return self.original.filename
        if self.source.page_title:
            return self.source.page_title
        if self.original.content:
            return self.original.content.strip().splitlines()[0][:40]
        return self.id

    def ai_line(self) -> str:
        parts = [*self.analysis.style[:3], *self.analysis.keywords[:4]]
        seen: list[str] = []
        for p in parts:
            if p and p not in seen:
                seen.append(p)
        return " / ".join(seen[:6])


class CapturePayload(BaseModel):
    """Material input contract for the capture layer (spec §10).

    The browser-extension teammate sends this (plus the original file for images)
    without needing to know how analysis works.
    """

    modality: Modality
    content: str | None = None  # text materials
    source: Source = Field(default_factory=Source)
    human: Human = Field(default_factory=Human)


# --------------------------------------------------------------------------- Task


class Task(BaseModel):
    """What the user is trying to make right now (spec §3.2). Kept deliberately small."""

    raw_request: str
    purpose: str | None = None
    desired_qualities: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    needed_reference_types: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)  # query expansion for retrieval
    language: str | None = None
    # Structured hints pulled out of an abstract request so retrieval can use *facts* about the
    # materials (palette hue families, modality) and not only embeddings.  Shape (all optional):
    #   {"colors": {"primary": ["red"], "adjacent": ["pink", "orange"], "saturation": "any|high|low",
    #               "lightness": "any|dark|light", "weight": "must|prefer"},
    #    "modality": "image|text|any", "must_terms": ["..."]}
    facets: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- Match / Pack


class PackMember(BaseModel):
    """A Match placed inside a pack group: similarity != usefulness != role."""

    material_id: str
    role: str | None = None  # chosen role inside the pack, e.g. "texture reference"
    reason: str | None = None  # why this helps THIS task now
    note: str | None = None  # optional human task-specific note
    score: float | None = None  # retrieval score (for transparency only)
    relevance: int | None = None  # 0-3 judged relevance to the task (agent step 3)
    added_by: Literal["magpie", "human"] = "magpie"


class PackGroup(BaseModel):
    name: str
    purpose: str | None = None
    members: list[PackMember] = Field(default_factory=list)


class Candidate(BaseModel):
    """Retrieval candidate kept with the pack so 'find alternatives' can reuse it."""

    material_id: str
    score: float
    via: str  # "human_thought" | "machine" | "fts"
    relevance: int | None = None  # judged 0-3 (None when the judge step was skipped)
    why: str | None = None  # judge's one-line verdict
    signals: dict[str, float | None] = Field(default_factory=dict)  # combined / human / fts / color


class HumanEdit(BaseModel):
    op: Literal["remove", "move", "add", "note", "rename_group"]
    at: str = Field(default_factory=now_iso)
    material_id: str | None = None
    from_group: str | None = None
    to_group: str | None = None
    detail: str | None = None


class MaterialPack(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pack"))
    name: str
    task: Task
    groups: list[PackGroup] = Field(default_factory=list)
    human_direction: str | None = None  # one-paragraph restatement used in export
    candidates: list[Candidate] = Field(default_factory=list)
    excluded: list[dict[str, str]] = Field(default_factory=list)  # {material_id, reason}
    gaps: list[str] = Field(default_factory=list)  # what the library lacks for this task (agent is honest about coverage)
    removed_material_ids: list[str] = Field(default_factory=list)  # human removals: never re-suggested
    human_edits: list[HumanEdit] = Field(default_factory=list)
    generation: dict[str, Any] = Field(default_factory=dict)  # model names, timings
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    # helpers ---------------------------------------------------------------------
    def member_ids(self) -> list[str]:
        return [m.material_id for g in self.groups for m in g.members]

    def find_group(self, name: str) -> PackGroup | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None

    def find_member(self, material_id: str) -> tuple[PackGroup, PackMember] | None:
        for g in self.groups:
            for m in g.members:
                if m.material_id == material_id:
                    return g, m
        return None
