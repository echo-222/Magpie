"""Environment-based settings. No secrets are hard-coded; see .env.example.

Model roles and where they run:

* chat   – task understanding, text analysis, recomposition, match reasons, alternatives.
           `MAGPIE_CHAT_PROVIDER=local` -> Ollama (MAGPIE_LLM_* + MAGPIE_CHAT_MODEL)
           `MAGPIE_CHAT_PROVIDER=deepseek` -> DeepSeek API (DEEPSEEK_*), local model kept as fallback
* vision – image description; always the MAGPIE_LLM_* endpoint (local Ollama by default)
* embed  – embeddings; MAGPIE_EMBED_* if set, else the MAGPIE_LLM_* endpoint
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load ./.env (if present) once at import time; real env vars always win.
load_dotenv(override=False)

TRUTHY = {"1", "true", "yes", "on"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


@dataclass(frozen=True)
class Endpoint:
    """One OpenAI-compatible endpoint + model for a role."""

    name: str  # "local" | "deepseek"
    base_url: str
    api_key: str
    model: str
    extra_body: dict | None = None  # provider-specific request fields (e.g. DeepSeek thinking switch)
    reasoning_budget: int = 0  # extra output tokens reserved for hidden reasoning when max_tokens is set

    @property
    def host(self) -> str:
        return self.base_url.split("//", 1)[-1].split("/", 1)[0]


@dataclass
class Settings:
    # "openai" = any OpenAI-compatible endpoint (OpenAI, Ollama, DeepSeek, ...); "fake" = offline test stub
    llm_provider: str = field(default_factory=lambda: _env("MAGPIE_LLM_PROVIDER", "openai"))

    # local / default endpoint (Ollama): vision + embeddings, and chat when MAGPIE_CHAT_PROVIDER=local
    llm_base_url: str = field(default_factory=lambda: _env("MAGPIE_LLM_BASE_URL", "http://localhost:11434/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("MAGPIE_LLM_API_KEY", "ollama"))
    # qwen3:8b-16k = qwen3:8b with num_ctx 16384 (scripts/setup_ollama_models.sh); Ollama's default
    # 4096-token context silently truncates the recomposition prompt.
    chat_model: str = field(default_factory=lambda: _env("MAGPIE_CHAT_MODEL", "qwen3:8b-16k"))
    vision_model: str = field(default_factory=lambda: _env("MAGPIE_VISION_MODEL", "qwen2.5vl:7b"))

    # chat provider switch
    chat_provider: str = field(default_factory=lambda: (_env("MAGPIE_CHAT_PROVIDER", "local") or "local").lower())
    deepseek_api_key: str | None = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = field(default_factory=lambda: _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    deepseek_chat_model: str = field(default_factory=lambda: _env("DEEPSEEK_CHAT_MODEL", "deepseek-v4-pro"))
    deepseek_thinking: str | None = field(default_factory=lambda: _env("DEEPSEEK_THINKING"))  # on | off | unset = provider default
    chat_fallback_to_local: bool = field(
        default_factory=lambda: (_env("MAGPIE_CHAT_FALLBACK_TO_LOCAL", "true") or "true").lower() in TRUTHY
    )

    embed_model: str = field(default_factory=lambda: _env("MAGPIE_EMBED_MODEL", "bge-m3"))
    embed_base_url: str | None = field(default_factory=lambda: _env("MAGPIE_EMBED_BASE_URL"))
    embed_api_key: str | None = field(default_factory=lambda: _env("MAGPIE_EMBED_API_KEY"))

    ocr_mode: str = field(default_factory=lambda: _env("MAGPIE_OCR", "auto"))  # auto | off
    data_dir: Path = field(default_factory=lambda: Path(_env("MAGPIE_DATA_DIR", "./data")).expanduser())

    llm_timeout_s: float = field(default_factory=lambda: float(_env("MAGPIE_LLM_TIMEOUT", "180")))

    # CORS for the dev-stage capture layer (browser extension / local pages): comma-separated origins or "*"
    cors_origins: str = field(default_factory=lambda: _env("MAGPIE_CORS_ORIGINS", "*"))

    # ------------------------------------------------------------------ endpoints per role
    def local_chat_endpoint(self) -> Endpoint:
        return Endpoint("local", self.llm_base_url, self.llm_api_key, self.chat_model)

    def deepseek_chat_endpoint(self) -> Endpoint:
        if not self.deepseek_api_key:
            raise RuntimeError(
                "MAGPIE_CHAT_PROVIDER=deepseek but DEEPSEEK_API_KEY is not set. "
                "Add it to .env or switch to MAGPIE_CHAT_PROVIDER=local."
            )
        extra = None
        budget = 8000  # DeepSeek counts hidden reasoning against max_tokens; reserve room for it
        if self.deepseek_thinking:
            enabled = self.deepseek_thinking.lower() in TRUTHY
            extra = {"thinking": {"type": "enabled" if enabled else "disabled"}}
            budget = 8000 if enabled else 0
        return Endpoint("deepseek", self.deepseek_base_url, self.deepseek_api_key, self.deepseek_chat_model, extra, budget)

    def chat_endpoint(self) -> Endpoint:
        if self.chat_provider == "deepseek":
            return self.deepseek_chat_endpoint()
        if self.chat_provider != "local":
            raise RuntimeError(f"unknown MAGPIE_CHAT_PROVIDER={self.chat_provider!r} (use local | deepseek)")
        return self.local_chat_endpoint()

    def chat_fallback_endpoint(self) -> Endpoint | None:
        """Local chat model used once when the remote chat provider fails; None when chat is already local."""
        if self.chat_provider == "local" or not self.chat_fallback_to_local:
            return None
        return self.local_chat_endpoint()

    def vision_endpoint(self) -> Endpoint:
        return Endpoint("local", self.llm_base_url, self.llm_api_key, self.vision_model)

    def embed_endpoint(self) -> Endpoint:
        return Endpoint(
            "local" if not self.embed_base_url else "embed",
            self.embed_base_url or self.llm_base_url,
            self.embed_api_key or self.llm_api_key,
            self.embed_model,
        )

    # ------------------------------------------------------------------ paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "magpie.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    def ensure_dirs(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)


def mask_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) <= 8:
        return "*" * len(key)
    return key[:5] + "…" + key[-3:]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Used by tests after mutating environment variables."""
    global _settings
    _settings = None
