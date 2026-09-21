// Magpie Capture — background service worker.
//
// All HTTP to the Magpie Core lives here (content scripts are subject to the page's CORS).
// Contract: docs/CAPTURE_API_CONTRACT.md — POST /materials (JSON for text, multipart for
// images; the extension fetches the image bytes itself), GET /materials/{id}, PATCH thought.

const DEFAULT_CORE_URL = "http://127.0.0.1:8765";
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024; // Core enforces no limit; keep uploads sane
const MENU_TEXT = "magpie-capture-selection";
const MENU_IMAGE = "magpie-capture-image";

class CaptureError extends Error {
  constructor(kind, message, status) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

// ------------------------------------------------------------------ entry points

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU_TEXT, title: "收集到 Magpie：选中的文字", contexts: ["selection"] });
    chrome.contextMenus.create({ id: MENU_IMAGE, title: "收集到 Magpie：这张图片", contexts: ["image"] });
  });
});

// restricted pages are reported via the toolbar badge (flagRestricted); nothing else to do here
const swallow = () => {};

chrome.commands.onCommand.addListener(async (command, tab) => {
  if (command !== "capture") return;
  captureInTab(tab || (await activeTab()), {}).catch(swallow);
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!tab) return;
  if (info.menuItemId === MENU_IMAGE) captureInTab(tab, { mode: "image", srcUrl: info.srcUrl }).catch(swallow);
  else if (info.menuItemId === MENU_TEXT) captureInTab(tab, { mode: "text", selectionText: info.selectionText || "" }).catch(swallow);
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handler = HANDLERS[msg && msg.type];
  if (!handler) return false;
  handler(msg.payload || {}, sender)
    .then((result) => sendResponse({ ok: true, ...result }))
    .catch((e) => sendResponse({ ok: false, error: toErrorInfo(e) }));
  return true; // async response
});

const HANDLERS = {
  MAGPIE_SAVE_TEXT: saveText,
  MAGPIE_SAVE_IMAGE: saveImage,
  MAGPIE_GET_MATERIAL: getMaterial,
  MAGPIE_SET_THOUGHT: setThought,
  MAGPIE_DELETE_MATERIAL: deleteMaterial,
  MAGPIE_HEALTH: health,
  MAGPIE_OPEN_LIBRARY: openLibrary,
  MAGPIE_CAPTURE_ACTIVE_TAB: async () => {
    const tab = await activeTab();
    if (!tab) throw new CaptureError("no_tab", "没有可用的标签页");
    await captureInTab(tab, {});
    return {};
  },
  MAGPIE_SET_CORE_URL: async ({ coreUrl }) => {
    const url = String(coreUrl || "").trim().replace(/\/+$/, "") || DEFAULT_CORE_URL;
    await chrome.storage.local.set({ coreUrl: url });
    return { coreUrl: url };
  },
};

// ------------------------------------------------------------------ capture orchestration

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// Inject content.js (idempotent) and tell it to start. `hint` is forwarded as-is:
// {} → shortcut (selection or pick), {mode:"image", srcUrl} / {mode:"text", selectionText} → context menu.
async function captureInTab(tab, hint) {
  if (!tab || !tab.id) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "MAGPIE_START_CAPTURE", ...hint });
  } catch (e) {
    // chrome://, the Web Store, PDF viewer, file:// without permission … cannot run content scripts
    flagRestricted(tab.id, e);
    throw new CaptureError("restricted_page", "这个页面不允许扩展运行（chrome:// / 商店 / PDF 等）");
  }
}

function flagRestricted(tabId, e) {
  console.warn("[magpie] cannot capture in tab", tabId, e && e.message);
  try {
    chrome.action.setBadgeBackgroundColor({ tabId, color: "#b91c1c" });
    chrome.action.setBadgeText({ tabId, text: "!" });
    chrome.action.setTitle({ tabId, title: "Magpie：这个页面不允许扩展运行" });
    setTimeout(() => {
      chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
      chrome.action.setTitle({ tabId, title: "Magpie Capture" }).catch(() => {});
    }, 4000);
  } catch (_) {
    /* tab may be gone */
  }
}

// ------------------------------------------------------------------ Core HTTP

async function coreUrl() {
  const { coreUrl } = await chrome.storage.local.get("coreUrl");
  return (coreUrl || DEFAULT_CORE_URL).replace(/\/+$/, "");
}

async function coreFetch(path, init) {
  const base = await coreUrl();
  try {
    return await fetch(base + path, init);
  } catch (_) {
    // browser: TypeError: Failed to fetch → Core is not running
    throw new CaptureError("core_unreachable", `Magpie Core 未启动（${base}）`);
  }
}

async function httpError(resp) {
  let body = null;
  try {
    body = await resp.json();
  } catch (_) {
    /* non-JSON body (500 plain text) */
  }
  const detail = body && body.detail;
  let message;
  if (Array.isArray(detail)) message = (detail[0] && detail[0].msg) || JSON.stringify(detail[0]);
  else if (typeof detail === "string") message = detail;
  else message = `Core 返回 HTTP ${resp.status}`;
  const kind = resp.status === 400 ? "bad_request" : resp.status === 422 ? "validation" : resp.status === 404 ? "not_found" : "http";
  return new CaptureError(kind, message, resp.status);
}

// Text: application/json CapturePayload (contract §4A).
async function saveText({ content, thought, page_url, page_title, captured_at }) {
  if (!content || !content.trim()) throw new CaptureError("empty", "没有选中文字");
  const body = {
    modality: "text",
    content,
    human: { thought: cleanThought(thought) },
    source: { page_url: page_url || null, page_title: page_title || null, captured_at: captured_at || new Date().toISOString() },
  };
  const resp = await coreFetch("/materials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return finishSave(resp, cleanThought(thought));
}

// Image: multipart/form-data with the real bytes (contract §3, §9). The Core never downloads
// resource_url itself. `image_url` → fetched here (extension origin + host permission);
// `data_url` → bytes the content script already obtained in the page (blob:, same-origin
// fallback, or a user-approved canvas render).
async function saveImage({ image_url, data_url, resource_url, thought, page_url, page_title, captured_at }) {
  const blob = data_url ? await dataUrlToBlob(data_url) : await fetchImage(image_url);
  if (blob.size > MAX_UPLOAD_BYTES) {
    throw new CaptureError("too_large", `图片 ${(blob.size / 1048576).toFixed(1)} MB，超过 ${MAX_UPLOAD_BYTES / 1048576} MB 上限`);
  }
  const form = new FormData(); // do NOT set Content-Type; the browser adds the boundary
  form.append("modality", "image");
  form.append("file", blob, guessFilename(resource_url || image_url || "", blob.type));
  form.append("thought", cleanThought(thought) || "");
  form.append("page_url", page_url || "");
  form.append("resource_url", resource_url || image_url || "");
  form.append("page_title", page_title || "");
  form.append("captured_at", captured_at || new Date().toISOString());
  const resp = await coreFetch("/materials", { method: "POST", body: form });
  return finishSave(resp, cleanThought(thought));
}

// 201 → created; 200 → identical bytes already in the library (Core keeps the OLD thought and
// drops ours). If the existing material has no thought yet, apply ours via PATCH so the user's
// input is never silently lost. If it already has one, report it and leave it alone.
async function finishSave(resp, thought) {
  if (!resp.ok) throw await httpError(resp);
  const body = await resp.json();
  let { created, material } = body;
  let thoughtApplied = false;
  if (!created && thought && material && !(material.human && material.human.thought)) {
    try {
      const r = await coreFetch(`/materials/${material.id}/thought`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thought }),
      });
      if (r.ok) {
        material = await r.json();
        thoughtApplied = true;
      }
    } catch (_) {
      /* keep the duplicate result; the thought just was not applied */
    }
  }
  return { created: Boolean(created), material, thoughtApplied };
}

async function fetchImage(url) {
  if (!url) throw new CaptureError("image_fetch_failed", "没有图片地址");
  let resp;
  try {
    // credentials: include → with host permission the request carries the site's cookies,
    // so images behind a login can still be fetched where the site allows it
    resp = await fetch(url, { credentials: "include" });
  } catch (_) {
    throw new CaptureError("image_fetch_failed", "无法从扩展侧获取图片文件");
  }
  if (!resp.ok) throw new CaptureError("image_fetch_failed", `无法获取图片文件（HTTP ${resp.status}）`);
  const blob = await resp.blob();
  if (!blob.size) throw new CaptureError("image_fetch_failed", "图片文件为空");
  return blob;
}

async function dataUrlToBlob(dataUrl) {
  const resp = await fetch(dataUrl);
  return resp.blob();
}

async function getMaterial({ id }) {
  const resp = await coreFetch(`/materials/${encodeURIComponent(id)}`);
  if (!resp.ok) throw await httpError(resp);
  return { material: await resp.json() };
}

// Quick save first, thought second: the hover toast lets the user add the Human Thought after
// a one-click save (contract: PATCH /materials/{id}/thought).
async function setThought({ id, thought }) {
  if (!id) throw new CaptureError("bad_request", "没有素材 id");
  const resp = await coreFetch(`/materials/${encodeURIComponent(id)}/thought`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thought: cleanThought(thought) }),
  });
  if (!resp.ok) throw await httpError(resp);
  return { material: await resp.json() };
}

// "撤销本次收集": the extension only ever deletes a material it created moments ago.
async function deleteMaterial({ id }) {
  if (!id) throw new CaptureError("bad_request", "没有素材 id");
  const resp = await coreFetch(`/materials/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!resp.ok) throw await httpError(resp);
  return { deleted: id };
}

async function health() {
  const resp = await coreFetch("/health");
  if (!resp.ok) throw await httpError(resp);
  return { health: await resp.json(), coreUrl: await coreUrl() };
}

async function openLibrary() {
  await chrome.tabs.create({ url: (await coreUrl()) + "/" });
  return {};
}

// ------------------------------------------------------------------ helpers

function cleanThought(t) {
  const s = (t || "").trim();
  return s ? s : null;
}

// contract §9 guessFilename, extended: the Core trusts a known image extension in the
// filename, so the extension must reflect the bytes we actually upload — the URL may say
// .jpg while a CDN served WebP, or the canvas fallback produced a PNG. Stem from the URL,
// extension from the blob's MIME type when it is a known image type.
const MIME_EXT = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tif" };
function guessFilename(url, mime) {
  let fromUrl = "";
  try {
    if (/^https?:/i.test(url)) fromUrl = decodeURIComponent(new URL(url).pathname.split("/").pop() || "");
  } catch (_) {
    fromUrl = "";
  }
  const m = fromUrl.match(/^(.*?)(?:\.(jpe?g|png|webp|gif|bmp|tiff?))?$/i);
  const stem = (m && m[1] ? m[1] : "capture").slice(0, 100) || "capture";
  const urlExt = m && m[2] ? m[2].toLowerCase() : "";
  const ext = MIME_EXT[(mime || "").toLowerCase()] || urlExt || "jpg";
  return `${stem}.${ext}`;
}

function toErrorInfo(e) {
  if (e instanceof CaptureError) return { kind: e.kind, message: e.message, status: e.status };
  return { kind: "unknown", message: String((e && e.message) || e) };
}

// exposed for automated tests / debugging from the service-worker console
globalThis.__magpie = { captureInTab, saveText, saveImage, coreUrl, guessFilename };
