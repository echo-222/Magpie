"""Offline test fixtures: FakeLLM provider, temp data dir, fresh SQLite per test."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

os.environ["MAGPIE_LLM_PROVIDER"] = "fake"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from magpie import config, llm

    monkeypatch.setenv("MAGPIE_LLM_PROVIDER", "fake")
    monkeypatch.setenv("MAGPIE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAGPIE_OCR", "off")  # keep tests fast: no onnx model load
    config.reset_settings()
    llm.reset_llm()
    settings = config.get_settings()
    settings.ensure_dirs()
    yield settings
    config.reset_settings()
    llm.reset_llm()


@pytest.fixture()
def db(env):
    from magpie.db import Database

    d = Database(env.db_path)
    yield d
    d.close()


@pytest.fixture()
def ingestor(db, env):
    from magpie.ingest import Ingestor
    from magpie.llm import get_llm

    return Ingestor(db=db, settings=env, llm=get_llm(env))


def make_image_bytes(color=(200, 180, 150), size=(320, 200), text: str | None = None) -> bytes:
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 120, 120], fill=(60, 50, 40))
    if text:
        d.text((140, 90), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def sample_image() -> bytes:
    return make_image_bytes(text="MAGPIE")


@pytest.fixture()
def demo_manifest_path() -> Path:
    return Path(__file__).resolve().parents[1] / "demo_materials" / "manifest.json"
