"""Material Pack building and editing helpers (spec §6.3-F).

`Recomposer.build` runs the retrieval agent (agent.py: understand -> recall -> judge -> compose ->
verify).  This module keeps what the pack *editing* side needs: candidate cards, plan validation
and task-aware "find alternatives".  Groups are never a fixed taxonomy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .db import Database
from .llm import LLM
from .models import Material, MaterialPack
from .retrieval import Hit, Retriever

log = logging.getLogger(__name__)

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


def plan_schema_problems(plan: dict, valid_ids: set[str]) -> list[str]:
    """Structural check of a recomposition reply before it is turned into a MaterialPack.
    Returns an empty list when the plan can be used (pydantic does the field-level coercion)."""
    problems: list[str] = []
    if not isinstance(plan, dict):
        return ["reply is not a JSON object"]
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        return ["missing or empty 'groups' array"]
    n_members = n_valid = 0
    for g in groups:
        if not isinstance(g, dict):
            problems.append("a group is not an object")
            continue
        if not str(g.get("name") or "").strip():
            problems.append("a group has no 'name'")
        members = g.get("members")
        if not isinstance(members, list):
            problems.append(f"group {g.get('name')!r} has no 'members' array")
            continue
        for mem in members:
            n_members += 1
            if isinstance(mem, dict) and str(mem.get("material_id", "")).strip() in valid_ids:
                n_valid += 1
                if not str(mem.get("reason") or "").strip():
                    problems.append(f"{mem.get('material_id')} has no 'reason'")
            else:
                problems.append(f"unknown material_id {mem.get('material_id') if isinstance(mem, dict) else mem!r}")
    if n_valid == 0:
        problems.append("no member uses a valid material_id")
    # tolerate a few stray ids / missing reasons as long as most of the plan is usable
    if n_valid and len(problems) <= max(1, n_members // 4):
        return []
    return problems


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
    def build(self, raw_task: str, limit: int | None = None) -> MaterialPack:
        """Run the retrieval agent (understand -> recall -> judge -> compose -> verify) and save the pack.
        `limit` caps recall (how many candidates the judge sees), default agent.RECALL_K."""
        from .agent import RECALL_K, RetrievalAgent  # local import: agent.py reuses helpers from here

        agent = RetrievalAgent(db=self.db, retriever=self.retriever, llm=self.llm)
        return agent.build_pack(raw_task, limit=limit or RECALL_K)

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
