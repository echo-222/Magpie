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
import logging
import math
import re
from typing import Protocol

from .config import Endpoint, Settings, get_settings

log = logging.getLogger(__name__)

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLM(Protocol):
    provider: str
    chat_provider: str
    chat_model: str
    vision_model: str
    embed_model: str
    last_chat_model: str | None

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        images: list[bytes] | None = None,
        purpose: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
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
    """One adapter, three roles (chat / vision / embed), each with its own endpoint.

    Chat may run remotely (DeepSeek) with the local model as a one-shot fallback on
    transport/API errors; vision and embeddings stay wherever MAGPIE_LLM_* / MAGPIE_EMBED_*
    point (local Ollama by default). Images are only ever sent to the vision endpoint.
    """

    provider = "openai-compatible"

    def __init__(self, settings: Settings):
        from openai import OpenAI  # imported lazily so tests never need network deps

        self.settings = settings
        self.chat_ep = settings.chat_endpoint()
        self.chat_fallback_ep = settings.chat_fallback_endpoint()
        self.vision_ep = settings.vision_endpoint()
        self.embed_ep = settings.embed_endpoint()
        self.chat_model = self.chat_ep.model
        self.chat_provider = self.chat_ep.name
        self.vision_model = self.vision_ep.model
        self.embed_model = self.embed_ep.model
        self.last_chat_model: str | None = None  # which model actually answered the last chat call
        self._clients: dict[tuple[str, str], OpenAI] = {}
        self._OpenAI = OpenAI

    def _client(self, ep: Endpoint):
        key = (ep.base_url, ep.api_key)
        if key not in self._clients:
            # max_retries=0: a slow local model must not turn one timeout into three.
            self._clients[key] = self._OpenAI(base_url=ep.base_url, api_key=ep.api_key, timeout=self.settings.llm_timeout_s, max_retries=0)
        return self._clients[key]

    # Qwen3 thinks by default; that is slow and useless for JSON extraction.
    @staticmethod
    def _system_for(model: str, system: str) -> str:
        if "qwen3" in model.lower() and "/no_think" not in system:
            return system + "\n/no_think"
        return system

    def _chat_once(self, ep: Endpoint, messages: list[dict], *, temperature: float, max_tokens: int | None, json_mode: bool) -> str:
        kwargs: dict = dict(model=ep.model, messages=messages, temperature=temperature)
        if max_tokens:
            kwargs["max_tokens"] = max_tokens + ep.reasoning_budget
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if ep.extra_body:
            kwargs["extra_body"] = ep.extra_body
        resp = self._client(ep).chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def _chat_json_on(self, ep: Endpoint, system: str, content, *, temperature: float, max_tokens: int | None, purpose: str) -> dict:
        """JSON mode first; on an unparsable reply retry once with a nudge and without JSON mode.
        Transport/API errors propagate so the caller can decide about a fallback endpoint."""
        import openai

        messages = [{"role": "system", "content": self._system_for(ep.model, system)}, {"role": "user", "content": content}]
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                text = self._chat_once(ep, messages, temperature=temperature, max_tokens=max_tokens, json_mode=attempt == 0)
                result = extract_json(text)
                self.last_chat_model = ep.model
                return result
            except openai.APIError:
                raise
            except Exception as e:  # noqa: BLE001 - parse problem: nudge once
                last_err = e
                messages = messages + [{"role": "user", "content": "Return ONLY a valid JSON object, nothing else."}]
        raise RuntimeError(f"LLM reply not parsable ({ep.name}/{ep.model}, purpose={purpose}): {last_err}") from last_err

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        images: list[bytes] | None = None,
        purpose: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict:
        import openai

        if images:  # vision stays on its own endpoint; images never go to the chat provider
            content: list[dict] = [{"type": "text", "text": user}]
            for img in images:
                b64 = base64.b64encode(img).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            try:
                return self._chat_json_on(self.vision_ep, system, content, temperature=temperature, max_tokens=max_tokens, purpose=purpose)
            except openai.APIError as e:
                raise RuntimeError(f"vision call failed ({self.vision_ep.model}, purpose={purpose}): {e}") from e

        try:
            return self._chat_json_on(self.chat_ep, system, user, temperature=temperature, max_tokens=max_tokens, purpose=purpose)
        except openai.APIError as e:
            if self.chat_fallback_ep is None:
                raise RuntimeError(f"chat call failed ({self.chat_ep.name}/{self.chat_ep.model}, purpose={purpose}): {e}") from e
            log.warning("chat provider %s failed (%s: %s); falling back to local %s", self.chat_ep.name, type(e).__name__, str(e)[:160], self.chat_fallback_ep.model)
            try:
                return self._chat_json_on(self.chat_fallback_ep, system, user, temperature=temperature, max_tokens=max_tokens, purpose=purpose)
            except openai.APIError as e2:
                raise RuntimeError(f"chat call failed on {self.chat_ep.name} and local fallback (purpose={purpose}): {e2}") from e2

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        client = self._client(self.embed_ep)
        # Ollama handles batches fine; keep batches small anyway for long texts.
        for i in range(0, len(texts), 16):
            batch = [t if t.strip() else " " for t in texts[i : i + 16]]
            resp = client.embeddings.create(model=self.embed_ep.model, input=batch)
            data = sorted(resp.data, key=lambda d: d.index)
            out.extend([d.embedding for d in data])
        return out


# --------------------------------------------------------------------------- fake


class FakeLLM:
    """Deterministic stand-in: hashed character n-gram embeddings + template JSON.

    Good enough to exercise persistence, retrieval ordering and pack editing offline.
    """

    provider = "fake"
    chat_provider = "fake"
    chat_model = "fake-chat"
    vision_model = "fake-vision"
    embed_model = "fake-embed-256"
    last_chat_model = "fake-chat"
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
        self,
        system: str,
        user: str,
        *,
        images: list[bytes] | None = None,
        purpose: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
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
