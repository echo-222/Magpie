"""Step 0 of the retrieval agent: what kind of input is this?

The user should not have to pick "keyword search" vs "natural-language request" — a thin intent
layer looks at the raw input and routes it:

  keyword  -> instant hybrid search on the literal text (embeddings + FTS), no model call
  request  -> the full agent (understand -> recall -> judge [-> compose])

plus a *time preference* both routes honour (sort newest/oldest, "saved recently" window).

Cheap first: a rule-based classifier settles the clear cases (a 2-word tag vs. a sentence that
says 我想 / 帮我找 …).  Only the ambiguous middle goes to a tiny LLM call (≈1-3 s) and even that
falls back to a length rule when the model is down.  Everything returns an `Intent` with a
`reason` so the UI can say what it decided and let the user flip it with one click.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass

from .llm import LLM
from .task import time_facets_from_text

log = logging.getLogger(__name__)

# words that only appear when someone is *asking* for something, not naming it
_REQUEST_WORDS = re.compile(
    r"我想|我要|我在|我们|帮我|请|找到|找一|找些|找出|找找|找点|找个|给我|需要|想做|要做|做一个|做个|设计一个|"
    r"适合|类似|像这样|参考|灵感|风格的|能不能|有没有|哪些|什么样|怎么|如何|推荐|建议|收集的|存的|收的|"
    r"\b(i want|i need|i'm|i am|help me|find me|show me|looking for|give me|something|references? for|"
    r"ideas? for|suggest|recommend|what|which|how)\b",
    re.IGNORECASE,
)
_CLAUSE_PUNCT = re.compile(r"[，。！？；、,.!?;]")
_TAG_SEPARATORS = re.compile(r"[/|#]|\s{2,}")
_CJK = re.compile(r"[\u3400-\u9fff]")


@dataclass
class Intent:
    mode: str  # "keyword" | "request"
    sort: str  # "relevance" | "newest" | "oldest"
    within_days: int | None
    confidence: float  # 0..1
    reason: str
    source: str  # "rules" | "model" | "fallback"

    def to_dict(self) -> dict:
        return asdict(self)


def _measure(text: str) -> dict:
    cjk = len(_CJK.findall(text))
    latin_words = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))
    return {
        "cjk": cjk,
        "latin_words": latin_words,
        "length": cjk + latin_words,  # rough "units of meaning"
        "clauses": len(_CLAUSE_PUNCT.findall(text.rstrip("。.!！?？"))) + 1,
        "request_words": len(_REQUEST_WORDS.findall(text)),
        "tags": len([t for t in _TAG_SEPARATORS.split(text) if t.strip()]) if _TAG_SEPARATORS.search(text) else 0,
    }


def classify_by_rules(text: str) -> Intent | None:
    """Clear cases only; returns None when the input sits in the ambiguous middle."""
    t = text.strip()
    m = _measure(t)
    tf = time_facets_from_text(t)
    sort = tf.get("order") or "relevance"
    within = tf.get("within_days")

    if m["request_words"] >= 1 and (m["length"] >= 6 or m["clauses"] >= 2):
        return Intent("request", sort, within, 0.9, "句子里有明确的需求表达（我想/帮我/找…）", "rules")
    if m["clauses"] >= 2 and m["length"] >= 10:
        return Intent("request", sort, within, 0.8, "多个分句的完整描述", "rules")
    if m["cjk"] >= 16 or m["latin_words"] >= 9:
        return Intent("request", sort, within, 0.75, "较长的自然语言描述", "rules")
    if m["tags"] >= 2:
        return Intent("keyword", sort, within, 0.9, "用分隔符列出的关键词", "rules")
    if m["request_words"] == 0 and (m["cjk"] <= 6 and m["latin_words"] <= 3) and m["clauses"] == 1:
        return Intent("keyword", sort, within, 0.85, "短词，直接按关键词匹配", "rules")
    return None


INTENT_SYSTEM = (
    "You route search-box input for Magpie, a designer's personal material library. Decide whether the input is "
    "a KEYWORD lookup (a tag, a name, a material/style word, something to match literally) or a REQUEST (a "
    "natural-language description of what the person is trying to make or find, which needs understanding and "
    "judgement). Also read any time preference: 'newest'/'oldest' ordering, or 'within_days' when they only want "
    "recently saved things. Return JSON only: {\"mode\": \"keyword\"|\"request\", \"sort\": \"relevance\"|\"newest\"|"
    "\"oldest\", \"within_days\": integer|null, \"reason\": \"<=12 words in the input's language\"}."
)


def classify_intent(text: str, llm: LLM | None = None) -> Intent:
    """Rules first; the ambiguous middle goes to the model; a length rule is the last resort."""
    t = text.strip()
    by_rules = classify_by_rules(t)
    if by_rules is not None:
        return by_rules
    tf = time_facets_from_text(t)
    if llm is not None:
        try:
            data = llm.chat_json(INTENT_SYSTEM, f'Input: """{t}"""', purpose="intent", temperature=0.0, max_tokens=120)
            mode = "request" if str(data.get("mode", "")).lower().startswith("req") else "keyword"
            sort = str(data.get("sort") or tf.get("order") or "relevance").lower()
            if sort not in ("newest", "oldest"):
                sort = "relevance"
            wd = data.get("within_days")
            try:
                wd = int(wd) if wd not in (None, "", "null") else tf.get("within_days")
            except (TypeError, ValueError):
                wd = tf.get("within_days")
            return Intent(mode, sort, wd if wd and wd > 0 else None, 0.7, str(data.get("reason") or "模型判断"), "model")
        except Exception as e:  # noqa: BLE001
            log.warning("intent model failed (%s); using length rule", e)
    m = _measure(t)
    mode = "request" if m["length"] >= 10 or m["clauses"] >= 2 else "keyword"
    return Intent(mode, tf.get("order") or "relevance", tf.get("within_days"), 0.55, "按长度判断", "fallback")
