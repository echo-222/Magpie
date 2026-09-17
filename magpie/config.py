"""Environment-based settings. No secrets are hard-coded; see .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load ./.env (if present) once at import time; real env vars always win.
load_dotenv(override=False)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


@dataclass
class Settings:
    # "openai" = any OpenAI-compatible endpoint (OpenAI, Ollama, DeepSeek, Qwen, ...)
    # "fake"   = deterministic offline stub used by tests
    llm_provider: str = field(default_factory=lambda: _env("MAGPIE_LLM_PROVIDER", "openai"))
    llm_base_url: str = field(default_factory=lambda: _env("MAGPIE_LLM_BASE_URL", "http://localhost:11434/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("MAGPIE_LLM_API_KEY", "ollama"))
    # qwen3:8b-16k = qwen3:8b with num_ctx 16384 (scripts/setup_ollama_models.sh); Ollama's default
    # 4096-token context silently truncates the recomposition prompt.
    chat_model: str = field(default_factory=lambda: _env("MAGPIE_CHAT_MODEL", "qwen3:8b-16k"))
    vision_model: str = field(default_factory=lambda: _env("MAGPIE_VISION_MODEL", "qwen2.5vl:7b"))

    embed_model: str = field(default_factory=lambda: _env("MAGPIE_EMBED_MODEL", "bge-m3"))
    embed_base_url: str | None = field(default_factory=lambda: _env("MAGPIE_EMBED_BASE_URL"))
    embed_api_key: str | None = field(default_factory=lambda: _env("MAGPIE_EMBED_API_KEY"))

    ocr_mode: str = field(default_factory=lambda: _env("MAGPIE_OCR", "auto"))  # auto | off
    data_dir: Path = field(default_factory=lambda: Path(_env("MAGPIE_DATA_DIR", "./data")).expanduser())

    llm_timeout_s: float = field(default_factory=lambda: float(_env("MAGPIE_LLM_TIMEOUT", "180")))

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
