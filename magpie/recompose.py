"""Task -> candidates -> task-aware selection -> groups -> reasons -> Material Pack (spec §6.3-F).

This is the differentiated Magpie logic. Retrieval only proposes; the recomposition
step decides *usefulness for this task*, assigns each material a role inside a
task-specific group, and writes a reason that references the task (and the human's
own thought when relevant).  Groups are not a fixed taxonomy.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from .db import Database
from .llm import LLM
from .models import Candidate, Material, MaterialPack, PackGroup, PackMember, Task
from .retrieval import Hit, Retriever
from .task import understand_task

log = logging.getLogger(__name__)

# Prompt shape matters for small local models (qwen3:8b): the exact keys are stated in the system
# prompt AND repeated as a fill-in skeleton (with two real candidate ids) at the very end.
RECOMPOSE_SYSTEM = """You are Magpie, a material-memory assistant for a designer. You receive a task brief and candidate materials the designer saved earlier, each with the designer's own Human Thought (why it caught them) and machine analysis (what it is). Recompose the useful ones into a task-specific Material Pack.
You MUST answer with one JSON object with EXACTLY these top-level keys: "human_direction" (string), "groups" (array), "excluded" (array).
Each group: {"name": string, "purpose": string, "members": [{"material_id": string, "role": string, "reason": string}]}.
Each excluded item: {"material_id": string, "reason": string}.
material_id values MUST be copied verbatim from the candidate list (they look like mat_xxxxxxxxxx). Never invent ids or labels.
Rules:
- Usefulness for THIS task beats similarity; leave out candidates that are merely similar (list them in "excluded" with a short reason).
- 2-5 task-specific groups (e.g. 材质/质感, 色彩方向, 排版/字体, 氛围, 文案语气, 版式结构, 反例). No generic buckets like "Images"/"Texts". 1-4 members each; each material at most once.
- A material may serve as a counter-example when the task says what to avoid; say so in its role.
- Every reason says why the material helps THIS task now (1-2 sentences) and builds on the Human Thought when relevant.
- "human_direction": 2-3 sentences restating the direction for another AI agent: what to go for, what to avoid.
Write in the task's language."""

RECOMPOSE_USER = """TASK BRIEF
raw: {raw}
purpose: {purpose}
desired qualities: {desired}
avoid: {avoid}
constraints: {constraints}
reference types wanted: {ref_types}

CANDIDATES ({n})
{candidates}

Now return ONLY the JSON object. Skeleton to fill (keep these exact keys; replace the values; use only candidate ids):
{{"human_direction": "...", "groups": [{{"name": "...", "purpose": "...", "members": [{{"material_id": "{ex1}", "role": "...", "reason": "..."}}]}}], "excluded": [{{"material_id": "{ex2}", "reason": "..."}}]}}"""

ALTERNATIVES_SYSTEM = (
    "You are Magpie. A designer is building a Material Pack for a task and asked for more materials like a given one "
    "for a specific group. For each candidate write one short reason (task language) saying how it would help this task "
    "in that group, or state briefly that it does not fit. Use only the given material_id values. Return JSON only."
)

ALTERNATIVES_USER = """TASK: {raw}
direction: {direction}
GROUP: {group}
SEED MATERIAL: {seed}

CANDIDATES
{candidates}

Return ONLY a JSON object with the single key "reasons" mapping every candidate id to one sentence, e.g.
{{"reasons": {{"{ex1}": "...", "{ex2}": "..."}}}}"""


def describe_candidate(m: Material, hit: Hit | None = None) -> str:
    """Compact candidate card. Kept short on purpose: local 7-8B models lose the schema when
    the prompt approaches their context window (Ollama defaults to 4096 tokens)."""
    a = m.analysis
    lines = [f"[{m.id}] {m.modality} | {(m.source.page_title or m.original.filename or m.short_label())[:60]}"]
    lines.append(f"  Human thought: {(m.human.thought or '(none)')[:120]}")
    ai_bits = []
    if a.summary:
        ai_bits.append(a.summary[:110])
    tags = [*a.style[:4], *[k for k in a.keywords[:5] if k not in a.style], *a.mood[:2]]
    if tags:
        ai_bits.append("tags: " + ", ".join(dict.fromkeys(tags)))
    if m.modality == "image":
        pal = (m.objective_metadata.get("palette") or {}).get("descriptors") or []
        cols = [c.name for c in a.colors[:3] if c.name]
        if pal or cols:
            ai_bits.append("palette: " + ", ".join(dict.fromkeys(pal + cols)))
    if ai_bits:
        lines.append("  AI: " + " | ".join(ai_bits))
    if m.modality == "text" and m.original.content:
        lines.append("  Text: " + m.original.content.strip().replace("\n", " ")[:160])
    elif a.ocr:
        lines.append("  Text in image: " + a.ocr.replace("\n", " ")[:60])
    if hit is not None:
        lines.append(f"  retrieval: {hit.score:.2f} via {hit.via}")
    return "\n".join(lines)


@dataclass
class Recomposer:
    db: Database
    retriever: Retriever
    llm: LLM

    # ------------------------------------------------------------------ build
    def build(self, raw_task: str, limit: int = 12) -> MaterialPack:
        t0 = time.time()
        task = understand_task(self.llm, raw_task)
        t1 = time.time()
        hits = self.retriever.search([task.raw_request] + task.search_queries, limit=limit)
        t2 = time.time()
        plan, fallback = self._plan(task, hits)
        t3 = time.time()

        by_id = {h.material_id: h for h in hits}
        groups: list[PackGroup] = []
        used: set[str] = set()
        for g in plan.get("groups", []):
            members: list[PackMember] = []
            for mem in g.get("members", []):
                mid = str(mem.get("material_id", "")).strip()
                if mid not in by_id or mid in used:
                    continue
                used.add(mid)
                members.append(
                    PackMember(
                        material_id=mid,
                        role=_s(mem.get("role")),
                        reason=_s(mem.get("reason")),
                        score=round(by_id[mid].score, 4),
                    )
                )
            if members:
                groups.append(PackGroup(name=_s(g.get("name")) or "Relevant", purpose=_s(g.get("purpose")), members=members))
        excluded = [
            {"material_id": str(e.get("material_id")), "reason": _s(e.get("reason")) or ""}
            for e in plan.get("excluded", [])
            if str(e.get("material_id", "")) in by_id and str(e.get("material_id")) not in used
        ]
        name = (task.purpose or task.raw_request).strip()[:80]
        pack = MaterialPack(
            name=name,
            task=task,
            groups=groups,
            human_direction=_s(plan.get("human_direction")),
            candidates=[Candidate(material_id=h.material_id, score=round(h.score, 4), via=h.via) for h in hits],
            excluded=excluded,
            generation={
                "chat_model": self.llm.chat_model,
                "embed_model": self.llm.embed_model,
                "fallback": fallback,
                "timing_s": {"task": round(t1 - t0, 1), "retrieve": round(t2 - t1, 1), "recompose": round(t3 - t2, 1)},
                "queries": [task.raw_request] + task.search_queries,
            },
        )
        return self.db.save_pack(pack)

    def _plan(self, task: Task, hits: list[Hit]) -> tuple[dict, bool]:
        if not hits:
            return {"groups": [], "excluded": [], "human_direction": task.purpose}, True
        cands = "\n".join(describe_candidate(h.material, h) for h in hits if h.material)
        user = RECOMPOSE_USER.format(
            raw=task.raw_request,
            purpose=task.purpose or "-",
            desired=", ".join(task.desired_qualities) or "-",
            avoid=", ".join(task.avoid) or "-",
            constraints=", ".join(task.constraints) or "-",
            ref_types=", ".join(task.needed_reference_types) or "-",
            n=len(hits),
            candidates=cands,
            ex1=hits[0].material_id,
            ex2=hits[-1].material_id,
        )
        try:
            plan = self.llm.chat_json(RECOMPOSE_SYSTEM, user, purpose="recompose", temperature=0.1, max_tokens=2000)
            if any(g.get("members") for g in plan.get("groups", []) if isinstance(g, dict)):
                return plan, False
            log.warning("recomposition returned no members; using retrieval fallback")
        except Exception as e:  # noqa: BLE001
            log.warning("recomposition failed (%s); using retrieval fallback", e)
        # plumbing fallback: keep the loop alive even when the model misbehaves (never used for validation runs)
        return {
            "human_direction": task.purpose,
            "groups": [
                {
                    "name": "Relevant materials (retrieval only)",
                    "purpose": "model recomposition unavailable; ranked by retrieval",
                    "members": [{"material_id": h.material_id, "role": None, "reason": f"retrieval match ({h.via}, {h.score:.2f})"} for h in hits[:8]],
                }
            ],
            "excluded": [],
        }, True

    # ------------------------------------------------------------------ alternatives
    def alternatives(self, pack: MaterialPack, material_id: str | None = None, group: str | None = None, limit: int = 4) -> list[dict]:
        """"Find more like this" that stays task-aware and never re-suggests human-removed items."""
        exclude = set(pack.member_ids()) | set(pack.removed_material_ids)
        task_queries = [pack.task.raw_request] + pack.task.search_queries
        seed = self.db.get_material(material_id) if material_id else None
        if seed is not None:
            exclude.add(seed.id)
            seed_vec = self.db.get_embedding(seed.id, "combined")
            task_vecs = self.llm.embed(task_queries[:2])
            task_vec = np.mean(np.asarray(task_vecs, dtype=np.float32), axis=0)
            if seed_vec is not None:
                blend = 0.6 * seed_vec / (np.linalg.norm(seed_vec) or 1) + 0.4 * task_vec / (np.linalg.norm(task_vec) or 1)
                hits = self.retriever.search([], limit=limit, exclude=exclude, query_vectors=[blend])
            else:
                hits = self.retriever.search(task_queries, limit=limit, exclude=exclude)
        else:
            hits = self.retriever.search(task_queries, limit=limit, exclude=exclude)
        if not hits:
            return []
        reasons: dict[str, str] = {}
        try:
            data = self.llm.chat_json(
                ALTERNATIVES_SYSTEM,
                ALTERNATIVES_USER.format(
                    raw=pack.task.raw_request,
                    direction=pack.human_direction or pack.task.purpose or "-",
                    group=group or "-",
                    seed=describe_candidate(seed) if seed else "(none: based on the task itself)",
                    candidates="\n".join(describe_candidate(h.material) for h in hits if h.material),
                    ex1=hits[0].material_id,
                    ex2=hits[-1].material_id,
                ),
                purpose="alternatives",
                temperature=0.1,
                max_tokens=800,
            )
            reasons = {str(k): str(v) for k, v in (data.get("reasons") or {}).items()}
        except Exception as e:  # noqa: BLE001
            log.warning("alternative reasons failed: %s", e)
        return [
            {
                "material_id": h.material_id,
                "score": round(h.score, 4),
                "via": h.via,
                "reason": reasons.get(h.material_id) or f"similar to the seed / task ({h.via}, {h.score:.2f})",
                "material": h.material,
            }
            for h in hits
        ]


def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
