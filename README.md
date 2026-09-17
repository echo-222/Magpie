# Magpie
Collect inspiration. Create with it.

Magpie is a personal material-memory layer: save what caught your eye **plus one line of
your own thought**, let the system understand it, and later — when a real task shows up —
have Magpie retrieve and recompose the relevant old materials into a **Material Pack** you
can hand to another AI agent.

Spec (source of truth): [`docs/Magpie_MVP_Spec_Agent_Handoff.md`](docs/Magpie_MVP_Spec_Agent_Handoff.md)
Phase 1 report: [`docs/PHASE1_REPORT.md`](docs/PHASE1_REPORT.md)

## Phase 1 status (backend/core prototype)

```
Material + Human Thought → AI understanding → Material Memory (SQLite)
→ Task understanding → task-aware retrieval → recomposition → Material Pack
→ human remove / move / alternatives / note → Copy for Agent (Markdown / JSON)
```

All of the above works end-to-end with **local models only** (Ollama), no API key required.
Browser capture is the front-end teammate's track and is not part of this repo yet; it will
talk to `POST /materials` (see "Capture contract").

## Quickstart

Requirements: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/) (or pip), [Ollama](https://ollama.com).

```bash
# 1. models (≈12 GB total; any OpenAI-compatible provider works instead, see .env.example)
scripts/setup_ollama_models.sh
#   = ollama pull qwen3:8b qwen2.5vl:7b bge-m3  +  a 16k-context variant `qwen3:8b-16k`
#     (Ollama's default 4096-token context truncates the recomposition prompt)

# 2. project
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev,ocr]"   # drop ",ocr" to skip RapidOCR
cp .env.example .env                                        # defaults already point at Ollama
.venv/bin/magpie doctor                                     # checks models / OCR / db

# 3. import the demo library (30 real materials with Human Thoughts; ~10 min on a laptop)
.venv/bin/magpie import demo_materials/manifest.json -v

# 4. dev UI + API
.venv/bin/magpie serve            # http://127.0.0.1:8765  (OpenAPI docs at /docs)
```

CLI shortcuts: `magpie search "纸张质感"`, `magpie pack "我要做一个克制、有物质感的首页"`,
`magpie packs`, `magpie export <pack_id> [--format json]`.

Offline tests (no models needed): `.venv/bin/python -m pytest`.

## Capture contract (for the browser-extension track)

`POST /materials` — `multipart/form-data` with `file` (image) **or** `content` (text), plus
`thought`, `page_url`, `resource_url`, `page_title`, `captured_at`; or `application/json`
with a `CapturePayload` body (`magpie/models.py`). Analysis runs in the background;
poll `GET /materials/{id}` until `processing.status == "ready"`.

## Layout

```
magpie/            core package
  models.py        Material / Task / Match / MaterialPack / CapturePayload
  db.py            SQLite persistence (human / file / model columns kept apart), FTS5, embeddings
  llm.py           OpenAI-compatible model adapter (+ FakeLLM for tests)
  analysis/        image (Pillow, RapidOCR, vision model) and text analysis
  ingest.py        capture payload → stored material → analysis → embeddings
  retrieval.py     hybrid retrieval over Human Thought + machine understanding
  task.py          task → small brief (+ retrieval query expansion)
  recompose.py     task-aware selection, grouping, roles, reasons → Material Pack; alternatives
  pack.py          human edits (remove / move / add / note / rename), persisted
  export.py        Copy for Agent (Markdown, JSON)
  api.py, cli.py   FastAPI app + dev page, command line
web/index.html     thin vanilla-JS dev UI
demo_materials/    manifest + 20 Commons images + 10 text clippings, each with a Human Thought
scripts/           fetch_demo_images.py, task_pack_evidence.py
docs/              spec, Phase 1 report, evidence of real Task → Pack runs
```
