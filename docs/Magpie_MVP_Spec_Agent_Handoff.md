# Magpie MVP Spec & Agent Handoff

Version: MVP-0.2  
Date: 2026-09-17  
Owner: 陈彬  
Status: Ready for local Agent execution in `echo-222/Magpie`  
Source baseline: 《素材 Agent：第一阶段产品需求文档 V0.3》 + 当前团队收敛后的 MVP 策略

---

## 0. Executive instruction to the Agent

You are implementing the first executable slice of **Magpie**.

Your first job is **NOT** to build the complete product, NOT to polish UI, and NOT to reimplement mature infrastructure.


## 0.1 Repository and Git workflow

Canonical repository:

```text
https://github.com/echo-222/Magpie
```

The repository is the single source of truth. Development happens in a local clone.

Use a dedicated branch for this work:

```text
chenbin/mvp-core
```

Rules:

- Do not develop in a separate unrelated local project and upload it later.
- Do not commit directly to `main`.
- Do not overwrite or rewrite teammates' work.
- Before coding, inspect the current repository and preserve any existing structure unless there is a clear reason to change it.
- Keep commits small and meaningful.
- Push the branch to GitHub so work is visible and recoverable.
- Before the final push, run the available tests / smoke checks and inspect `git status`.
- If `origin/main` changes while working, fetch and rebase/merge carefully; never force-push over teammates' changes.
- Do not start work on the browser capture/front-end teammate's files unless explicitly asked.

Recommended local start:

```bash
git clone https://github.com/echo-222/Magpie.git
cd Magpie
git switch -c chenbin/mvp-core
```

If the repository is already cloned, fetch first and create/switch to the branch from the latest `origin/main`.

The execution spec should live in the repository under:

```text
docs/Magpie_MVP_Spec_Agent_Handoff.md
```

---

## 0.2 Open-source reuse and license gate

"Do not reinvent wheels" does **not** mean "copy any GitHub code blindly."

Before copying source code from another project:

1. Check its current license.
2. Prefer permissive dependencies or codebases (MIT / Apache-2.0 / BSD) when direct reuse is useful.
3. Preserve required copyright / license notices.
4. Record reused components and licenses in project documentation.
5. If license compatibility is unclear, use the project only as an architectural/product reference until Human approval.

Current guidance for projects mentioned in this spec:

- **Linkwarden Browser Extension — MIT:** suitable as a front-end/browser-extension reference and potentially as a code starting point, with license obligations preserved.
- **Karakeep — AGPL-3.0:** study architecture, ingestion, jobs, search and API/MCP patterns; do not copy its core into Magpie by default.
- **TagStudio — GPL-3.0-only:** study file/metadata modeling; do not copy its core into Magpie by default.
- **Arkiv — PolyForm Perimeter:** architecture/pipeline reference only for this project unless Human explicitly approves a compatible use.

Prefer installing mature libraries as dependencies over copying whole applications.

---

## 0.3 Implementation discipline

Phase 1 is a **vertical-slice product validation**, not an infrastructure project.

Build in this order:

### Gate A — one-material vertical slice
One real image/text item can be ingested, persisted, analyzed, displayed, and exported through the same Material model.

### Gate B — small Material Memory
A small real library can be searched/retrieved using Human Thought + machine understanding.

### Gate C — differentiated loop
A real Task produces a Material Pack with task-specific reasons; a Human edit persists; `Copy for Agent` produces usable context.

Do not spend days completing all ingestion/analysis infrastructure before attempting Gate C.

Do not introduce Docker in Phase 1 unless the existing repository already requires it or there is a documented blocker that cannot reasonably be solved without it.

Secrets/API keys:
- never hard-code them;
- use environment variables and an `.env.example`;
- keep model/provider calls behind a small adapter where practical;
- plumbing may use mocks/fallbacks temporarily, but the final three Task → Pack validation runs must use real analysis/retrieval rather than fake outputs.

---


Your goal is to produce the thinnest end-to-end prototype that can prove the core product hypothesis:

> A person can save something that caught their attention, preserve both the material and their own thought, let the system understand it, and later—when a real task appears—have Magpie retrieve and recompose relevant past materials into a usable Material Pack that can be copied/sent as context to another AI Agent.

### Hard constraint: DO NOT REINVENT WHEELS

Before implementing any commodity capability, first check whether a mature library or open-source project already solves it.

Reuse, adapt, or learn from existing solutions wherever legally and technically appropriate.

Do **not** spend project time building custom versions of:
- OCR
- speech recognition
- video decoding
- frame extraction
- image metadata parsing
- vector databases
- generic embedding pipelines
- generic browser-extension plumbing
- generic bookmark CRUD
- generic full-text search
- generic async job infrastructure unless absolutely necessary

Engineering attention should be concentrated on Magpie-specific product logic:
1. Material model
2. Human Thought as first-class context
3. Task understanding
4. Task-aware retrieval / matching
5. Recomposition into Material Packs
6. Human adjustment of those packs
7. Agent-ready context export / handoff

If a mature project already implements a low-level problem, prefer integration over invention.

---

# 1. Product definition

## 1.1 What Magpie is

Magpie is **not merely an AI bookmark manager**.

It is a personal creative/material memory layer.

The product should enable this loop:

1. A user encounters an interesting item while browsing.
2. The user quickly saves it.
3. Optionally, the user adds one short thought explaining why it caught their attention.
4. Magpie stores the original material, source information, the human thought, and machine-generated understanding.
5. Over time, these materials become a searchable personal material memory.
6. When the user begins a real task, Magpie understands that task.
7. Magpie retrieves past materials that may now be useful.
8. Instead of returning only search results, Magpie recomposes them into a task-specific **Material Pack**.
9. The user can remove, move, regroup, or ask for alternatives.
10. Magpie preserves those human decisions.
11. The pack can be exported as Agent-readable context for ChatGPT, Claude, Codex, Cursor, or other Agents.

## 1.2 Core product thesis

Traditional bookmark tools optimize for:

> “I saved this before. Help me find it again.”

Magpie should optimize for:

> “I am doing this now. Which things I saved before can help me now, and how should they be assembled for this task?”

The product's differentiated value begins **after retrieval**.

---

# 2. Product pipeline

Target product pipeline:

```text
CAPTURE
web material
  ↓
quick save
  ↓
optional Human Thought
(text first; voice can follow)

UNDERSTAND
  ↓
original file/source
  ↓
objective metadata
  ↓
OCR / vision / embedding
  ↓
Material Memory

USE
  ↓
user enters a real task
  ↓
Task Understanding
  ↓
retrieve candidates
  ↓
task-aware Match reasoning
  ↓
Recompose
  ↓
Material Pack
  ↓
Human edits / corrections
  ↓
persist pack

HANDOFF
  ↓
Copy for Agent / Share Context
  ↓
Agent-readable Markdown / JSON / material references
```

---

# 3. Key objects

## 3.1 Material

A Material is not just a bookmark.

Minimum conceptual structure:

```json
{
  "id": "mat_xxx",
  "modality": "image | text",

  "original": {
    "file_or_content": "...",
    "mime_type": "...",
    "size": null
  },

  "source": {
    "page_url": "...",
    "resource_url": "...",
    "page_title": "...",
    "captured_at": "..."
  },

  "human": {
    "thought": "喜欢这里粗糙的纸张质感"
  },

  "objective_metadata": {
    "...": "..."
  },

  "analysis": {
    "summary": "...",
    "subjects": [],
    "ocr": "...",
    "colors": [],
    "style": [],
    "keywords": []
  },

  "provenance": {
    "human_fields": [],
    "file_fields": [],
    "model_fields": []
  },

  "embedding": "...",

  "processing": {
    "status": "ready"
  }
}
```

### Important

`human.thought` is a first-class field.

Do not treat it as an unimportant generic note.

Machine understanding answers:

> What is this?

Human Thought answers:

> Why did this matter to me when I saved it?

Both should participate in later retrieval and task matching.

---

## 3.2 Task

A Task represents what the user is currently trying to make or accomplish.

Example:

> 我要做一个克制、有物质感、不要典型 AI 蓝紫科技感的网站首页。

Minimum normalized structure may contain:

```json
{
  "raw_request": "...",
  "purpose": "...",
  "desired_qualities": [],
  "avoid": [],
  "constraints": [],
  "needed_reference_types": []
}
```

Do not over-engineer the ontology in MVP.

The Task object exists to support retrieval and recomposition.

---

## 3.3 Match

A Match is not only vector similarity.

It explains why a historical Material is useful **for this Task now**.

Example:

```text
Material:
粗纤维纸海报

Human Thought:
“喜欢这种纸张颗粒。”

Match role:
Material / texture reference

Why now:
The current task explicitly wants to reduce generic digital/AI visual language.
This reference introduces a physical texture and imperfect surface quality.
```

The system must distinguish:
- similarity
- task usefulness
- chosen role inside the pack

---

## 3.4 Material Pack

This is the core Magpie product object.

Example:

```text
Task: Magpie landing page

Mood
- mat_012
- mat_044

Material / Texture
- mat_003
- mat_081

Typography
- mat_023

Copy Tone
- mat_018
```

A pack must persist:
- pack name
- original/current task
- groups
- members
- member order
- match reason
- optional task-specific note
- later human changes

A material may appear in multiple packs and groups without duplication of the original material.

---

## 3.5 Agent Context

Agent Context is the export form of a Material Pack.

MVP output should support at minimum:

### Markdown

```markdown
# Task
...

## Human Direction
...

## Material / Texture
### Material 1
Human Thought:
...

Why selected:
...

Source:
...

## Typography
...
```

### Optional JSON

Provide a structured version of the same context when trivial to implement.

The MVP does NOT need native integrations with every AI product.

First prove:

> Copy for Agent

A user should be able to paste the output into ChatGPT / Claude / Codex and continue working.

---

# 4. MVP scope

## 4.1 The MVP is intentionally narrower than PRD V0.3

The full PRD includes:
- text
- images
- audio
- video
- screenshots
- share snapshots
- rich media processing
- segment-level video retrieval
- extensive failure handling

Do not implement the entire PRD in the first pass.

The MVP must first prove the differentiated loop.

### MVP modalities

Start with:

1. Images
2. Text

Video comes after the core loop works.

Audio can follow video.

Reason:

OCR, ASR, FFmpeg and video analysis are technically known problems.

The main unknown is whether:

> Task → historical material retrieval → recomposition → human adjustment → Agent context

creates a materially better product experience.

---

# 5. MVP end-to-end acceptance scenario

The prototype is successful if the following demo works:

1. Import or save an image.
2. Add a Human Thought:
   - “喜欢这个纸张质感。”
3. Magpie stores the material.
4. Magpie generates basic image understanding:
   - dimensions
   - OCR if relevant
   - dominant colors
   - short description
   - style / keywords
   - embedding
5. Repeat with enough real materials to create a small personal library.
6. Enter a task:
   - “我要做一个没有典型科技公司味、克制、有物质感的 AI 产品首页。”
7. Magpie retrieves relevant historical materials.
8. Magpie forms a Material Pack with a small number of useful groups.
9. Each selected item has a short explanation of why it helps this task.
10. User can:
   - Remove an item
   - Move an item to another group
   - Ask for alternatives / “find more like this”
11. The modified pack persists.
12. User clicks:
   - `Copy for Agent`
13. Magpie outputs usable Markdown context.

If this loop works, the MVP succeeds even if the UI is ugly.

---

# 6. Phase 1 — CURRENT ASSIGNMENT FOR THE AGENT

## 6.1 Objective

Implement the **backend/core prototype** for the differentiated half of Magpie.

Do not wait for the browser-extension/front-end teammate.

Use local test materials first.

### Phase 1 must produce:

```text
local real materials
    ↓
Material ingestion
    ↓
basic analysis
    ↓
persistence
    ↓
search/retrieval
    ↓
Task input
    ↓
task-aware candidate selection
    ↓
Material Pack
    ↓
simple human edits
    ↓
Copy for Agent
```

---

## 6.2 Test dataset

Create or use a folder such as:

```text
demo_materials/
  images/
  texts/
```

Start with approximately **12–30 real materials that a human genuinely finds interesting**, not random benchmark images. Once the vertical slice works, scale toward **30–100** for meaningful human validation.

Reason:

The product cannot be evaluated with meaningless test data.

The evaluator must be able to say:

> “Yes, this is actually the thing I would want Magpie to bring back for this task.”

Allow simple manual import.

---

## 6.3 Minimum backend capabilities

### A. Ingest Material

Support:
- image upload/import
- text input/import
- Human Thought
- optional source metadata

Minimum endpoint/interface concept:

```text
POST /materials
GET /materials/:id
GET /materials
```

Do not obsess over API design if a simpler local interface is faster.

---

### B. Persist Material

For MVP, choose the simplest stable storage.

Acceptable:
- SQLite
- PostgreSQL if already convenient

Files may be stored locally.

Do not build distributed/object-storage infrastructure yet.

Persist separately:
- original content/file
- source fields
- Human Thought
- machine analysis
- processing status

Do not overwrite human-authored fields with model output.

---

### C. Analyze images

Use mature dependencies.

Minimum useful output:
- file format
- width / height
- OCR when useful
- dominant colors + approximate proportion
- concise scene/content description
- a few useful visual/style keywords
- embedding

Avoid giant taxonomies.

Do not create an “aesthetic quality score.”

---

### D. Analyze text

Minimum:
- preserve original text
- short summary
- keywords/topics
- embedding

Human Thought remains separate.

---

### E. Retrieval

Support:
- basic metadata/full-text query when convenient
- semantic similarity
- retrieval over both machine understanding and Human Thought

MVP ranking does not need academic perfection.

The purpose is to produce useful candidates for Task recomposition.

---

### F. Task → Pack

Implement the first version of Magpie-specific logic.

Input:
```text
我要做一个克制、有物质感、不要典型 AI 蓝紫色的网站首页。
```

System:
1. Parse the task into a small useful representation.
2. Retrieve candidate materials.
3. Re-rank/choose candidates based on task usefulness.
4. Assign candidates to a few task-specific groups.
5. Generate a short match reason.
6. Create and persist a Material Pack.

Do not force fixed global groups.

Groups should reflect the task.

For a visual task they may be:
- Mood
- Material / Texture
- Color
- Typography
- Copy Tone

But do not hard-code a universal taxonomy unless needed for the first prototype.

---

### G. Human adjustment

MVP operations:

```text
remove item
move item to group
find alternatives / more like this
```

Persist the resulting pack state.

Human changes must not be silently undone by regeneration.

---

### H. Copy for Agent

Generate clean Markdown from the current pack.

It must contain:
- task
- human direction / constraints
- groups
- selected materials
- Human Thought when available
- why each material was selected
- source/reference when available

The output should be immediately usable as context in another Agent.

---

# 7. UI requirement for Phase 1

The backend prototype needs only a minimal development UI.

No visual polish is required.

One lightweight page is enough. Do not introduce a full SPA/design-system stack solely for this Phase 1 dev UI.

Suggested layout:

```text
--------------------------------------------------
MAGPIE MVP

[ Import Material ]

Human Thought:
[________________________________]

[ Add ]

--------------------------------------------------

Material Library

[thumbnail] poster01.jpg
Human: 喜欢这个粗糙纸张感
AI: warm / paper / low saturation / poster

...

--------------------------------------------------

What are you making?

[ 我要做一个克制、有物质感的 AI 网站首页 ]

[ Build Material Pack ]

--------------------------------------------------

PACK

Material / Texture
[1] poster01   [Remove] [Move]
Reason: ...

Typography
...

[ Find alternatives ]

[ Copy for Agent ]
--------------------------------------------------
```

Ugly but functional is desirable.

Do not spend Phase 1 building a full design system.

---

# 8. Open-source / mature technology reuse policy

## 8.1 Browser capture layer — front-end teammate

Primary reference / possible starting point:

### Linkwarden Browser Extension
Repository:
https://github.com/linkwarden/browser-extension

Why:
- mature browser-extension skeleton
- official Linkwarden extension
- TypeScript
- existing save flow / screenshot / authentication / API communication
- MIT license

Use it to avoid rebuilding generic browser-extension plumbing.

The teammate may adapt:
- extension structure
- manifest/background logic
- popup patterns
- API communication

Magpie-specific work remains:
- selected text/image capture
- richer element selection
- original media capture where feasible
- Human Thought input
- later voice Thought
- Magpie-specific quick-save flow

---

## 8.2 Bookmark / async/search architecture reference

### Karakeep
Repository:
https://github.com/karakeep-app/karakeep

Use primarily as an architecture reference for:
- ingestion
- processing states
- background enrichment
- indexing
- search
- API/MCP exposure

Important:
Karakeep's core product model is bookmark-centric.
Do not force Magpie into its data model.

License:
AGPL-family licensing must be respected.
Do not blindly copy the core code into a product that may require different licensing.

---

## 8.3 Local file / metadata modeling reference

### TagStudio
Repository:
https://github.com/TagStudioDev/TagStudio

Study:
- file entry model
- metadata separation
- database overlay over original files
- tag/metadata relationships

Borrow ideas, not unnecessary complexity.

---

## 8.4 Video pipeline reference — later phase

### Arkiv
Repository:
https://github.com/vulture-s/arkiv

Use as a conceptual implementation reference for:
- FFmpeg pipeline
- transcript + vision analysis
- media metadata
- semantic retrieval

Do not make video the first MVP dependency.

Observe license restrictions before copying code.

---

## 8.5 Commodity dependencies

Prefer existing mature tools:

### Image
- Pillow
- OpenCV

### OCR
- PaddleOCR or another mature OCR system

### Audio / later video
- FFmpeg
- Whisper / WhisperX

WhisperX:
https://github.com/m-bain/whisperX

### Vector retrieval
For MVP:
- simplest vector store available

If using PostgreSQL:
- pgvector
https://github.com/pgvector/pgvector

Do not build a vector database.

---

# 9. Anti-goals

The Agent MUST NOT spend Phase 1 on:

- polished UI
- complete Chrome extension
- video ingestion
- audio ingestion
- microservices
- Docker-heavy infrastructure unless unavoidable
- Kubernetes
- custom OCR
- custom ASR
- custom vector database
- complicated multi-Agent architecture
- generic Agent framework
- perfect tagging taxonomy
- giant ontology
- role/user permission system
- billing
- collaboration
- production-grade sharing
- mobile app
- elaborate animation
- “AI aesthetic score”
- exhaustive support for every file type
- full PRD error matrix

If such work starts to dominate, stop and return to the differentiated MVP loop.

---

# 10. Architecture principle

The system should remain modular enough that the browser capture layer can connect later.

Keep a clean boundary:

```text
CAPTURE LAYER
(front-end teammate)
        ↓
Material input contract
        ↓
PROCESSING / MEMORY LAYER
(Phase 1 current work)
        ↓
TASK / RECOMPOSITION LAYER
(Magpie core)
        ↓
AGENT CONTEXT OUTPUT
```

Expected capture payload concept:

```json
{
  "modality": "image",
  "source": {
    "page_url": "...",
    "resource_url": "...",
    "page_title": "...",
    "captured_at": "..."
  },
  "human": {
    "thought": "喜欢这里的空间尺度差"
  }
}
```

Plus the original file/content.

The capture teammate should eventually be able to send this without knowing how analysis works.

The backend should not depend on the browser UI.

---

# 11. Front-end teammate parallel assignment

The front-end/capture teammate should work in parallel.

The current backend/core Agent must not implement this browser-extension track unless Human explicitly reassigns it.

Their MVP is:

```text
web page
  ↓
keyboard shortcut
  ↓
capture selected text OR image
  ↓
small preview
  ↓
Human Thought input
  ↓
Save
```

They should NOT wait for the backend.

Mock the save API if necessary.

Primary goals:
- capture feels fast
- minimal interruption
- Human Thought is easy to add
- success/failure is clear

Do not build the full library UI yet.

When both tracks are ready, connect via the agreed Material input contract.

---

# 12. Phase 1 completion criteria

Phase 1 is done only when there is a working demo, not when infrastructure exists.

Required demonstration:

1. A real image/text material is imported.
2. Human Thought is saved separately.
3. Machine analysis appears.
4. Several dozen real materials exist in the library.
5. User enters a real task.
6. Relevant old materials are retrieved.
7. Magpie creates a Material Pack.
8. Selected materials have task-specific reasons.
9. User removes or moves at least one item.
10. Pack state persists.
11. `Copy for Agent` produces usable Markdown.

Do not claim completion because:
- embeddings work
- database works
- OCR works
- API endpoints exist

Those are implementation details, not product proof.

---

# 13. After Phase 1

STOP after Phase 1 and report:

1. What is actually implemented.
2. Which open-source/library components were reused.
3. What was written specifically for Magpie.
4. The current architecture.
5. A demo procedure.
6. Known limitations.
7. Evidence from at least 3 real Task → Material Pack tests.
8. Which parts felt useful vs generic.
9. Any areas where the implementation started reinventing an existing solution.
10. Recommended Phase 2, but DO NOT begin Phase 2 until human review.

The human will review and coordinate with the capture/front-end teammate before further implementation.

---

# 14. Future direction after MVP validation

Only after the core loop is validated, expand toward the full PRD:

- browser capture integration
- voice Human Thought
- audio
- video
- timestamped transcript
- keyframes
- segment-level material references
- better hybrid retrieval
- share snapshots
- revocable share links
- direct Agent/MCP integrations
- historical pack retrieval
- richer failure recovery
- production-level storage

The future product may expose Agent tools such as:

```text
search_materials()
get_material()
get_pack()
create_pack()
update_pack()
```

But MCP/native Agent integration is not required for the first MVP.

First prove that the Agent Context is worth sending.

---

# 15. Final priority rule

When choosing between:

A. making infrastructure more sophisticated  
B. testing whether Magpie can bring the right old material back into a real new task

always choose **B**.

When choosing between:

A. implementing a commodity technical capability from scratch  
B. integrating a mature existing solution

always choose **B**, unless there is a clearly documented reason not to.

The purpose of this MVP is not to prove that we can build OCR, vector search, browser extensions, or media pipelines.

The purpose is to prove:

> **Can Magpie turn scattered personal inspiration into usable memory, and then turn that memory into relevant context at the moment of creation?**
