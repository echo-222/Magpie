"""Task understanding (spec §3.2): a real task -> small brief that drives retrieval + recomposition.

Deliberately tiny ontology. The one Magpie-specific trick is `search_queries`:
the task is rephrased into what good *reference material* would look/feel like,
because a library holds materials, not tasks.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from .llm import LLM
from .models import Task

log = logging.getLogger(__name__)

COLOR_FAMILIES = ("red", "orange", "yellow", "green", "teal", "blue", "purple", "pink", "brown", "black", "white", "grey", "warm neutral")

# Agent step 1 — UNDERSTAND. The request is often abstract ("做一个红色系的页面"); the brief must
# (a) stay faithful, (b) rephrase it into what good *reference material* looks like, and (c) pull out
# facets that can be checked against stored facts (palette hue families, modality).
TASK_SYSTEM = (
    "You are the first step of Magpie, a personal material-memory agent for a designer. You turn an abstract, "
    "natural-language request into a small structured brief. The brief drives three later steps: recall from the "
    "designer's own library (images, textures, posters, objects, text clippings — all saved earlier with a personal "
    "'Human Thought'), relevance judging, and composing a Material Pack.\n"
    "Principles:\n"
    "- Be faithful. Restate what is asked; do NOT invent qualities, constraints or things to avoid that are not stated "
    "or clearly implied. Leave lists empty rather than guess.\n"
    "- The library holds materials, not tasks: search_queries must describe what a useful reference LOOKS or FEELS like.\n"
    "- Facets are hard facts we can check: colour families from a fixed vocabulary, whether the person wants images, "
    "text, or either, and time (when the material was saved into the library). Only fill a facet when the request "
    "clearly asks for it.\n"
    "Return JSON only."
)

TASK_USER = """Request (verbatim):
\"\"\"{task}\"\"\"
Today is {today}.

Return ONLY JSON, written in the request's own language (English keys; values in the request's language unless noted):
{{
  "purpose": "one sentence: what is being made, for what",
  "desired_qualities": ["0-6 short qualities the result should have — only those stated or clearly implied"],
  "avoid": ["things explicitly or clearly implied to avoid; [] if none"],
  "constraints": ["hard constraints (medium, platform, audience, format); [] if none"],
  "needed_reference_types": ["2-5 kinds of reference that would help, e.g. 材质/质感, 色彩, 排版/字体, 氛围, 文案语气, 版式结构, 反例"],
  "search_queries": ["4-6 short retrieval phrases describing what a useful reference LOOKS or FEELS like (mix Chinese and English design terms); each targets a different reference type; include literal colour words when colour matters"],
  "facets": {{
    "colors": {{
      "primary": ["colour families the request is about, from: {families}; [] if colour is not part of the request"],
      "adjacent": ["neighbouring families that would still read as the same palette, e.g. red -> pink, orange, brown; [] if none"],
      "saturation": "any | high | low",
      "lightness": "any | dark | light",
      "weight": "must (the request IS about this colour) | prefer (colour is one wish among others)"
    }},
    "modality": "image | text | any  — 'image' when the person asks for 参考图/pictures/visual references, 'text' for copy/quotes/wording, else 'any'",
    "time": {{
      "within_days": "integer or null — only materials SAVED within the last N days (今天=1, 最近/这几天/这周=7, 这个月=30, 今年=365); null when the request does not limit time",
      "order": "newest | oldest | null — only when the person asks for results in time order (最新的/最近存的 -> newest, 最早的/最老的 -> oldest)"
    }}
  }},
  "language": "zh or en"
}}"""


def understand_task(llm: LLM, raw_request: str) -> Task:
    raw = raw_request.strip()
    try:
        data = llm.chat_json(
            TASK_SYSTEM,
            TASK_USER.format(task=raw, families=", ".join(COLOR_FAMILIES), today=date.today().isoformat()),
            purpose="task",
        )
    except Exception as e:  # noqa: BLE001 - keep the loop alive: retrieval can still run on the raw text
        log.warning("task understanding failed (%s); using the raw request only", e)
        return Task(raw_request=raw, search_queries=[raw[:80]], facets=_facets_from_text(raw))
    task = Task(
        raw_request=raw,
        purpose=_s(data.get("purpose")),
        desired_qualities=_list(data.get("desired_qualities"), 8),
        avoid=_list(data.get("avoid"), 6),
        constraints=_list(data.get("constraints"), 6),
        needed_reference_types=_list(data.get("needed_reference_types"), 6),
        search_queries=_list(data.get("search_queries"), 6),
        language=_s(data.get("language")),
        facets=_clean_facets(data.get("facets")) or _facets_from_text(raw),
    )
    if not task.search_queries:
        task.search_queries = [raw[:80]]
    if "time" not in task.facets:  # time words are literal enough to trust the regex when the model skipped them
        t = time_facets_from_text(raw)
        if t:
            task.facets["time"] = t
    return task


# --------------------------------------------------------------------------- facets

_COLOR_WORDS: dict[str, str] = {
    "红": "red", "朱": "red", "赤": "red", "绯": "red", "red": "red", "crimson": "red", "scarlet": "red",
    "橙": "orange", "橘": "orange", "orange": "orange",
    "黄": "yellow", "yellow": "yellow", "gold": "yellow", "金色": "yellow",
    "绿": "green", "green": "green",
    "青": "teal", "teal": "teal", "cyan": "teal",
    "蓝": "blue", "blue": "blue", "navy": "blue",
    "紫": "purple", "purple": "purple", "violet": "purple",
    "粉": "pink", "pink": "pink", "rose": "pink",
    "棕": "brown", "褐": "brown", "brown": "brown",
    "黑": "black", "black": "black",
    "白": "white", "white": "white",
    "灰": "grey", "grey": "grey", "gray": "grey",
}
_ADJACENT: dict[str, list[str]] = {
    "red": ["pink", "orange", "brown"],
    "orange": ["red", "yellow", "brown", "warm neutral"],
    "yellow": ["orange", "warm neutral"],
    "green": ["teal", "yellow"],
    "teal": ["green", "blue"],
    "blue": ["teal", "purple"],
    "purple": ["blue", "pink"],
    "pink": ["red", "purple"],
    "brown": ["orange", "warm neutral"],
    "black": ["grey"],
    "white": ["grey", "warm neutral"],
    "grey": ["black", "white"],
    "warm neutral": ["brown", "yellow", "white"],
}


def _clean_facets(raw) -> dict:
    """Validate the model's facets against the fixed vocabulary; anything off-list is dropped."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    colors = raw.get("colors")
    if isinstance(colors, dict):
        primary = [c for c in _list(colors.get("primary"), 4) if c in COLOR_FAMILIES]
        if primary:
            adjacent = [c for c in _list(colors.get("adjacent"), 5) if c in COLOR_FAMILIES and c not in primary]
            if not adjacent:
                adjacent = [c for p in primary for c in _ADJACENT.get(p, []) if c not in primary]
            sat = str(colors.get("saturation") or "any").lower()
            lig = str(colors.get("lightness") or "any").lower()
            out["colors"] = {
                "primary": primary,
                "adjacent": list(dict.fromkeys(adjacent)),
                "saturation": sat if sat in ("high", "low") else "any",
                "lightness": lig if lig in ("dark", "light") else "any",
                "weight": "must" if str(colors.get("weight") or "").lower().startswith("must") else "prefer",
            }
    modality = str(raw.get("modality") or "any").lower()
    out["modality"] = modality if modality in ("image", "text") else "any"
    time_f = _clean_time(raw.get("time"))
    if time_f:
        out["time"] = time_f
    return out


def _clean_time(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    wd = raw.get("within_days")
    try:
        wd = int(wd) if wd not in (None, "", "null") else None
    except (TypeError, ValueError):
        wd = None
    if wd and 0 < wd <= 3650:
        out["within_days"] = wd
    order = str(raw.get("order") or "").lower()
    if order in ("newest", "oldest"):
        out["order"] = order
    return out


_TIME_WINDOWS: list[tuple[str, int]] = [
    (r"今天|today", 1),
    (r"昨天|yesterday", 2),
    (r"这几天|最近几天|这周|本周|这一周|上周|近一周|past week|this week|last week", 7),
    (r"这个月|本月|上个月|近一个月|最近一个月|this month|last month", 30),
    (r"最近|近期|新存|刚存|刚收|recent|recently|lately", 14),
    (r"今年|this year|近一年|最近一年", 365),
]
_ORDER_WORDS: list[tuple[str, str]] = [
    (r"最新|最近存的|最近收的|新到旧|倒序|按时间倒|newest|latest|most recent", "newest"),
    (r"最早|最老|最先|旧到新|正序|按时间顺|oldest|earliest", "oldest"),
]


def time_facets_from_text(text: str) -> dict:
    """Literal time words -> {within_days, order}. Used as the no-model fallback and by the intent layer."""
    low = text.lower()
    out: dict = {}
    for pat, days in _TIME_WINDOWS:
        if re.search(pat, low):
            out["within_days"] = days
            break
    for pat, order in _ORDER_WORDS:
        if re.search(pat, low):
            out["order"] = order
            break
    return out


def _facets_from_text(raw: str) -> dict:
    """No-model fallback: literal colour words and 参考图/图片 -> image."""
    low = raw.lower()
    # single CJK characters only count as colour words when used as one ("红色", "红系", "红调"),
    # otherwise 留白 / 黑体 would turn into palette facets
    fams = list(
        dict.fromkeys(
            f
            for w, f in _COLOR_WORDS.items()
            if (w in low if len(w) > 1 or w.isascii() else any(w + suffix in low for suffix in ("色", "系", "调")))
        )
    )
    out: dict = {"modality": "image" if any(k in low for k in ("参考图", "图片", "配图", "image", "picture", "photo")) else "any"}
    if fams:
        out["colors"] = {
            "primary": fams[:2],
            "adjacent": [c for p in fams[:2] for c in _ADJACENT.get(p, []) if c not in fams],
            "saturation": "any",
            "lightness": "any",
            "weight": "must" if any(k in low for k in ("色系", "配色", "色调", "palette", "color", "colour")) else "prefer",
        }
    t = time_facets_from_text(raw)
    if t:
        out["time"] = t
    return out


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
