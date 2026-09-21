"""The retrieval agent: abstract request -> ranked, explained references -> Material Pack.

Runtime (each step is a plain function so it can be traced, tested and swapped):

  1. UNDERSTAND  (LLM, task.py)  request -> Task brief + facets (colour families, modality)
  2. RECALL      (no LLM)        hybrid vectors + FTS over the expanded queries, wide (k≈40),
                                 fused with *facts* about each material: palette hue-family ratios
                                 for colour facets, modality filter.  Recall proposes, never decides.
  3. JUDGE       (LLM)           every candidate gets a 0-3 relevance to THIS request, the aspect
                                 it serves and a one-line verdict.  This is the ranking the user sees.
  4. COMPOSE     (LLM)           relevant items -> 2-5 task-specific groups with roles + reasons,
                                 a direction paragraph for another agent, and the honest "gaps".
  5. VERIFY      (no LLM)        schema / id validation, dedupe, fallbacks; a trace is stored on the
                                 pack so the UI can show what happened at each step.

Prompts live next to the step that uses them.  See docs/RETRIEVAL_AGENT.md for the design.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .db import Database
from .llm import LLM
from .models import Candidate, Material, MaterialPack, PackGroup, PackMember, Task
from .recompose import describe_candidate, plan_schema_problems
from .retrieval import Hit, Retriever
from .task import understand_task

log = logging.getLogger(__name__)

RECALL_K = 40  # how many candidates recall hands to the judge (upper bound)
JUDGE_BATCH = 24  # candidates per judge call (local 8B models lose the schema past ~4k tokens)
W_COLOR_MUST = 0.45  # fusion weight of the colour-fact score when the request IS about a colour
W_COLOR_PREFER = 0.2
COLOR_MUST_PENALTY = 0.25  # images with no trace of the requested colour drop back by this much
W_JUDGE = 0.7  # final rank = W_JUDGE * relevance/3 + (1-W_JUDGE) * normalised recall score

# Selection discipline (integration acceptance P1-b): a broad request must not turn the whole
# library into a pack.  Relevance 3 is scarce by definition, a pack is a selection, and the
# "also relevant" safety net stays small.
TOP_SHARE = 0.25  # at most this share of judged candidates may keep relevance 3 ...
TOP_MIN = 3  # ... but never fewer than this many when the judge gave that many
PACK_MAX_MEMBERS = 12  # hard cap on members in a generated pack (humans can add more)
STRAGGLER_MAX = 4  # judged >=2 but not grouped by the composer: at most this many are surfaced
VAGUE_DIRECTION_NOTE = "用户没有给出具体偏好；以下素材按库里的整体方向挑选，不代表用户本次的要求。"


# --------------------------------------------------------------------------- step 2: recall


def color_fact_score(m: Material, colors: dict) -> float | None:
    """How much of the image's sampled pixels fall in the requested hue families (0..1).
    Uses the palette facts computed at ingest (objective_metadata.palette.hue_families), so it is
    model-free and deterministic.  None for materials without a palette (text)."""
    pal = m.objective_metadata.get("palette") or {}
    fams: dict[str, float] = pal.get("hue_families") or {}
    if not fams:
        if m.analysis.colors:  # very old materials: fall back to the swatches
            fams = {}
            for c in m.analysis.colors:
                if c.name:
                    fams[c.name] = fams.get(c.name, 0.0) + c.ratio
        else:
            return None
    primary = set(colors.get("primary") or [])
    adjacent = set(colors.get("adjacent") or [])

    def hit(name: str, targets: set[str]) -> bool:
        base = name.replace("muted ", "")
        return base in targets or name in targets

    score = adj = 0.0
    for name, ratio in fams.items():
        if hit(name, primary):
            score += ratio * (0.7 if name.startswith("muted ") else 1.0)
        elif hit(name, adjacent):
            adj += ratio * 0.35
    # neighbouring hues support the palette but can never make an image "red" on their own
    # (an all-orange picture tops out at 0.5)
    score += min(adj, 0.15)
    # saturation / lightness wishes, when stated
    desc = pal.get("descriptors") or []
    if colors.get("saturation") == "high" and "low saturation" in desc:
        score *= 0.6
    if colors.get("saturation") == "low" and "high saturation" in desc:
        score *= 0.6
    if colors.get("lightness") == "dark" and "light" in desc:
        score *= 0.7
    if colors.get("lightness") == "light" and "dark" in desc:
        score *= 0.7
    # 30% of pixels in the requested family already reads as "that colour"
    return round(min(1.0, score / 0.30), 4)


def time_since(facets: dict) -> str | None:
    """ISO timestamp lower bound for a `time.within_days` facet (entry time = Material.created_at)."""
    days = (facets.get("time") or {}).get("within_days")
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(microsecond=0).isoformat()


def order_hits(items: list, order: str | None, key=lambda x: x.created_at):
    """Re-order a ranked list by entry time when the user asked for it (newest/oldest); relevance otherwise.
    Time is bucketed by day and the sort is stable, so within one day the incoming relevance order
    survives — "最近存的优先" should not scramble a batch imported minutes apart."""
    if order not in ("newest", "oldest"):
        return items
    return sorted(items, key=lambda x: (key(x) or "")[:10], reverse=order == "newest")


def recall(retriever: Retriever, task: Task, k: int = RECALL_K) -> list[Hit]:
    """Wide hybrid recall, then fuse in facts the request pinned down (facets)."""
    queries = [task.raw_request] + task.search_queries
    hits = retriever.search(queries, limit=max(k * 2, 60))
    facets = task.facets or {}
    modality = facets.get("modality", "any")
    colors = facets.get("colors")
    since = time_since(facets)

    out: list[Hit] = []
    for h in hits:
        m = h.material
        if m is None:
            continue
        if modality in ("image", "text") and m.modality != modality:
            continue
        if since and (m.created_at or "") < since:
            continue
        h.signals["semantic"] = round(h.score, 4)
        if colors:
            cs = color_fact_score(m, colors)
            h.signals["color"] = cs
            if cs is not None:
                w = W_COLOR_MUST if colors.get("weight") == "must" else W_COLOR_PREFER
                h.score += w * cs
                if colors.get("weight") == "must" and cs == 0.0:
                    h.score -= COLOR_MUST_PENALTY
        out.append(h)
    out.sort(key=lambda h: -h.score)
    return out[:k]


# --------------------------------------------------------------------------- step 3: judge

JUDGE_SYSTEM = """You are the relevance judge inside Magpie, a designer's personal material-memory agent. The designer typed an abstract request; a recall step already pulled candidate materials from their own library. Your job is to decide, for EACH candidate, how useful it is for THIS request, so the results can be ranked and the weak ones dropped.

Each candidate card shows: id, modality, title, the designer's own Human Thought (why it caught them), machine analysis (what it is, tags, palette), any text, and recall signals (semantic similarity; "color" = share of pixels in the requested hue family, 0-1, when the request is about a colour).

Scoring scale (be strict, the library is small and the designer will see this order):
  3 = directly what was asked for; could be shown as a primary reference
  2 = clearly useful for the request in a secondary way (a supporting texture, a layout idea, a counter-example the request implies)
  1 = weak / tangential: only a small detail relates, or it relates to the general topic but not to what was asked
  0 = not relevant to this request (leave it out)
Expected distribution: 3 is scarce — no more than about a quarter of the candidates, even when the whole library shares the request's mood. When many candidates fit equally well, rank them: only the ones a designer would open first get 3, the rest are 2, the merely on-topic are 1. Over-scoring is a failure, not generosity.
Rules:
- Judge against the request, not against general quality. A beautiful material that does not serve the request is a 0 or 1.
- Trust facts over vibes: when the request is about a colour, the "color" signal and the palette line are evidence; a material with color=0 cannot be a 3 for that colour unless its text/OCR clearly mentions it (e.g. red seals on a page).
- Use the Human Thought: if the designer saved something for the very reason the request needs, that raises it. But a Human Thought is a note from when the material was saved, not a constraint of this request — never treat it as something the designer asked for or wants to avoid now.
- "aspect" names the part of the request the material serves, in 2-5 words, phrased in terms of THIS request (a colour name only when the request is about that colour).
- "why" is one short sentence in the request's language.
Return JSON only: {"judgments": [{"material_id": "...", "relevance": 0-3, "aspect": "...", "why": "..."}]} covering EVERY candidate id exactly once. Copy ids verbatim."""

JUDGE_USER = """REQUEST
raw: {raw}
purpose: {purpose}
desired qualities: {desired}
avoid: {avoid}
facets: {facets}

CANDIDATES ({n})
{candidates}

Return ONLY the JSON object with one judgment per candidate id ({n} items)."""


@dataclass
class Judged:
    hit: Hit
    relevance: int
    aspect: str | None
    why: str | None
    final: float = 0.0

    @property
    def material(self) -> Material:
        return self.hit.material  # type: ignore[return-value]


def judge(llm: LLM, task: Task, hits: list[Hit]) -> tuple[list[Judged], dict]:
    """LLM relevance judging in batches; returns items sorted by fused final score plus a trace."""
    if not hits:
        return [], {"batches": 0, "judged": 0, "fallback": False}
    top = max(h.score for h in hits) or 1.0
    low = min(h.score for h in hits)
    span = (top - low) or 1.0
    verdicts: dict[str, dict] = {}
    fallback = False
    batches = 0
    for i in range(0, len(hits), JUDGE_BATCH):
        batch = hits[i : i + JUDGE_BATCH]
        batches += 1
        user = JUDGE_USER.format(
            raw=task.raw_request,
            purpose=task.purpose or "-",
            desired=", ".join(task.desired_qualities) or "-",
            avoid=", ".join(task.avoid) or "-",
            facets=_facets_line(task),
            n=len(batch),
            candidates="\n".join(_judge_card(h) for h in batch),
        )
        try:
            data = llm.chat_json(JUDGE_SYSTEM, user, purpose="judge", temperature=0.0, max_tokens=2400)
            for j in data.get("judgments") or []:
                if not isinstance(j, dict):
                    continue
                mid = str(j.get("material_id", "")).strip()
                if mid in {h.material_id for h in batch}:
                    try:
                        rel = max(0, min(3, int(j.get("relevance", 0))))
                    except (TypeError, ValueError):
                        rel = 0
                    verdicts[mid] = {"relevance": rel, "aspect": _s(j.get("aspect")), "why": _s(j.get("why"))}
        except Exception as e:  # noqa: BLE001 - keep going with recall order
            log.warning("judge step failed (%s); ranking by recall only", e)
            fallback = True

    out: list[Judged] = []
    for h in hits:
        v = verdicts.get(h.material_id)
        recall_norm = (h.score - low) / span
        if v is None:
            # unjudged (model skipped it / call failed): recall order decides, marked as relevance 1
            rel = 1 if fallback or recall_norm > 0.5 else 0
            j = Judged(h, rel, None, None)
        else:
            j = Judged(h, v["relevance"], v["aspect"], v["why"])
        j.final = round(W_JUDGE * (j.relevance / 3) + (1 - W_JUDGE) * recall_norm, 4)
        out.append(j)
    capped = cap_top_relevance(out)
    out.sort(key=lambda j: (-j.relevance, -j.final))
    return out, {"batches": batches, "judged": len(verdicts), "fallback": fallback, "capped": capped}


def cap_top_relevance(judged: list[Judged], share: float = TOP_SHARE, minimum: int = TOP_MIN) -> int:
    """Relevance 3 is scarce: keep at most `share` of the candidates (never fewer than `minimum`)
    at 3, demoting the weakest 3s (by fused score) to 2.  Returns how many were demoted.
    Deterministic guard against judge inflation on broad requests (acceptance P1-b)."""
    threes = sorted((j for j in judged if j.relevance == 3), key=lambda j: -j.final)
    allowed = max(minimum, math.ceil(len(judged) * share))
    demoted = 0
    for j in threes[allowed:]:
        j.relevance = 2
        j.final = round(W_JUDGE * (2 / 3) + (j.final - W_JUDGE), 4)  # same recall component, lower judge component
        demoted += 1
    return demoted


def _judge_card(h: Hit) -> str:
    card = describe_candidate(h.material, None)  # type: ignore[arg-type]
    sig = f"  recall: semantic {h.signals.get('semantic', h.score):.2f}"
    if h.signals.get("color") is not None:
        sig += f", color {h.signals['color']:.2f}"
    if h.signals.get("fts"):
        sig += ", exact keyword hit"
    return card + "\n" + sig


def _facets_line(task: Task) -> str:
    f = task.facets or {}
    bits = []
    c = f.get("colors")
    if c:
        bits.append(
            f"colour {'/'.join(c.get('primary', []))} ({c.get('weight')}; adjacent {', '.join(c.get('adjacent', [])) or '-'}; "
            f"saturation {c.get('saturation')}, lightness {c.get('lightness')})"
        )
    if f.get("modality") in ("image", "text"):
        bits.append(f"modality {f['modality']} only")
    t = f.get("time") or {}
    if t.get("within_days"):
        bits.append(f"saved within the last {t['within_days']} days (already filtered)")
    if t.get("order"):
        bits.append(f"user wants results ordered by time ({t['order']} first)")
    return "; ".join(bits) or "-"


# --------------------------------------------------------------------------- step 4: compose

COMPOSE_SYSTEM = """You are the composer inside Magpie, a designer's personal material-memory agent. A judge already scored every candidate for THIS request (relevance 3 = primary, 2 = supporting, 1 = weak) and named the aspect each one serves. Turn the relevant ones into a Material Pack: task-specific groups a designer would actually browse, with a role and a reason per material.
You MUST answer with one JSON object with EXACTLY these top-level keys: "human_direction" (string), "groups" (array), "excluded" (array), "gaps" (array of strings).
Each group: {"name": string, "purpose": string, "members": [{"material_id": string, "role": string, "reason": string}]}.
Each excluded item: {"material_id": string, "reason": string}.
Rules:
- Groups follow the REQUEST, not a fixed taxonomy. For a colour request think 主色参考 / 配色对比 / 邻近暖色与背景 / 材质与质感 / 反例; for a layout request think 版式结构 / 字体 / 留白 …  2-5 groups, 1-4 members each, each material at most once. No generic buckets like "Images"/"Texts".
- A pack is a selection, not the library: at most {max_members} members in total, fewer is better. Include the relevance-3 items first, then the relevance-2 items that add something different; everything else goes to "excluded" with a short honest reason (e.g. "同类里已有更直接的").
- Order groups from most to least central to the request; order members inside a group by relevance (3 first).
- Relevance-1 items: include only when they add something the stronger ones lack (say what), otherwise exclude them.
- Each "reason" (1-2 sentences, request language) says why this material helps THIS request now, and builds on the designer's Human Thought when relevant. Do not restate the summary.
- "human_direction": 2-3 sentences for another AI agent: what to go for, what to avoid — only what the request says or clearly implies. If the REQUEST block lists no desired qualities and nothing to avoid, the direction may ONLY restate the request and say that the picks follow the library's overall direction; do not add preferences, do not tell the agent to avoid anything.
- "gaps": 0-3 short sentences on what the library does NOT have for THIS request — derive them from the request's own needed reference types and facets versus what the candidates cover. Be concrete; [] if coverage is fine. Never mention a colour, style or topic the request did not ask for.
- The REQUEST block below is the only source of what the designer wants now. The Human Thoughts on the cards are notes written when each material was saved (possibly for other projects); use them to explain why a material helps, never as constraints of this request, and never claim "the designer said/wants to avoid X" unless X is in the REQUEST block. This pack is built from scratch: no earlier requests or packs exist.
material_id values MUST be copied verbatim from the candidate list. Never invent ids. Write in the request's language."""

COMPOSE_USER = """REQUEST
raw: {raw}
purpose: {purpose}
desired qualities: {desired}
avoid: {avoid}
constraints: {constraints}
facets: {facets}
{vague_note}
JUDGED CANDIDATES ({n}; relevance, aspect, judge verdict, then the card)
{candidates}

Now return ONLY the JSON object. Skeleton to fill (keep these exact keys; replace the values; use only candidate ids):
{{"human_direction": "...", "groups": [{{"name": "...", "purpose": "...", "members": [{{"material_id": "{ex1}", "role": "...", "reason": "..."}}]}}], "excluded": [{{"material_id": "{ex2}", "reason": "..."}}], "gaps": ["..."]}}"""


def trim_to_cap(groups: list[PackGroup], cap: int) -> list[str]:
    """Keep at most `cap` members across all groups, dropping the weakest (lowest relevance, then
    fused score) from the least central groups first; drops empty groups.  Returns dropped ids."""
    total = sum(len(g.members) for g in groups)
    dropped: list[str] = []
    while total > cap:
        # candidates for removal: the last member of each group (members are sorted by relevance desc)
        victims = [(g, g.members[-1]) for g in groups if g.members]
        g, m = min(victims, key=lambda gm: (gm[1].relevance or 0, gm[1].score or 0.0, -groups.index(gm[0])))
        g.members.remove(m)
        dropped.append(m.material_id)
        total -= 1
    groups[:] = [g for g in groups if g.members]
    return dropped


def is_vague(task: Task) -> bool:
    """A request that names nothing to go for or avoid: the brief has no qualities, no avoid list,
    no constraints.  Such packs must not invent preferences (acceptance P1-a)."""
    return not (task.desired_qualities or task.avoid or task.constraints)


def vague_direction(task: Task) -> str:
    base = (task.purpose or task.raw_request).strip().rstrip("。.")
    return f"{base}。{VAGUE_DIRECTION_NOTE}"


def compose(llm: LLM, task: Task, judged: list[Judged]) -> tuple[dict, bool]:
    """Groups + roles + reasons + gaps from the judged list. Returns (plan, used_fallback)."""
    relevant = [j for j in judged if j.relevance >= 1]
    if not relevant:
        return {"human_direction": task.purpose, "groups": [], "excluded": [], "gaps": ["库里没有和这个请求相关的素材。"]}, True
    cards = "\n".join(
        f"[{j.hit.material_id}] relevance {j.relevance} | aspect: {j.aspect or '-'} | judge: {j.why or '-'}\n"
        + describe_candidate(j.material, None)
        for j in relevant
    )
    vague_note = (
        "NOTE: the request names nothing to go for or avoid. Keep human_direction to a restatement of the request; "
        "do not add any preference, style or avoid-list derived from the Human Thoughts.\n"
        if is_vague(task)
        else ""
    )
    user = COMPOSE_USER.format(
        raw=task.raw_request,
        purpose=task.purpose or "-",
        desired=", ".join(task.desired_qualities) or "-",
        avoid=", ".join(task.avoid) or "-",
        constraints=", ".join(task.constraints) or "-",
        facets=_facets_line(task),
        vague_note=vague_note,
        n=len(relevant),
        candidates=cards,
        ex1=relevant[0].hit.material_id,
        ex2=relevant[-1].hit.material_id,
    )
    system = COMPOSE_SYSTEM.replace("{max_members}", str(PACK_MAX_MEMBERS))
    valid_ids = {j.hit.material_id for j in relevant}
    prompt = user
    for attempt in range(2):
        try:
            plan = llm.chat_json(system, prompt, purpose="recompose", temperature=0.1, max_tokens=2400)
        except Exception as e:  # noqa: BLE001
            log.warning("compose call failed (%s); grouping by judged aspect", e)
            break
        problems = plan_schema_problems(plan, valid_ids)
        if not problems:
            plan["_attempts"] = attempt + 1
            return plan, False
        log.warning("compose reply off-schema (attempt %d): %s", attempt + 1, "; ".join(problems))
        prompt = (
            user
            + "\n\nYour previous answer was rejected: "
            + "; ".join(problems)
            + f".\nAnswer again with EXACTLY the keys human_direction / groups / excluded / gaps and only these material_id values: {sorted(valid_ids)}"
        )
    # verified fallback: group by the judge's aspect so the user still gets a sensible pack
    by_aspect: dict[str, list[Judged]] = {}
    for j in relevant:
        by_aspect.setdefault(j.aspect or "相关素材", []).append(j)
    return {
        "human_direction": task.purpose,
        "groups": [
            {
                "name": aspect,
                "purpose": "按判定的作用分组（组合步骤不可用）",
                "members": [{"material_id": j.hit.material_id, "role": j.aspect, "reason": j.why or f"relevance {j.relevance}"} for j in items],
            }
            for aspect, items in by_aspect.items()
        ],
        "excluded": [],
        "gaps": [],
    }, True


# --------------------------------------------------------------------------- orchestration


@dataclass
class AgentResult:
    task: Task
    judged: list[Judged]
    trace: dict = field(default_factory=dict)


@dataclass
class RetrievalAgent:
    db: Database
    retriever: Retriever
    llm: LLM

    def find(self, request: str, limit: int = RECALL_K) -> AgentResult:
        """Steps 1-3: natural-language request -> judged, ranked references (no pack)."""
        t0 = time.time()
        task = understand_task(self.llm, request)
        t1 = time.time()
        hits = recall(self.retriever, task, k=limit)
        t2 = time.time()
        judged, jtrace = judge(self.llm, task, hits)
        t3 = time.time()
        trace = {
            "steps": [
                {"step": "understand", "s": round(t1 - t0, 1), "facets": task.facets, "queries": [task.raw_request] + task.search_queries},
                {"step": "recall", "s": round(t2 - t1, 1), "candidates": len(hits), "k": limit, "since": time_since(task.facets or {})},
                {
                    "step": "judge",
                    "s": round(t3 - t2, 1),
                    **jtrace,
                    "kept": sum(1 for j in judged if j.relevance >= 2),  # what "相关" means to the user: primary + supporting
                    "weak": sum(1 for j in judged if j.relevance == 1),
                },
            ]
        }
        return AgentResult(task=task, judged=judged, trace=trace)

    def build_pack(self, request: str, limit: int = RECALL_K) -> MaterialPack:
        """Steps 1-5: request -> saved Material Pack with groups, reasons, gaps and a trace."""
        res = self.find(request, limit=limit)
        task, judged = res.task, res.judged
        t3 = time.time()
        plan, fallback = compose(self.llm, task, judged)
        t4 = time.time()

        by_id = {j.hit.material_id: j for j in judged}
        groups: list[PackGroup] = []
        used: set[str] = set()
        for g in plan.get("groups", []):
            members: list[PackMember] = []
            for mem in g.get("members", []):
                mid = str(mem.get("material_id", "")).strip()
                if mid not in by_id or mid in used or by_id[mid].relevance == 0:
                    continue
                used.add(mid)
                j = by_id[mid]
                members.append(
                    PackMember(
                        material_id=mid,
                        role=_s(mem.get("role")) or j.aspect,
                        reason=_s(mem.get("reason")) or j.why,
                        score=round(j.final, 4),
                        relevance=j.relevance,
                    )
                )
            if members:
                members.sort(key=lambda m: -(m.relevance or 0))
                groups.append(PackGroup(name=_s(g.get("name")) or "Relevant", purpose=_s(g.get("purpose")), members=members))
        # verify 1: a pack is a selection — enforce the member cap the composer was asked to respect
        excluded_ids: set[str] = set()
        excluded: list[dict[str, str]] = []
        trimmed = trim_to_cap(groups, PACK_MAX_MEMBERS)
        for mid in trimmed:
            used.discard(mid)
            excluded.append({"material_id": mid, "reason": f"精选上限 {PACK_MAX_MEMBERS} 条，未入包（{by_id[mid].why or '判定相关'}）"})
            excluded_ids.add(mid)
        # verify 2: anything judged 2-3 that the composer dropped is surfaced, not silently lost —
        # but only a few, and only while there is room under the cap
        room = max(0, PACK_MAX_MEMBERS - sum(len(g.members) for g in groups))
        stragglers = sorted(
            (j for j in judged if j.relevance >= 2 and j.hit.material_id not in used and j.hit.material_id not in excluded_ids),
            key=lambda j: (-j.relevance, -j.final),
        )
        shown, overflow = stragglers[: min(STRAGGLER_MAX, room)], stragglers[min(STRAGGLER_MAX, room) :]
        if shown:
            groups.append(
                PackGroup(
                    name="其他相关" if (task.language or "zh").startswith("zh") else "Also relevant",
                    purpose="判定相关但未被分组的素材",
                    members=[
                        PackMember(material_id=j.hit.material_id, role=j.aspect, reason=j.why, score=round(j.final, 4), relevance=j.relevance)
                        for j in shown
                    ],
                )
            )
            used.update(j.hit.material_id for j in shown)
        for j in overflow:
            excluded.append({"material_id": j.hit.material_id, "reason": f"判定相关（{j.relevance}）但超出精选上限，未入包：{j.why or ''}".rstrip("：")})
            excluded_ids.add(j.hit.material_id)
        for e in plan.get("excluded", []):
            mid = str(e.get("material_id", ""))
            if mid in by_id and mid not in used and mid not in excluded_ids:
                excluded.append({"material_id": mid, "reason": _s(e.get("reason")) or by_id[mid].why or ""})
                excluded_ids.add(mid)
        for j in judged:  # judged-out items are excluded too, with the judge's verdict
            if j.hit.material_id not in used and j.hit.material_id not in excluded_ids and j.relevance >= 1:
                excluded.append({"material_id": j.hit.material_id, "reason": j.why or f"relevance {j.relevance}"})
                excluded_ids.add(j.hit.material_id)

        gaps = [s for s in (_s(x) for x in (plan.get("gaps") or []) if isinstance(x, (str, int, float))) if s][:3]
        # verify 3: a vague request gets a direction that restates it — Human Thoughts are history, not the brief
        model_direction = _s(plan.get("human_direction"))
        direction = vague_direction(task) if is_vague(task) else model_direction
        name = (task.purpose or task.raw_request).strip()[:80]
        trace = dict(res.trace)
        trace["steps"] = trace["steps"] + [
            {
                "step": "compose",
                "s": round(t4 - t3, 1),
                "fallback": fallback,
                "attempts": plan.get("_attempts", 0),
                "groups": len(groups),
                "trimmed": len(trimmed),
                "stragglers_shown": len(shown),
                "stragglers_excluded": len(overflow),
                "vague": is_vague(task),
                **({"model_direction": model_direction} if is_vague(task) and model_direction else {}),
            }
        ]
        pack = MaterialPack(
            name=name,
            task=task,
            groups=groups,
            human_direction=direction,
            candidates=[
                Candidate(
                    material_id=j.hit.material_id,
                    score=round(j.final, 4),
                    via=j.hit.via,
                    relevance=j.relevance,
                    why=j.why,
                    signals={k: v for k, v in j.hit.signals.items() if k in ("semantic", "color", "fts")},
                )
                for j in judged
            ],
            excluded=excluded,
            gaps=gaps,
            generation={
                "pipeline": "agent-v2",
                "chat_provider": getattr(self.llm, "chat_provider", None),
                "chat_model": self.llm.chat_model,
                "chat_model_used": getattr(self.llm, "last_chat_model", None) or self.llm.chat_model,
                "embed_model": self.llm.embed_model,
                "fallback": fallback,
                "plan_attempts": plan.get("_attempts", 0),
                "task_understood": task.purpose is not None,
                "timing_s": {s["step"]: s["s"] for s in trace["steps"]},
                "queries": [task.raw_request] + task.search_queries,
                "trace": trace,
            },
        )
        return self.db.save_pack(pack)


def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
