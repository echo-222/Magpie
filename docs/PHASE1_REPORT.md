# Magpie Phase 1 Report (spec §13)

Branch: `chenbin/mvp-core` · Date: 2026-09-17 · Author: local coding agent for 陈彬
Spec: `docs/Magpie_MVP_Spec_Agent_Handoff.md` (MVP-0.2). Phase 2 has **not** been started.

## 0. One-paragraph summary

The differentiated loop runs end-to-end on a laptop with local models only (no API key):
30 real materials with first-person Human Thoughts were ingested and analysed; three real
tasks produced three clearly different Material Packs with task-specific groups, roles and
reasons; a human removed / moved / annotated items and the edits persisted and were honoured
by "find alternatives"; `Copy for Agent` produced ~8.6 k characters of Markdown context that
carries the task, the human direction, every Human Thought, why each item was chosen, what
each image looks like, quoted texts and sources. Evidence is in `docs/phase1_evidence/`.
The most convincing single observation: a paper-texture close-up that the 7B vision model
mis-described as a "gradient background" was still retrieved for the typography-booklet task
**because of the Human Thought** ("纸纤维的颗粒感") — exactly the thesis the spec asks us to test.

## 1. What is actually implemented (spec §12 checklist)

| # | Completion criterion | Status | Where / evidence |
|---|---|---|---|
| 1 | Real image/text material imported | done | `magpie import demo_materials/manifest.json` → 30/30 ready |
| 2 | Human Thought saved separately | done | `materials.human_json` column, `provenance.human_fields`; `PATCH /materials/{id}/thought` only touches that column |
| 3 | Machine analysis appears | done | summary / subjects / style / keywords / mood / OCR / palette per material (`magpie list`, dev UI) |
| 4 | Several dozen real materials | done (30) | 20 Wikimedia Commons images + 10 text clippings, each with a thought and a license record |
| 5 | User enters a real task | done | dev UI "What are you making?", `POST /packs`, `magpie pack` |
| 6 | Relevant old materials retrieved | done | hybrid retrieval: combined + human-thought embeddings (bge-m3) + FTS5, query expansion from the task brief |
| 7 | Magpie creates a Material Pack | done | 3 real packs, 3–5 task-specific groups each, `generation.fallback == False` |
| 8 | Selected materials have task-specific reasons | done | every member has `role` + `reason`; see `phase1_evidence/SUMMARY.md` |
| 9 | User removes or moves at least one item | done | task 1: removed rusted shutter, moved Swiss-style text to a new group, added a note — via HTTP and via the dev UI |
| 10 | Pack state persists | done | `packs.doc` JSON incl. `human_edits` log and `removed_material_ids`; reload verified |
| 11 | `Copy for Agent` produces usable Markdown | done | `GET /packs/{id}/export`, clipboard button in the UI; JSON twin at `?format=json` |

Gate order was followed (A → B → C), each gate has its own commit and offline smoke tests
(20 tests, `python -m pytest`, FakeLLM, < 1 s). The final validation runs used real models.

Not done on purpose (anti-goals / other track): browser extension, video/audio, Docker,
auth, polished UI, MCP.

## 2. Reused open-source / library components

Full table with licenses: `docs/REUSED_COMPONENTS.md`. In short:

- **Commodity capabilities via dependencies, nothing self-built:** Pillow (metadata, EXIF,
  thumbnails, median-cut dominant colours), RapidOCR/onnxruntime (OCR, optional extra),
  Qwen2.5-VL-7B (image description), Qwen3-8B (text analysis, task understanding,
  recomposition), BGE-M3 (embeddings), SQLite + FTS5 (persistence, full text), FastAPI /
  Pydantic / Uvicorn (API), `openai` SDK (one adapter for any OpenAI-compatible endpoint,
  default Ollama), NumPy (cosine over the embedding matrix).
- **Architecture references only, no code copied:** Karakeep (processing status, background
  enrichment, hybrid search), TagStudio (overlay DB over original files, human vs derived
  metadata). Linkwarden extension and Arkiv were not needed in Phase 1.

## 3. What is genuinely Magpie-specific (written here)

| Module | Magpie logic |
|---|---|
| `magpie/models.py` | Material with `human.thought` as a first-class field, `provenance` (human / file / machine fields), Task, PackMember (= Match: role ≠ similarity ≠ reason), MaterialPack with `human_edits` and `removed_material_ids`, CapturePayload contract for the browser track |
| `magpie/db.py` | human / file / model columns stored and updated **separately** so re-analysis can never overwrite a thought; two embeddings per material |
| `magpie/retrieval.py` | `build_embedding_texts` (what the memory actually embeds), the **combined + human-thought** dual-vector scoring with a `via` signal, FTS fusion, best-of-N query phrasings |
| `magpie/task.py` | task → small brief; `search_queries` rephrase the task as *what a good reference looks/feels like* |
| `magpie/recompose.py` | task-aware selection (usefulness over similarity), task-specific groups, roles, per-item reasons built on the Human Thought, `excluded` with reasons; `alternatives` = seed embedding blended with the task embedding, never re-suggesting human removals; schema-forcing prompt shape that keeps an 8B model on the rails |
| `magpie/pack.py` | remove / move / add / note / rename with a persisted edit log; explicit human re-add wins over an earlier removal |
| `magpie/export.py` | Copy-for-Agent Markdown/JSON: what an agent cannot see for itself (thought, why-now, image description + palette, quoted text, source, deliberate removals) |
| `magpie/analysis/image.py` (part) | palette *descriptors* (low saturation / warm / blue-purple dominant / near-monochrome) so colour language in tasks is matchable without a model |
| `web/index.html` | the one-page dev UI from spec §7 |
| `demo_materials/` | 30 curated materials + Human Thoughts (see limitation 6.1) |

Everything else (HTTP plumbing, CLI, config) is thin glue.

## 4. Current architecture

```
CAPTURE (teammate, later)        PROCESSING / MEMORY (Phase 1)                   TASK / RECOMPOSITION (Phase 1)
browser ext ──POST /materials──▶ ingest.create_*  → data/files/<id>.<ext>       task text ──▶ task.understand_task
   CapturePayload + file          ingest.analyze   → Pillow · RapidOCR · vision   │  brief + search_queries
                                  build_embedding_texts → bge-m3 (combined, human)│
                                  SQLite: materials · material_embeddings · FTS5  ├─▶ retrieval.search (best-of queries,
                                                                                  │    combined+human vectors, FTS bonus)
                                                                                  ├─▶ recompose._plan (one schema-forced call)
                                                                                  ├─▶ MaterialPack → packs.doc
                                                                                  ├─▶ pack.remove/move/add/note  (edit log)
                                                                                  ├─▶ recompose.alternatives (task-aware)
                                                                                  └─▶ export.pack_to_markdown / _json
                                                                                        ── "Copy for Agent" ──▶ ChatGPT / Claude / Cursor
```

Model access goes through one adapter (`magpie/llm.py`, OpenAI-compatible; `FakeLLM` for
tests). Defaults: Ollama at `localhost:11434` with `qwen3:8b-16k` (chat), `qwen2.5vl:7b`
(vision), `bge-m3` (embeddings). Any OpenAI-compatible cloud provider is a `.env` change.

Storage: one SQLite file (`data/magpie.db`, WAL) + original files + thumbnails under
`data/`. Vector search is brute-force cosine with NumPy over ≤ hundreds of rows — the
"simplest vector store available"; swap to sqlite-vec / pgvector when the library grows.

## 5. Demo procedure

```bash
scripts/setup_ollama_models.sh                      # pulls qwen3:8b, qwen2.5vl:7b, bge-m3; creates qwen3:8b-16k
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev,ocr]"
cp .env.example .env
.venv/bin/magpie doctor                             # models reachable, OCR present, num_ctx >= 8192
.venv/bin/magpie import demo_materials/manifest.json -v   # ~10 min on an M-series laptop
.venv/bin/magpie serve                              # open http://127.0.0.1:8765
```

In the page: import one of your own images with a thought → watch analysis appear → search
the library (`纸张质感`, `克制 安静`) → type a task → **Build Material Pack** (≈ 1–2.5 min
locally) → Remove / Move / Note / Alternatives → **Copy for Agent** → paste into ChatGPT /
Claude / Cursor. Equivalent CLI: `magpie search`, `magpie pack "<task>"`, `magpie export <pack_id>`.
Re-run the evidence: `python scripts/task_pack_evidence.py` (default three tasks, or your own).

## 6. Known limitations

1. **Human Thoughts in the demo library were written by the agent**, first person, as a
   stand-in for the product owner. They are plausible but not yours. Serious validation needs
   your own saved things and your own one-liners (the import manifest makes that a JSON edit).
2. **Local 8B models are slow and occasionally sloppy.** Build ≈ 60–150 s (task brief 15–50 s,
   recomposition 45–100 s); a cloud model would be 5–10 s. Quality slips seen: one member put
   in a group with a role of "反例排除" instead of being excluded (task 3, moon gate); Ando /
   Nuremberg filed under 排版/字体 for an Instagram task; a reason claiming the Braun SK4 is
   "哑光黑与白" (it is white and wood). Reasons are otherwise specific and often echo the thought.
3. **Ollama's default 4096-token context truncated the recomposition prompt** (the system
   prompt with the schema is what gets cut). Symptoms were English drift, invented labels and
   very slow generation. Fixed by a 16k Modelfile variant (`qwen3:8b-16k`), tighter candidate
   cards and `max_tokens`; `magpie doctor` now checks `num_ctx`. Providers other than Ollama
   are unaffected.
4. **Vision analysis errors on abstract textures.** `washi_fiber_closeup` was described as a
   grey gradient; `kintsugi_bowl` lost the gold repair. The thought compensates in retrieval,
   but machine-only searches (e.g. "蓝紫渐变") can surface the wrong image first.
5. **Retrieval scale**: brute-force cosine, no ANN, no re-ranker, fixed weights
   (0.65 combined / 0.35 human, +0.08 FTS bonus). Fine for ≤ a few hundred materials.
6. **FTS5 trigram** needs ≥ 3 characters; shorter queries fall back to `LIKE`. No Chinese
   word segmentation (embeddings carry the semantic load).
7. **Single-process, synchronous model calls** behind one lock; background analysis is
   FastAPI `BackgroundTasks`, not a queue (deliberate: no generic job infrastructure in Phase 1).
8. **No regenerate-in-place.** Re-running a task creates a new pack; human edits live per pack.
   `alternatives` honours removals; nothing else regenerates.
9. **Dev UI only** (vanilla HTML/JS, `prompt()` dialogs). No auth, no multi-user.
10. Tooling note: Cursor's workspace-move call failed with an internal error, so the work was
    done in `/Users/shadow/Projects/Magpie` via absolute paths; no effect on the repo.

## 7. Evidence: three real Task → Material Pack runs

Library: 30 materials. Models: qwen3:8b(-16k) / qwen2.5vl:7b / bge-m3, fallback never
triggered. Full Copy-for-Agent output per task in `docs/phase1_evidence/*.md`, JSON twins
alongside, compact summary in `docs/phase1_evidence/SUMMARY.md`.

**Task 1 — 我要做一个克制、有物质感、不要典型 AI 蓝紫色的网站首页。** `pack_939dd82f46`
- 材质/质感: 白石和纸信纸 (主材质), 沙丘风纹 (辅助纹理, "可代替噪点渐变" ← echoes the thought), Barbican 剁斧混凝土 (工业质感)
- 色彩方向: 北宋汝窑青瓷 (主色调 — "可替代科技蓝", straight from the thought), Hammershøi (氛围色), Braun SK4 (辅助色)
- 反例: Corporate Memphis, Kelmscott Chaucer (the thought had said "反极简的极致，做对比参考" — consistent)
- Excluded: Synthwave render ("蓝紫色调与 AI 视觉趋势高度重合")
- Human edits: removed the rusted shutter (Magpie had flagged its blue as risky), moved the
  Swiss-style text into a new group 版式结构, noted 汝窑 "主色就用这个天青，别再调蓝". All persisted;
  `alternatives` for 材质/质感 then proposed the washi fibre close-up first (with a reason built
  on its thought) and did not re-propose the removed shutter.

**Task 2 — 手工陶器小品牌的 Instagram 视觉方向和文案语气：安静、有手感、接受不完美，不要网红滤镜和营销腔。** `pack_e5530f841f`
- 材质/质感: 黑乐茶碗 (核心案例, "不规则釉色与斑点直接体现瑕疵美学"), 剁斧混凝土, 唐碑拓本
- 色彩方向: 汝窑青瓷, 沙丘风纹
- 排版/字体: 纽伦堡编年史, 安藤忠雄 光之教会 (weak fit, see limitation 2)
- 文案语气: William Morris (宣言参考), Saint-Exupéry (思想延伸) — the text clippings appear **only** in the task that asked for a copy tone
- Excluded: Braun SK4 ("工业设计的温润感与品牌追求的粗粝手工质感冲突")

**Task 3 — 为一本讲中文字体历史的小册子做封面和内页排版，要有纸本书的物质感，参考老版本的版面知识。** `pack_cf18804507`
- 材质/质感: 和纸纤维特写 (基底材质, retrieved **via human_thought** although the AI description was wrong), 白石和纸, 唐碑拓本, 活字铅字盒
- 排版/字体: 宋版《周易》 (结构参考), 纽伦堡编年史 (栏框范例), Lissitzky (留白示范), Swiss style (网格反例 "需与纸张温度结合使用" — the thought said exactly this)
- 装饰/边框: Kelmscott Chaucer (边框灵感); 苏州月洞门 mis-filed as "反例排除" (should have been excluded)
- Excluded: Braun SK4

Overlap between the three packs is small and explainable (concrete and celadon are
material/colour anchors for two tasks each); the copy-tone texts, the paper textures and the
typography pages each show up only where the task calls for them. That is the behaviour the
spec wanted to see.

## 8. What felt useful vs generic

Useful (this is where Magpie is different):
- The **Human Thought inside retrieval and inside the reasons**. Thoughts rescued
  mis-described images, and reasons that quote the thought read like the person's own notes
  rather than captions.
- **Task-specific groups and roles** (主材质 / 辅助纹理 / 主色调 / 反例 / 文案语气). The same
  library re-sorted itself per task; "excluded with a reason" was often the most honest line.
- **Removals that stay removed** and a Copy-for-Agent that lists them ("Deliberately left out
  by the human") — the agent gets intent, not just a list.
- **Search queries phrased as references** ("陶土质感 close-up", "低饱和度色彩") instead of the
  task sentence — cheap and it visibly improved recall over 30 items.

Generic (necessary, not differentiating): ingestion, OCR, palette extraction, embeddings,
CRUD API, dev UI. All done with libraries; none of it took design attention.

## 9. Reinventing-the-wheel check

- Cosine similarity over a NumPy matrix instead of a vector database: ~20 lines, deliberate
  at this scale, flagged for replacement (sqlite-vec / pgvector). Not a vector DB.
- Hue-family naming and palette descriptors in `analysis/image.py`: ~40 lines of colour maths
  on top of Pillow's quantiser. Could be replaced by a colour-naming library later; kept because
  the descriptors ("warm", "low saturation", "blue-purple dominant") are what tasks talk about.
- `extract_json` / `<think>` stripping / `/no_think` handling in `llm.py`: small model-hygiene
  glue, not a framework.
- No custom OCR, ASR, video, embedding, job queue, agent framework, or browser plumbing.

## 10. Recommended Phase 2 (not started — awaiting human review)

1. **Real validation with the owner's materials and thoughts** (replace the demo library;
   30–100 items), then judge the three packs again. This is the actual product test.
2. **Connect the capture track**: the extension posts to `POST /materials` as-is; add
   `resource_url` fetching for image URLs and a small "saved ✓ / analysing…" status endpoint.
3. **Quality/speed**: try a cloud chat model for recomposition (`.env` change) to separate
   "the idea is weak" from "the 8B model is weak"; add a light re-rank step; consider two-pass
   recomposition (select+group, then reasons) if group placement stays noisy.
4. **Pack UX**: regenerate a group while pinning human choices; drag ordering; edit reasons;
   pack history and retrieving old packs as materials.
5. **Memory growth**: sqlite-vec, thumbnails in export, duplicate detection across sources.
6. Later per spec §14: voice thought, video/audio (FFmpeg/Whisper via Arkiv patterns),
   MCP tools (`search_materials`, `get_pack`, …) once Copy for Agent proves worth sending.
