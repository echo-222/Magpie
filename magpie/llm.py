"""Small model-provider adapter (spec §0.3: keep provider calls behind a small adapter).

Two implementations:

* OpenAICompatLLM – any OpenAI-compatible endpoint via the official `openai` SDK.
  Works unchanged for Ollama (default, local, no key), OpenAI, DeepSeek, Qwen, ...
* FakeLLM – deterministic offline stub so the whole pipeline can be smoke-tested
  without a model.  Never used for the real Task -> Pack validation runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Protocol

from .config import Settings, get_settings

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLM(Protocol):
    provider: str
    chat_model: str
    vision_model: str
    embed_model: str

    def chat_json(
        self, system: str, user: str, *, images: list[bytes] | None = None, purpose: str = "", temperature: float = 0.2
    ) -> dict: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (tolerates fences / <think> blocks)."""
    cleaned = THINK_RE.sub("", text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = JSON_RE.search(cleaned)
    if not m:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("model reply JSON is not an object")
    return obj


# --------------------------------------------------------------------------- real


class OpenAICompatLLM:
    provider = "openai-compatible"

    def __init__(self, settings: Settings):
        from openai import OpenAI  # imported lazily so tests never need network deps

        self.settings = settings
        self.chat_model = settings.chat_model
        self.vision_model = settings.vision_model
        self.embed_model = settings.embed_model
        self.client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=settings.llm_timeout_s)
        if settings.embed_base_url or settings.embed_api_key:
            self.embed_client = OpenAI(
                base_url=settings.embed_base_url or settings.llm_base_url,
                api_key=settings.embed_api_key or settings.llm_api_key,
                timeout=settings.llm_timeout_s,
            )
        else:
            self.embed_client = self.client

    # Qwen3 thinks by default; that is slow and useless for JSON extraction.
    @staticmethod
    def _system_for(model: str, system: str) -> str:
        if "qwen3" in model.lower() and "/no_think" not in system:
            return system + "\n/no_think"
        return system

    def chat_json(
        self, system: str, user: str, *, images: list[bytes] | None = None, purpose: str = "", temperature: float = 0.2
    ) -> dict:
        model = self.vision_model if images else self.chat_model
        content: list[dict] | str
        if images:
            content = [{"type": "text", "text": user}]
            for img in images:
                b64 = base64.b64encode(img).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        else:
            content = user
        messages = [
            {"role": "system", "content": self._system_for(model, system)},
            {"role": "user", "content": content},
        ]
        last_err: Exception | None = None
        for attempt in range(2):
            kwargs = dict(model=model, messages=messages, temperature=temperature)
            if attempt == 0:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = self.client.chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
                return extract_json(text)
            except Exception as e:  # noqa: BLE001 - retry once without json mode / with nudge
                last_err = e
                messages = messages + [{"role": "user", "content": "Return ONLY a valid JSON object, nothing else."}]
        raise RuntimeError(f"LLM call failed ({model}, purpose={purpose}): {last_err}") from last_err

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        # Ollama handles batches fine; keep batches small anyway for long texts.
        for i in range(0, len(texts), 16):
            batch = [t if t.strip() else " " for t in texts[i : i + 16]]
            resp = self.embed_client.embeddings.create(model=self.embed_model, input=batch)
            data = sorted(resp.data, key=lambda d: d.index)
            out.extend([d.embedding for d in data])
        return out


# --------------------------------------------------------------------------- fake


class FakeLLM:
    """Deterministic stand-in: hashed character n-gram embeddings + template JSON.

    Good enough to exercise persistence, retrieval ordering and pack editing offline.
    """

    provider = "fake"
    chat_model = "fake-chat"
    vision_model = "fake-vision"
    embed_model = "fake-embed-256"
    DIM = 256

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            v = [0.0] * self.DIM
            t = (t or "").lower()
            grams = list(t) + [t[i : i + 2] for i in range(len(t) - 1)] + [t[i : i + 3] for i in range(len(t) - 2)]
            for g in grams:
                if g.strip() == "":
                    continue
                h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
                v[h % self.DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / norm for x in v])
        return vecs

    def chat_json(
        self, system: str, user: str, *, images: list[bytes] | None = None, purpose: str = "", temperature: float = 0.2
    ) -> dict:
        words = [w for w in re.split(r"[\s,，。、;；:：\"'()（）\[\]]+", user) if 2 <= len(w) <= 12][:8]
        if purpose == "image_analysis":
            return {
                "summary": "fake image description",
                "subjects": ["subject"],
                "style": ["fake-style", "texture"],
                "keywords": ["fake", "image"] + words[:2],
                "mood": ["calm"],
                "visible_text": "",
            }
        if purpose == "text_analysis":
            return {"summary": user[:80], "keywords": words[:5], "topics": words[:2], "tone": ["neutral"]}
        if purpose == "task":
            return {
                "purpose": user[:60],
                "desired_qualities": words[:3],
                "avoid": [w for w in words if "不" in w][:2],
                "constraints": [],
                "needed_reference_types": ["mood", "texture"],
                "search_queries": [user[:30]] + words[:2],
                "language": "zh",
            }
        if purpose in ("recompose", "alternatives"):
            ids = list(dict.fromkeys(re.findall(r"mat_[0-9a-f]{6,}", user)))
            if purpose == "alternatives":
                return {"reasons": {mid: f"fake reason for {mid}" for mid in ids}}
            half = max(1, math.ceil(len(ids) / 2))
            groups = [
                {"name": "Mood", "purpose": "overall feel", "members": [
                    {"material_id": mid, "role": "mood reference", "reason": f"fake: fits task ({mid})"} for mid in ids[:half]]},
                {"name": "Material / Texture", "purpose": "surface quality", "members": [
                    {"material_id": mid, "role": "texture reference", "reason": f"fake: adds texture ({mid})"} for mid in ids[half:]]},
            ]
            return {"human_direction": user[:80], "groups": [g for g in groups if g["members"]], "excluded": []}
        return {"result": user[:80]}


# --------------------------------------------------------------------------- factory

_llm: LLM | None = None


def get_llm(settings: Settings | None = None) -> LLM:
    global _llm
    settings = settings or get_settings()
    if _llm is None:
        _llm = FakeLLM() if settings.llm_provider.lower() == "fake" else OpenAICompatLLM(settings)
    return _llm


def reset_llm() -> None:
    global _llm
    _llm = None
