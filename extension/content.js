// Magpie Capture — content script, injected on demand by background.js (idempotent).
//
//   MAGPIE_START_CAPTURE
//     ├─ selected text            → overlay(text)
//     ├─ context menu on an image → overlay(image)
//     └─ nothing selected         → pick mode: hover-highlight <img>, click → overlay(image)
//   overlay: preview · Human Thought (one line, optional) · Enter = Save · Esc = cancel
//   Save → background does the HTTP → 已保存 / 已在素材库 / 错误(可重试)
//
// Rendered inside a closed-off shadow root so page CSS cannot touch it.
(() => {
  if (window.__magpieCapture) return;

  const HOST_ID = "magpie-capture-host";
  const AUTO_CLOSE_MS = 2400; // duplicates, toasts
  const AUTO_CLOSE_SAVED_MS = 4500; // after a save: leaves time to hit 撤销 (hover pauses the timer)
  const TEXT_PREVIEW_CHARS = 400;

  const state = { host: null, root: null, candidate: null, closeTimer: null, lastCloseMs: AUTO_CLOSE_MS, pick: null, onDocKey: null };
  window.__magpieCapture = state;

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "MAGPIE_START_CAPTURE") return false;
    try {
      startCapture(msg);
      sendResponse({ ok: true });
    } catch (e) {
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
    return false;
  });

  // ---------------------------------------------------------------- entry

  function startCapture(msg) {
    stopPick();
    closeOverlay();
    if (msg.mode === "image" && msg.srcUrl) {
      openOverlay(imageCandidate(findImage(msg.srcUrl), msg.srcUrl));
      return;
    }
    const text = selectedText() || msg.selectionText || "";
    if (text.trim()) {
      openOverlay({ kind: "text", content: text });
      return;
    }
    if (msg.mode === "text") {
      toast("没有选中文字，先选中一段再试", "warn");
      return;
    }
    startPick();
  }

  // Selection inside <input>/<textarea> is not exposed by window.getSelection() in Chrome.
  function selectedText() {
    const el = document.activeElement;
    if (el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && /^(text|search|url|email|tel)$/i.test(el.type || "text")))) {
      const t = (el.value || "").slice(el.selectionStart || 0, el.selectionEnd || 0);
      if (t.trim()) return t;
    }
    const sel = window.getSelection();
    const t = sel ? sel.toString() : "";
    return t.trim() ? t : "";
  }

  // ---------------------------------------------------------------- images

  function findImage(url) {
    for (const img of document.images) if (img.currentSrc === url || img.src === url) return img;
    return null;
  }

  function imageCandidate(img, fallbackUrl) {
    return {
      kind: "image",
      url: img ? bestImageUrl(img) : fallbackUrl,
      el: img,
      alt: (img && img.alt) || "",
      width: img ? img.naturalWidth : 0,
      height: img ? img.naturalHeight : 0,
    };
  }

  // Prefer the largest srcset candidate (PRD F01.2: highest resolution the page offers),
  // else what the browser is showing, else src. Lazy-loaded images that still show a data:
  // placeholder fall back to the usual data-* attributes. URL fragments are dropped (never sent
  // to the server; WeChat appends "#imgIndex=0").
  const LAZY_ATTRS = ["data-src", "data-original", "data-lazy-src", "data-actualsrc"];
  function bestImageUrl(img) {
    let best = null;
    const srcset = img.srcset || img.getAttribute("data-srcset") || "";
    if (srcset) {
      // candidate = url [descriptor] separated by commas; urls themselves may contain commas
      // (Cloudinary "w_800,c_fill/…"), so match "non-space run + optional descriptor" instead of split(",")
      for (const m of srcset.matchAll(/(\S+)(?:\s+(\d*\.?\d+)[wx])?\s*(?:,|$)/g)) {
        const u = m[1].replace(/,$/, "");
        if (!u) continue;
        const v = m[2] ? parseFloat(m[2]) : 1; // "800w" or "2x" — a srcset never mixes the two
        if (!best || v > best.v) best = { u, v };
      }
    }
    let chosen = (best && best.u) || img.currentSrc || img.src || "";
    if (!chosen || /^data:/i.test(chosen)) {
      for (const attr of LAZY_ATTRS) {
        const v = img.getAttribute(attr);
        if (v && !/^data:/i.test(v)) {
          chosen = v;
          break;
        }
      }
    }
    try {
      const u = new URL(chosen, document.baseURI);
      u.hash = "";
      return u.href;
    } catch (_) {
      return chosen;
    }
  }

  function imageAt(x, y) {
    for (const el of document.elementsFromPoint(x, y)) {
      if (el.id === HOST_ID) continue;
      if (el.tagName === "IMG") return el;
    }
    return null;
  }

  // ---------------------------------------------------------------- pick mode

  function startPick() {
    const root = ensureHost();
    root.innerHTML = `${STYLE}<div class="banner">Magpie · 点击要收集的图片<span class="muted">Esc 取消</span></div><div class="pickbox" hidden></div>`;
    const box = root.querySelector(".pickbox");
    let current = null;

    const onMove = (e) => {
      const img = imageAt(e.clientX, e.clientY);
      if (img === current) return;
      current = img;
      if (!img) {
        box.hidden = true;
        return;
      }
      const r = img.getBoundingClientRect();
      Object.assign(box.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
      box.hidden = false;
    };
    const onClick = (e) => {
      const img = imageAt(e.clientX, e.clientY);
      if (!img) return; // keep picking
      e.preventDefault();
      e.stopImmediatePropagation();
      stopPick();
      openOverlay(imageCandidate(img, null));
    };
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      stopPick();
      teardownHost();
    };

    document.addEventListener("mousemove", onMove, true);
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);
    state.pick = { onMove, onClick, onKey };
  }

  function stopPick() {
    if (!state.pick) return;
    document.removeEventListener("mousemove", state.pick.onMove, true);
    document.removeEventListener("click", state.pick.onClick, true);
    document.removeEventListener("keydown", state.pick.onKey, true);
    state.pick = null;
  }

  // ---------------------------------------------------------------- overlay

  function openOverlay(cand) {
    state.candidate = cand;
    const root = ensureHost();
    root.innerHTML = `${STYLE}
      <div class="card" role="dialog" aria-label="Magpie 收集">
        <div class="head">
          <span class="brand">Magpie</span>
          <span class="kind">${cand.kind === "text" ? "文字" : "图片"}</span>
          <button class="x" type="button" title="关闭 (Esc)" aria-label="关闭">×</button>
        </div>
        <div class="preview"></div>
        <div class="src"></div>
        <input class="thought" type="text" maxlength="500" autocomplete="off" spellcheck="false"
               placeholder="为什么记下它？一句话，可选" aria-label="Human Thought">
        <div class="foot"><span class="hint">Enter 保存 · Esc 取消</span><button class="save" type="button">保存</button></div>
        <div class="status" hidden></div>
      </div>`;

    renderPreview(root, cand);
    root.querySelector(".src").textContent = [document.title, location.hostname].filter(Boolean).join(" · ");

    const card = root.querySelector(".card");
    const input = root.querySelector(".thought");
    root.querySelector(".x").addEventListener("click", closeOverlay);
    root.querySelector(".save").addEventListener("click", save);
    card.addEventListener("keydown", (e) => {
      e.stopPropagation(); // keep page shortcuts (YouTube, Notion …) out of the thought input
      if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        save();
      } else if (e.key === "Escape") {
        e.preventDefault();
        closeOverlay();
      }
    });
    // hovering the card pauses the auto-close after a save
    card.addEventListener("mouseenter", () => clearTimeout(state.closeTimer));
    card.addEventListener("mouseleave", () => {
      if (root.querySelector(".status.ok, .status.warn")) scheduleClose(state.lastCloseMs);
    });

    state.onDocKey = (e) => {
      if (e.key !== "Escape") return;
      e.stopImmediatePropagation();
      closeOverlay();
    };
    document.addEventListener("keydown", state.onDocKey, true);
    input.focus();
    if (root.activeElement !== input) requestAnimationFrame(() => input.focus()); // page stole focus back
  }

  function renderPreview(root, cand) {
    const box = root.querySelector(".preview");
    if (cand.kind === "text") {
      const q = document.createElement("blockquote");
      q.textContent = cand.content.length > TEXT_PREVIEW_CHARS ? `${cand.content.slice(0, TEXT_PREVIEW_CHARS)}…` : cand.content;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${Array.from(cand.content).length} 字`;
      box.append(q, meta);
      return;
    }
    const img = document.createElement("img");
    img.alt = cand.alt;
    img.decoding = "async";
    const meta = document.createElement("div");
    meta.className = "meta";
    const describe = (w, h) => {
      meta.textContent = [w && h ? `${w}×${h}` : "", basename(cand.url)].filter(Boolean).join(" · ");
    };
    describe(cand.width, cand.height);
    img.addEventListener("load", () => {
      if (!cand.width) describe(img.naturalWidth, img.naturalHeight);
    });
    img.addEventListener("error", () => {
      img.replaceWith(Object.assign(document.createElement("div"), { className: "noimg", textContent: "预览不可用，保存仍会尝试获取原文件" }));
    });
    img.src = cand.url;
    box.append(img, meta);
  }

  function closeOverlay() {
    clearTimeout(state.closeTimer);
    state.closeTimer = null;
    if (state.onDocKey) document.removeEventListener("keydown", state.onDocKey, true);
    state.onDocKey = null;
    state.candidate = null;
    teardownHost();
  }

  function scheduleClose(ms = AUTO_CLOSE_MS) {
    clearTimeout(state.closeTimer);
    state.lastCloseMs = ms;
    state.closeTimer = setTimeout(closeOverlay, ms);
  }

  // ---------------------------------------------------------------- save

  async function save() {
    const root = state.root;
    const cand = state.candidate;
    if (!root || !cand) return;
    const btn = root.querySelector(".save");
    if (btn.disabled) return;
    const thought = root.querySelector(".thought").value.trim();
    const base = { thought, page_url: location.href, page_title: document.title, captured_at: localIso() };

    setBusy(root, true);
    setStatus(root, "info", "保存中…");

    let res;
    if (cand.kind === "text") {
      res = await send({ type: "MAGPIE_SAVE_TEXT", payload: { ...base, content: cand.content } });
    } else {
      res = await send({ type: "MAGPIE_SAVE_IMAGE", payload: { ...base, resource_url: cand.url, image_url: cand.url } });
      if (!res.ok && res.error && res.error.kind === "image_fetch_failed") {
        // blob: urls and same-origin/cookie-gated images are reachable from the page itself
        const dataUrl = await fetchFromPage(cand.url);
        if (dataUrl) res = await send({ type: "MAGPIE_SAVE_IMAGE", payload: { ...base, resource_url: cand.url, data_url: dataUrl } });
      }
    }
    if (state.root !== root) return; // user closed the overlay while saving; the save itself still happened
    setBusy(root, false);
    if (!res.ok) {
      showError(root, res.error, cand, base);
      return;
    }
    if (res.created) {
      lockForm(root, "已保存");
      setStatus(root, "ok", "已保存 ✓ 正在后台分析", { library: true, undo: res.material && res.material.id });
      scheduleClose(AUTO_CLOSE_SAVED_MS);
    } else {
      const m = res.material || {};
      const when = fmtDate((m.source && m.source.captured_at) || m.created_at);
      let text = `已在素材库里${when ? `（${when} 收集）` : ""}`;
      if (thought) {
        if (res.thoughtApplied) text += "，已补上这句 Thought";
        else if (m.human && m.human.thought) text += `。原 Thought 保留：“${m.human.thought}”，本次输入未保存`;
      }
      lockForm(root, "已在库中");
      setStatus(root, "warn", text, { library: true });
      scheduleClose();
    }
  }

  // 撤销本次收集 (PRD §5.1): delete the material we just created. Only offered for created
  // materials — a duplicate existed before this capture and is left alone.
  async function undoSave(root, materialId) {
    clearTimeout(state.closeTimer);
    setStatus(root, "info", "撤销中…");
    const res = await send({ type: "MAGPIE_DELETE_MATERIAL", payload: { id: materialId } });
    if (state.root !== root) return;
    if (!res.ok) {
      setStatus(root, "error", `撤销失败：${(res.error && res.error.message) || "未知错误"}`, [{ label: "重试", onClick: () => undoSave(root, materialId) }]);
      return;
    }
    lockForm(root, "已撤销");
    setStatus(root, "info", "已撤销，这条素材已从库里删除");
    scheduleClose();
  }

  function showError(root, err, cand, base) {
    const kind = (err && err.kind) || "unknown";
    let text = (err && err.message) || "保存失败";
    if (kind === "core_unreachable") text += "，运行 magpie serve 后重试";
    const actions = [{ label: "重试", onClick: save }];
    if (kind === "image_fetch_failed" && cand.el && cand.el.naturalWidth) {
      // PRD F01.2: never silently swap the original for a re-encoded copy — ask first
      text = "拿不到原图文件（跨域或站点限制）";
      actions.push({
        label: "保存页面渲染版本 (PNG)",
        onClick: async () => {
          let dataUrl;
          try {
            dataUrl = canvasDataUrl(cand.el);
          } catch (_) {
            setStatus(root, "error", "页面里的这张图是跨域的，无法读取像素", [{ label: "重试原图", onClick: save }]);
            return;
          }
          setBusy(root, true);
          setStatus(root, "info", "保存中…");
          const res = await send({ type: "MAGPIE_SAVE_IMAGE", payload: { ...base, thought: root.querySelector(".thought").value.trim(), resource_url: cand.url, data_url: dataUrl } });
          if (state.root !== root) return;
          setBusy(root, false);
          if (!res.ok) return setStatus(root, "error", (res.error && res.error.message) || "保存失败", [{ label: "重试", onClick: save }]);
          if (res.created) {
            lockForm(root, "已保存");
            setStatus(root, "ok", "已保存渲染版本 ✓（非原始文件）", { library: true, undo: res.material && res.material.id });
            scheduleClose(AUTO_CLOSE_SAVED_MS);
          } else {
            lockForm(root, "已在库中");
            setStatus(root, "warn", "已在素材库里", { library: true });
            scheduleClose();
          }
        },
      });
    }
    setStatus(root, "error", text, actions);
  }

  // ---------------------------------------------------------------- page-side image fallbacks

  async function fetchFromPage(url) {
    try {
      const r = await fetch(url, { credentials: "include" });
      if (!r.ok) return null;
      const b = await r.blob();
      if (!b.size) return null;
      return await blobToDataUrl(b);
    } catch (_) {
      return null;
    }
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(blob);
    });
  }

  // throws SecurityError when the image is cross-origin (tainted canvas)
  function canvasDataUrl(img) {
    const c = document.createElement("canvas");
    c.width = img.naturalWidth;
    c.height = img.naturalHeight;
    c.getContext("2d").drawImage(img, 0, 0);
    return c.toDataURL("image/png");
  }

  // ---------------------------------------------------------------- ui helpers

  function setBusy(root, busy) {
    const btn = root.querySelector(".save");
    const input = root.querySelector(".thought");
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? "保存中…" : "保存";
    }
    if (input) input.disabled = busy;
  }

  // After a successful save the form stays locked: a second Enter must not create a duplicate
  // request; the remaining actions are 撤销 / 打开素材库 / Esc.
  function lockForm(root, label) {
    setBusy(root, true);
    const btn = root.querySelector(".save");
    if (btn) btn.textContent = label;
  }

  // actions: array of {label, onClick}  |  { undo: materialId, library: true } for the standard buttons
  function setStatus(root, kind, text, actions) {
    const el = root.querySelector(".status");
    if (!el) return;
    el.className = `status ${kind}`;
    el.hidden = false;
    el.textContent = "";
    el.append(Object.assign(document.createElement("span"), { textContent: text }));
    const list = Array.isArray(actions) ? actions : [];
    if (actions && actions.undo) list.push({ label: "撤销", onClick: () => undoSave(root, actions.undo) });
    if (actions && actions.library) list.push({ label: "打开素材库", onClick: () => send({ type: "MAGPIE_OPEN_LIBRARY" }) });
    for (const a of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.textContent = a.label;
      b.addEventListener("click", a.onClick);
      el.append(b);
    }
  }

  function toast(text, kind) {
    const root = ensureHost();
    root.innerHTML = `${STYLE}<div class="card"><div class="head"><span class="brand">Magpie</span></div><div class="status ${kind || "info"}">${escapeHtml(text)}</div></div>`;
    scheduleClose();
  }

  function ensureHost() {
    if (state.host && state.host.isConnected) return state.root;
    const host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;pointer-events:none;";
    (document.body || document.documentElement).appendChild(host);
    state.host = host;
    state.root = host.attachShadow({ mode: "open" });
    return state.root;
  }

  function teardownHost() {
    if (state.host) state.host.remove();
    state.host = null;
    state.root = null;
  }

  function send(msg) {
    return new Promise((resolve) => {
      const dead = { ok: false, error: { kind: "extension", message: "扩展已重新加载，请刷新页面后重试" } };
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          if (chrome.runtime.lastError || !res) resolve(dead);
          else resolve(res);
        });
      } catch (_) {
        resolve(dead);
      }
    });
  }

  function basename(url) {
    if (!url) return "";
    if (/^(data|blob):/i.test(url)) return url.split(":")[0] + " 图片";
    try {
      const name = decodeURIComponent(new URL(url).pathname.split("/").pop() || "");
      return name.length > 48 ? `${name.slice(0, 45)}…` : name;
    } catch (_) {
      return "";
    }
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : `${d.getMonth() + 1}月${d.getDate()}日`;
  }

  // ISO 8601 with the local offset (contract: prefer a timezone; +08:00 reads better than Z later)
  function localIso(d = new Date()) {
    const pad = (n) => String(n).padStart(2, "0");
    const off = -d.getTimezoneOffset();
    const sign = off >= 0 ? "+" : "-";
    const abs = Math.abs(off);
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
      `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
    );
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  const STYLE = `<style>
    :host { all: initial; }
    .card, .banner, .pickbox, .card * {
      box-sizing: border-box;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", "Microsoft YaHei", sans-serif;
    }
    .card {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483647;
      width: 360px; max-width: calc(100vw - 32px);
      background: #fff; color: #111; border: 1px solid #e5e7eb; border-radius: 14px;
      box-shadow: 0 12px 40px rgba(0,0,0,.18); padding: 12px 14px; pointer-events: auto;
      font-size: 13px; line-height: 1.45; text-align: left;
    }
    .head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .brand { font-weight: 700; letter-spacing: .2px; }
    .kind { font-size: 11px; color: #6b7280; background: #f3f4f6; border-radius: 999px; padding: 1px 8px; }
    .x { margin-left: auto; border: 0; background: transparent; font-size: 18px; line-height: 1; color: #9ca3af; cursor: pointer; padding: 2px 6px; }
    .x:hover { color: #111; }
    .preview blockquote {
      margin: 0; padding: 8px 10px; border-left: 3px solid #d1d5db; background: #f9fafb; border-radius: 6px;
      max-height: 120px; overflow: auto; white-space: pre-wrap; word-break: break-word; color: #374151;
    }
    .preview img { display: block; max-width: 100%; max-height: 160px; border-radius: 8px; background: #f3f4f6; object-fit: contain; }
    .noimg { padding: 14px 10px; border-radius: 8px; background: #f3f4f6; color: #6b7280; font-size: 12px; }
    .meta, .src { font-size: 11px; color: #6b7280; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .src { margin-top: 6px; }
    .thought {
      display: block; width: 100%; margin-top: 10px; padding: 8px 10px;
      border: 1px solid #d1d5db; border-radius: 8px; font: inherit; color: #111; background: #fff; outline: none;
    }
    .thought:focus { border-color: #111; box-shadow: 0 0 0 3px rgba(17,17,17,.08); }
    .thought[disabled] { color: #6b7280; background: #f9fafb; }
    .foot { display: flex; align-items: center; margin-top: 10px; gap: 8px; }
    .hint { font-size: 11px; color: #9ca3af; }
    .save {
      margin-left: auto; border: 0; border-radius: 8px; padding: 7px 14px; font: inherit; font-weight: 600;
      cursor: pointer; background: #111; color: #fff;
    }
    .save[disabled] { opacity: .5; cursor: default; }
    .status {
      margin-top: 10px; padding: 8px 10px; border-radius: 8px; font-size: 12px; background: #f3f4f6; color: #374151;
      display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center;
    }
    .status[hidden], .pickbox[hidden] { display: none; }
    .status.ok { background: #ecfdf5; color: #065f46; }
    .status.warn { background: #fffbeb; color: #92400e; }
    .status.error { background: #fef2f2; color: #991b1b; }
    .btn {
      border: 0; border-radius: 6px; padding: 3px 10px; font: inherit; font-size: 12px; cursor: pointer;
      background: rgba(0,0,0,.08); color: inherit;
    }
    .btn:hover { background: rgba(0,0,0,.14); }
    .banner {
      position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 2147483647;
      background: #111; color: #fff; padding: 8px 14px; border-radius: 999px; font-size: 13px;
      box-shadow: 0 8px 30px rgba(0,0,0,.25); pointer-events: none; white-space: nowrap;
    }
    .banner .muted { opacity: .6; margin-left: 10px; }
    .pickbox {
      position: fixed; z-index: 2147483646; border: 2px solid #111; border-radius: 4px; pointer-events: none;
      box-shadow: 0 0 0 4px rgba(255,255,255,.75), 0 0 0 9999px rgba(0,0,0,.12);
    }
  </style>`;
})();
