"""Text material understanding: objective facts from the text itself + a short model summary."""

from __future__ import annotations

import re

from ..llm import LLM

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
URL_RE = re.compile(r"https?://\S+")

TEXT_SYSTEM = (
    "You analyse a short text clipping saved by a designer into a personal material library. "
    "Summarise faithfully; extract retrieval keywords; do not add opinions. Return JSON only. "
    "Write `summary` in the same language as the text (Chinese if the text is Chinese)."
)

TEXT_USER = """Text clipping:
\"\"\"
{text}
\"\"\"

Return ONLY JSON:
{{
  "summary": "1-2 sentence summary",
  "keywords": ["5-8 retrieval keywords (design concepts, materials, names, principles)"],
  "topics": ["1-3 broad topics"],
  "tone": ["1-3 words describing the writing tone, e.g. 克制, manifesto, technical"]
}}"""


def text_metadata(text: str) -> dict:
    cjk = len(CJK_RE.findall(text))
    letters = sum(ch.isalpha() for ch in text)
    lang = "zh" if cjk and cjk / max(1, letters) > 0.3 else "en" if letters else "unknown"
    return {
        "char_count": len(text),
        "word_count": len(re.findall(r"\w+", text)) if lang != "zh" else None,
        "line_count": text.count("\n") + 1,
        "language": lang,
        "urls_in_text": URL_RE.findall(text)[:5],
    }


def analyze_text(llm: LLM, text: str) -> dict:
    data = llm.chat_json(TEXT_SYSTEM, TEXT_USER.format(text=text[:6000]), purpose="text_analysis")
    return {
        "summary": (str(data.get("summary") or "").strip() or None),
        "keywords": _list(data.get("keywords")),
        "topics": _list(data.get("topics"), 4),
        "tone": _list(data.get("tone"), 4),
    }


def _list(v, limit: int = 10) -> list[str]:
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
