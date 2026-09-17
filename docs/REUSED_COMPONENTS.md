# Reused components and licenses (spec §0.2 license gate)

Everything below is used **as a dependency** (installed package or model weights) — no
source code was copied from another project into Magpie.

## Python dependencies

| Component | Role in Magpie | License |
|---|---|---|
| FastAPI, Starlette, Uvicorn | HTTP API + dev page hosting | MIT / BSD-3 |
| Pydantic | Material / Task / Pack models, request validation | MIT |
| python-multipart | multipart uploads for `POST /materials` | Apache-2.0 |
| python-dotenv | `.env` loading (no secrets in code) | BSD-3 |
| openai (SDK) | one adapter for any OpenAI-compatible endpoint (Ollama, OpenAI, DeepSeek, Qwen…) | Apache-2.0 |
| Pillow | image metadata, EXIF, thumbnails, median-cut dominant colours | MIT-CMU (HPND) |
| NumPy | cosine similarity over the embedding matrix | BSD-3 |
| RapidOCR (rapidocr-onnxruntime) + onnxruntime | OCR (PP-OCR models, CJK + Latin) — optional extra | Apache-2.0 / MIT |
| SQLite (stdlib `sqlite3`) + FTS5 | persistence, full-text index | Public domain |
| pytest, httpx | tests | MIT / BSD-3 |

## Models (run locally through Ollama by default; swappable via `.env`)

| Model | Role | License |
|---|---|---|
| Qwen3-8B (`qwen3:8b`) | task understanding, text analysis, recomposition reasoning | Apache-2.0 |
| Qwen2.5-VL-7B (`qwen2.5vl:7b`) | image description, style keywords, visible text fallback | Apache-2.0 |
| BGE-M3 (`bge-m3`) | multilingual embeddings (Chinese tasks ↔ mixed-language materials) | MIT |
| Ollama | local model server, OpenAI-compatible endpoint | MIT |

## Projects used only as architecture references (no code copied)

- **Karakeep** (AGPL-3.0): ingestion → processing status → background enrichment → indexing → search
  shape informed `processing.status`, background analysis after `POST /materials`, and the
  hybrid (FTS + vector) search. Its bookmark-centric data model was deliberately **not** adopted.
- **TagStudio** (GPL-3.0-only): "database overlay over original files" and human-vs-derived
  metadata separation informed keeping `original` files on disk with a SQLite row whose
  human / file / model columns are stored and updated separately.
- **Linkwarden browser extension** (MIT) and **Arkiv** (PolyForm Perimeter): not used in Phase 1
  (browser capture is the front-end track; video is post-MVP).

## Demo materials

`demo_materials/manifest.json` records, per item, the Wikimedia Commons file page, original
file URL, license (Public domain / CC0 / CC BY / CC BY-SA) and attribution. Text clippings are
short quotations or CC BY-SA encyclopedia excerpts with their source page. Images are stored
as ≤1400 px JPEGs for the demo only; the manifest links to the originals.
