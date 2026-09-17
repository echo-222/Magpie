"""Task understanding (spec §3.2): a real task -> small brief that drives retrieval + recomposition.

Deliberately tiny ontology. The one Magpie-specific trick is `search_queries`:
the task is rephrased into what good *reference material* would look/feel like,
because a library holds materials, not tasks.
"""

from __future__ import annotations

import logging

from .llm import LLM
from .models import Task

log = logging.getLogger(__name__)

TASK_SYSTEM = (
    "You turn a creator's task description into a small structured brief that is used to retrieve and "
    "recompose reference materials from their personal library (images, textures, posters, objects, text clippings). "
    "Be faithful to the task; do not invent constraints that are not stated or clearly implied. Return JSON only."
)

TASK_USER = """Task (verbatim):
\"\"\"{task}\"\"\"

Return ONLY JSON, written in the task's own language:
{{
  "purpose": "one sentence: what is being made, for what",
  "desired_qualities": ["3-6 short qualities the result should have"],
  "avoid": ["things explicitly or clearly implied to avoid; [] if none"],
  "constraints": ["hard constraints (medium, platform, audience, format); [] if none"],
  "needed_reference_types": ["2-5 kinds of reference that would help, e.g. 材质/质感, 色彩, 排版/字体, 氛围, 文案语气, 版式结构, 反例"],
  "search_queries": ["4-6 short retrieval phrases describing what a useful reference LOOKS or FEELS like (mix Chinese and English design terms); each phrase should target a different reference type"],
  "language": "zh or en"
}}"""


def understand_task(llm: LLM, raw_request: str) -> Task:
    raw = raw_request.strip()
    try:
        data = llm.chat_json(TASK_SYSTEM, TASK_USER.format(task=raw), purpose="task")
    except Exception as e:  # noqa: BLE001 - keep the loop alive: retrieval can still run on the raw text
        log.warning("task understanding failed (%s); using the raw request only", e)
        return Task(raw_request=raw, search_queries=[raw[:80]])
    task = Task(
        raw_request=raw,
        purpose=_s(data.get("purpose")),
        desired_qualities=_list(data.get("desired_qualities"), 8),
        avoid=_list(data.get("avoid"), 6),
        constraints=_list(data.get("constraints"), 6),
        needed_reference_types=_list(data.get("needed_reference_types"), 6),
        search_queries=_list(data.get("search_queries"), 6),
        language=_s(data.get("language")),
    )
    if not task.search_queries:
        task.search_queries = [raw[:80]]
    return task


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v)
    return str(v).strip() or None


def _list(v, limit: int) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [p for p in (x.strip() for x in v.replace("，", ",").split(",")) if p]
    out: list[str] = []
    for x in v:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out[:limit]
