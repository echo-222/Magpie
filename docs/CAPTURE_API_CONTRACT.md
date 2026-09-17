# Magpie Capture API Contract

Status: **verified against the running code** on branch `chenbin/mvp-core` (2026-09-17).
Every request/response below was produced by real HTTP calls to the local Core; nothing is
taken from the spec or guessed. Interactive reference (same routes, auto-generated):
`http://127.0.0.1:8765/docs`.

## 1. Purpose / Boundary

Capture UI (browser extension, this contract's consumer) is responsible for:

```
web page → shortcut / capture action → selected text OR image
→ Human Thought input (one short line, optional) → Save → POST to Magpie Core
```

Magpie Core (this repo) is responsible for everything after Save: storing the original,
keeping the Human Thought as a first-class field, image/text analysis, OCR, embeddings,
retrieval, task understanding, Material Pack recomposition, exports. **The Capture UI never
needs to know about DeepSeek / Qwen / OCR / embeddings / vector search / packs.** It only
needs the four calls in §3–§6, and only the first two on the Save path.

## 2. Base URL

```
http://127.0.0.1:8765
```

Started with `.venv/bin/magpie serve` (binds `127.0.0.1:8765`; `--host/--port` to change).
Use `127.0.0.1`, not `localhost` (the server does not listen on IPv6 `::1`).
Health check: `GET /health` → `200 {"version": "0.1.0", "materials": 31, ...}`.

## 3. Image Material API

| | |
|---|---|
| Method / path | `POST /materials` |
| Content-Type | `multipart/form-data` (let the client set the boundary; do **not** set the header manually with `FormData`) |
| Image field | `file` — the image bytes as a file part, with a filename (`poster.jpg`, `image.png`, …) |
| Required | `file`. `modality=image` is recommended (it is inferred as `image` when a file part is present, but send it explicitly) |
| Optional | `thought`, `page_url`, `resource_url`, `page_title`, `captured_at`, `sync` |
| Success | `201 Created` with `{ "created": true, "material": Material }` |
| Duplicate bytes | `200 OK` with `{ "created": false, "material": <the existing Material> }` — same image bytes (SHA-256) already in the library; **the existing thought is kept, the new one is ignored** |

Field names are the real ones in `magpie/api.py` (the spec's nested `source.*` / `human.thought`
are flattened for multipart):

| form field | maps to | notes |
|---|---|---|
| `file` | `material.original` (file stored under `data/files/<id>.<ext>`) | JPEG / PNG / WebP / GIF / BMP / TIFF bytes; format is detected from the bytes, the extension in the filename is used when it is a known image extension |
| `modality` | `material.modality` | `"image"` or `"text"` — anything else → 422 |
| `thought` | `material.human.thought` | the Human Thought; empty/whitespace → stored as `null` |
| `page_url` | `material.source.page_url` | the page the user was on |
| `resource_url` | `material.source.resource_url` | the image's own URL (`<img src>`) |
| `page_title` | `material.source.page_title` | `document.title` |
| `captured_at` | `material.source.captured_at` | free string; send ISO 8601 (`2026-09-17T19:40:00+08:00`). If omitted the server stores UTC now |
| `sync` | — | `"true"` = run analysis inside the request (10–30 s). Default `false`; not recommended for the extension |

Minimum a browser extension has to send for a web image: `file` (+ `modality=image`).
Send `page_url`, `resource_url`, `page_title` and `thought` whenever you have them — they are
what makes the material useful later, and the Core cannot recover them afterwards.

The Core does **not** download `resource_url` itself in this phase: the extension fetches the
image bytes (see §9) and uploads them.

Real response (201, right after Save — analysis has not run yet):

```json
{
  "created": true,
  "material": {
    "id": "mat_e0fcf901e1",
    "modality": "image",
    "original": {
      "file_path": "mat_e0fcf901e1.jpg",
      "content": null,
      "mime_type": "image/jpeg",
      "size": 180620,
      "sha256": "d79267b3fdb4cfbe0fda221d42e429cb4c43aacf1bb787c0ef7576fcf5707d69",
      "filename": "barbican_crop.jpg"
    },
    "source": {
      "page_url": "https://commons.wikimedia.org/wiki/File:Barbican_Concrete_finish_(8085917893).jpg",
      "resource_url": "https://upload.wikimedia.org/wikipedia/commons/5/56/Barbican_Concrete_finish_%288085917893%29.jpg",
      "page_title": "Barbican Concrete finish - Wikimedia Commons",
      "captured_at": "2026-09-17T19:40:00+08:00",
      "license": null
    },
    "human": { "thought": "剁斧混凝土的局部：粗糙但有节奏，想拿来做首页的分隔区块背景。" },
    "objective_metadata": {},
    "analysis": { "summary": null, "subjects": [], "ocr": null, "colors": [], "style": [], "keywords": [], "mood": [], "model": null, "ocr_engine": null, "analyzed_at": null },
    "provenance": { "human_fields": ["human.thought"], "file_fields": ["original"], "machine_fields": [] },
    "processing": { "status": "pending", "error": null, "updated_at": null },
    "created_at": "2026-09-17T11:46:15+00:00",
    "updated_at": "2026-09-17T11:46:15+00:00"
  }
}
```

## 4. Text Material API

Two equivalent ways; the JSON one is the cleaner fit for an extension.

**A. JSON** — `POST /materials`, `Content-Type: application/json`, body = `CapturePayload`:

| JSON field | required | notes |
|---|---|---|
| `modality` | yes | `"text"` |
| `content` | yes | the selected text, verbatim. Whitespace-only → 400 |
| `human.thought` | no | the Human Thought |
| `source.page_url` | no | page URL |
| `source.page_title` | no | `document.title` |
| `source.resource_url` | no | usually omitted for text |
| `source.captured_at` | no | ISO 8601; server fills UTC now if omitted |
| `source.license` | no | exists in the model; leave out |
| `sync` | no | default `false` |

Real request:

```json
{
  "modality": "text",
  "content": "Letterpress printing is a technique of relief printing for producing many copies by repeated direct impression of an inked, raised surface against individual sheets of paper or a continuous roll of paper.",
  "human": { "thought": "“直接压印”这个动作本身就是物质感的来源，网页里能不能有类似的压痕？" },
  "source": {
    "page_url": "https://en.wikipedia.org/wiki/Letterpress_printing",
    "page_title": "Letterpress printing - Wikipedia",
    "captured_at": "2026-09-17T19:41:00+08:00"
  }
}
```

Real response (201):

```json
{
  "created": true,
  "material": {
    "id": "mat_430ee59229",
    "modality": "text",
    "original": {
      "file_path": null,
      "content": "Letterpress printing is a technique of relief printing for producing many copies by repeated direct impression of an inked, raised surface against individual sheets of paper or a continuous roll of paper.",
      "mime_type": "text/plain",
      "size": 204,
      "sha256": "d1dcb9a8b2c18825bce165abec8e6b2e3aed380f692196b12b62e25288fa6a41",
      "filename": null
    },
    "source": {
      "page_url": "https://en.wikipedia.org/wiki/Letterpress_printing",
      "resource_url": null,
      "page_title": "Letterpress printing - Wikipedia",
      "captured_at": "2026-09-17T19:41:00+08:00",
      "license": null
    },
    "human": { "thought": "“直接压印”这个动作本身就是物质感的来源，网页里能不能有类似的压痕？" },
    "processing": { "status": "pending", "error": null, "updated_at": null },
    "created_at": "2026-09-17T11:36:50+00:00"
  }
}
```
(`objective_metadata`, `analysis`, `provenance`, `updated_at` are present too, same as §3.)

**B. multipart/form-data** (same endpoint, no `file` part): `modality=text`, `content=…`,
`thought=…`, `page_url=…`, `page_title=…`, `captured_at=…`. Verified: returns the same shape, 201.

Identical text (same bytes) already saved → `200 {"created": false, ...}` with the existing material.

## 5. Get Material

| | |
|---|---|
| Method / path | `GET /materials/{material_id}` |
| Path param | `material_id` — the `material.id` returned by Save (`mat_` + 10 hex chars) |
| Success | `200` with the full `Material` object (no wrapper) |
| Unknown id | `404 {"detail": "material not found"}` |

`Material` fields the Capture UI may care about:

| field | type | meaning |
|---|---|---|
| `id` | string | `mat_…` |
| `modality` | `"image"` \| `"text"` | |
| `original.content` | string \| null | the saved text (text materials) |
| `original.filename`, `original.mime_type`, `original.size` | | the uploaded image (image materials) |
| `source.page_url`, `source.resource_url`, `source.page_title`, `source.captured_at` | string \| null | exactly what was sent |
| `human.thought` | string \| null | the Human Thought, never modified by the Core |
| `processing.status` | `"pending"` \| `"analyzing"` \| `"ready"` \| `"failed"` | see §6 |
| `processing.error` | string \| null | set when `failed` |
| `analysis.summary` | string \| null | one–two sentence description (Chinese for images) once `ready` |
| `analysis.style`, `analysis.keywords`, `analysis.mood`, `analysis.subjects` | string[] | once `ready` |
| `analysis.ocr` | string \| null | text found in the image, once `ready` |
| `analysis.colors` | `[{hex, ratio, name}]` | dominant colours (images) |
| `objective_metadata` | object | width/height/format/palette (images), char_count/language (text) |
| `provenance` | object | which fields came from the human / the file / a model |
| `created_at`, `updated_at` | ISO 8601 UTC | |

Real `GET /materials/mat_e0fcf901e1` after analysis (abridged):

```json
{
  "id": "mat_e0fcf901e1",
  "modality": "image",
  "original": { "file_path": "mat_e0fcf901e1.jpg", "mime_type": "image/jpeg", "size": 180620, "filename": "barbican_crop.jpg", "content": null, "sha256": "d792…" },
  "source": { "page_url": "https://commons.wikimedia.org/wiki/File:Barbican_Concrete_finish_(8085917893).jpg", "resource_url": "https://upload.wikimedia.org/…/Barbican_Concrete_finish_%288085917893%29.jpg", "page_title": "Barbican Concrete finish - Wikimedia Commons", "captured_at": "2026-09-17T19:40:00+08:00", "license": null },
  "human": { "thought": "剁斧混凝土的局部：粗糙但有节奏，想拿来做首页的分隔区块背景。" },
  "objective_metadata": { "format": "JPEG", "width": 700, "height": 500, "mode": "RGB", "file_size": 180620, "aspect_ratio": 1.4, "palette": { "descriptors": ["low saturation", "mid-tone", "warm", "near-monochrome"], "...": "..." } },
  "analysis": {
    "summary": "这张图片展示了一块碎石地面，表面不平整，有大小不一的碎石和泥土混合在一起。",
    "subjects": ["碎石", "泥土", "地面"],
    "ocr": null,
    "colors": [{ "hex": "#a89f91", "ratio": 0.199, "name": "warm neutral" }, { "hex": "#858078", "ratio": 0.192, "name": "grey" }, "..."],
    "style": ["自然纹理", "粗糙质感", "自然构图", "中性色调"],
    "keywords": ["碎石地面", "自然纹理", "户外场景", "泥土质感", "自然元素", "中性色调", "户外设计", "自然背景"],
    "mood": ["自然", "质朴"],
    "model": "qwen2.5vl:7b", "ocr_engine": null, "analyzed_at": "2026-09-17T11:46:24+00:00"
  },
  "provenance": { "human_fields": ["human.thought"], "file_fields": ["original", "objective_metadata", "analysis.colors"], "machine_fields": ["analysis.summary", "analysis.subjects", "analysis.style", "analysis.keywords", "analysis.mood"] },
  "processing": { "status": "ready", "error": null, "updated_at": "2026-09-17T11:46:24+00:00" },
  "created_at": "2026-09-17T11:46:15+00:00",
  "updated_at": "2026-09-17T11:46:24+00:00"
}
```

Related, optional: `GET /materials` (list, newest first, `{items, total}`),
`GET /materials/{id}/thumb` (JPEG thumbnail, available once analysed; falls back to the
original), `GET /materials/{id}/file` (original bytes), `PATCH /materials/{id}/thought`
with `{"thought": "..."}` (edit the thought later), `DELETE /materials/{id}`.

## 6. Processing Status

Save is **synchronous for persistence, asynchronous for analysis**:

1. `POST /materials` stores the original file/text, the Human Thought and the source
   metadata, then returns immediately (`201`, `processing.status = "pending"`). At this
   point the material is in the database — **Save has succeeded**. Measured: ~0.1–0.3 s.
2. Analysis runs in the background inside the Core (one material at a time):
   `pending → analyzing → ready` or `failed`. Measured on the dev machine: image ≈ 10 s
   (local vision model + OCR + embedding), text ≈ 2–4 s.
3. On `failed`, `processing.error` holds the reason; the material, its thought and source are
   still stored. `POST /materials/{id}/reanalyze` (no body) queues another attempt.

Real status values: `"pending"`, `"analyzing"`, `"ready"`, `"failed"` (nothing else).

What the Capture UI should do:
- On `201`/`200`: show "已保存" (saved). Nothing more is required — the extension's job is done.
- Optional: poll `GET /materials/{id}` every **2 s** until `status` is `ready` or `failed`
  to show "已分析" / the AI summary. Stop polling after ~60 s and leave it at "分析中".
- `sync=true` makes the POST wait for analysis (10–30 s). Fine for scripts, not for the
  popup.

## 7. Error Responses

All observed on the running server. Bodies are JSON.

| case | status | body | how to read it |
|---|---|---|---|
| image material without a file part | `400` | `{"detail": "image material needs a file"}` | client bug: attach `file` |
| text material with empty/whitespace `content` | `400` | `{"detail": "text material needs content"}` | nothing selected; do not save |
| `file` bytes are not a decodable image | `400` | `{"detail": "file is not a decodable image (notes.txt); send JPEG/PNG/WebP/GIF bytes"}` | the fetched resource was not an image (e.g. an HTML error page); nothing was stored |
| unsupported `modality` (e.g. `video`) | `422` | `{"detail": [{"loc": ["modality"], "msg": "Input should be 'image' or 'text'", "type": "literal_error"}]}` | only `image` / `text` exist in this phase |
| JSON body missing a required field | `422` | `{"detail": [{"loc": ["modality"], "msg": "Field required", "type": "missing"}]}` | schema error; `loc` names the field |
| JSON body is not an object / invalid JSON | `422` | `{"detail": [{"loc": ["body"], "msg": "body must be a JSON object (CapturePayload)", "type": "type_error"}]}` / `{"detail": [{"loc": ["body"], "msg": "invalid JSON: …", "type": "json_invalid"}]}` | serialisation bug on the client |
| unknown material id | `404` | `{"detail": "material not found"}` | |
| analysis failed (after a successful Save) | `200` on `GET` | `"processing": {"status": "failed", "error": "cannot identify image file …"}` | material is saved; show "分析失败"; optionally `POST /materials/{id}/reanalyze` |
| duplicate bytes | `200` | `{"created": false, "material": …}` | already in the library; the earlier thought is kept |
| Core not running | — | browser: `TypeError: Failed to fetch`; curl: `Connection refused` | tell the user to start `magpie serve` |

Unhandled server errors would be `500 Internal Server Error` (plain text); none were hit in
the smoke run after the fixes in this patch.

## 8. curl Examples

Image (multipart) — run from any directory, replace the file path:

```bash
curl -X POST http://127.0.0.1:8765/materials \
  -F "modality=image" \
  -F "file=@/path/to/barbican_crop.jpg" \
  -F "thought=剁斧混凝土的局部：粗糙但有节奏，想拿来做首页的分隔区块背景。" \
  -F "page_url=https://commons.wikimedia.org/wiki/File:Barbican_Concrete_finish_(8085917893).jpg" \
  -F "resource_url=https://upload.wikimedia.org/wikipedia/commons/5/56/Barbican_Concrete_finish_%288085917893%29.jpg" \
  -F "page_title=Barbican Concrete finish - Wikimedia Commons" \
  -F "captured_at=2026-09-17T19:40:00+08:00"
```

Text (JSON):

```bash
curl -X POST http://127.0.0.1:8765/materials \
  -H "Content-Type: application/json" \
  -d '{
    "modality": "text",
    "content": "Letterpress printing is a technique of relief printing for producing many copies by repeated direct impression of an inked, raised surface against individual sheets of paper or a continuous roll of paper.",
    "human": {"thought": "“直接压印”这个动作本身就是物质感的来源，网页里能不能有类似的压痕？"},
    "source": {
      "page_url": "https://en.wikipedia.org/wiki/Letterpress_printing",
      "page_title": "Letterpress printing - Wikipedia",
      "captured_at": "2026-09-17T19:41:00+08:00"
    }
  }'
```

Read it back / poll:

```bash
curl http://127.0.0.1:8765/materials/mat_e0fcf901e1
curl http://127.0.0.1:8765/materials/mat_e0fcf901e1 | python3 -c 'import sys,json; print(json.load(sys.stdin)["processing"])'
```

## 9. Browser Extension fetch Examples

Run these from the extension's **background service worker** (see §10). `MAGPIE` is the base URL.

Image — the extension fetches the image bytes itself, then uploads them:

```javascript
const MAGPIE = "http://127.0.0.1:8765";

async function saveImage({ imageUrl, pageUrl, pageTitle, thought }) {
  // 1. get the bytes of the selected <img> (needs host_permissions for that site, see §10)
  const imgResp = await fetch(imageUrl);
  if (!imgResp.ok) throw new Error(`image fetch failed: ${imgResp.status}`);
  const blob = await imgResp.blob(); // e.g. image/jpeg

  // 2. build the multipart body — do NOT set Content-Type yourself
  const form = new FormData();
  form.append("modality", "image");
  form.append("file", blob, guessFilename(imageUrl, blob.type)); // filename with an image extension
  form.append("thought", thought ?? "");
  form.append("page_url", pageUrl);
  form.append("resource_url", imageUrl);
  form.append("page_title", pageTitle ?? "");
  form.append("captured_at", new Date().toISOString());

  // 3. save
  const response = await fetch(`${MAGPIE}/materials`, { method: "POST", body: form });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail?.[0]?.msg ?? body.detail ?? `HTTP ${response.status}`);
  return body.material; // body.created === false => same image was already in the library
}

function guessFilename(url, mime) {
  const fromUrl = new URL(url).pathname.split("/").pop() || "";
  if (/\.(jpe?g|png|webp|gif|bmp|tiff?)$/i.test(fromUrl)) return fromUrl;
  const ext = { "image/png": "png", "image/webp": "webp", "image/gif": "gif" }[mime] ?? "jpg";
  return `capture.${ext}`;
}
```

Text:

```javascript
async function saveText({ text, pageUrl, pageTitle, thought }) {
  const response = await fetch(`${MAGPIE}/materials`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      modality: "text",
      content: text,
      human: { thought: thought ?? null },
      source: { page_url: pageUrl, page_title: pageTitle ?? null, captured_at: new Date().toISOString() },
    }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail?.[0]?.msg ?? body.detail ?? `HTTP ${response.status}`);
  return body.material;
}
```

Optional status polling (after Save succeeded):

```javascript
async function waitForAnalysis(materialId, { everyMs = 2000, maxMs = 60000 } = {}) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    const m = await (await fetch(`${MAGPIE}/materials/${materialId}`)).json();
    if (m.processing.status === "ready" || m.processing.status === "failed") return m;
    await new Promise((r) => setTimeout(r, everyMs));
  }
  return null; // still analysing; fine to leave it
}
```

Error handling that matches §7: `response.status === 400` → show `body.detail` (a string);
`422` → `body.detail` is an array, show `detail[0].msg`; network error → "Magpie Core 未启动".

## 10. CORS / Chrome Extension Notes

- The Core now sends permissive dev-stage CORS headers on every response:
  `Access-Control-Allow-Origin: *`, all methods and headers, preflight `OPTIONS` → `200`
  (`Access-Control-Max-Age: 600`). No credentials/cookies are used anywhere.
  Configurable with `MAGPIE_CORS_ORIGINS` (comma-separated origins; default `*`).
- Verified in a real Chromium page context: from `https://example.com` a `GET /health`,
  a JSON `POST /materials` (with preflight) and a multipart `POST /materials` all succeeded
  (`200` / `201` / `201`).
- Manifest V3 extension: add the Core to `host_permissions`, and also the sites whose
  images you will fetch:

  ```json
  {
    "manifest_version": 3,
    "permissions": ["activeTab", "scripting", "storage"],
    "host_permissions": ["http://127.0.0.1:8765/*", "<all_urls>"],
    "background": { "service_worker": "background.js" }
  }
  ```

  `<all_urls>` (or a narrower pattern) is what lets the background worker `fetch()` the
  selected image's bytes from third-party sites; `http://127.0.0.1:8765/*` covers the Core.
- Do the network calls in the **background service worker**. Content scripts run inside the
  page's origin and are subject to the page's CORS/private-network rules; send the capture
  payload from the content script with `chrome.runtime.sendMessage`, and let the worker call
  the Core. Popup pages may also call the Core directly (extension origin + host permission).
- Use `http://127.0.0.1:8765`, not `http://localhost:8765` (the server listens on IPv4 only).
- Local dev only. There is no auth, no HTTPS, no rate limiting; the Core is a single process
  bound to loopback. Do not expose it beyond the machine as-is.

## 11. Known Limitations

- **Core does not fetch `resource_url`**: the extension must upload the bytes. Images the page
  loads with credentials, or that block cross-origin fetches even for extensions, cannot be
  saved by URL alone (screenshot/canvas capture would be the fallback).
- **Duplicate by content**: identical bytes/text → `200 created:false`, the existing material
  is returned and the new thought is dropped. Use `PATCH /materials/{id}/thought` to change a
  thought.
- **Analysis is serialised** (one at a time, local vision model): a burst of saves is accepted
  immediately but analyses queue up (~10 s per image).
- **No batch endpoint, no base64 images in JSON**: one material per request; images must be
  multipart.
- **No size limit is enforced** on uploads; the Core downsizes images internally. Keep uploads
  reasonable (a few MB).
- `captured_at` is stored as the string you send; prefer ISO 8601 with timezone.
- `source.license` exists on the model (used by the demo library); the extension can ignore it.
- Only `image` and `text` materials exist in this phase (no video/audio/screenshot types).
- Single-user, no auth: anything on the machine can read/write the library.
