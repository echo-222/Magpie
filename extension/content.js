// Magpie Capture — content script. Auto-injected on http(s) pages (manifest content_scripts) and
// also injected on demand by background.js for pages that were open before the extension loaded
// (idempotent).
//
//   hover capture (always on, no shortcut; toggle in the popup)
//     ├─ hover an <img>           → small badge at its top-right; hover the badge → arc menu:
//     │                              保存 (quick save) · 加批注保存 (overlay) · 打开素材库
//     └─ finish a text selection  → pill next to the cursor: 保存 · 加批注保存
//   MAGPIE_START_CAPTURE (shortcut / context menu / popup)
//     ├─ selected text            → overlay(text)
//     ├─ context menu on an image → overlay(image)
//     └─ nothing selected         → pick mode: hover-highlight <img>, click → overlay(image)
//   overlay: preview · Human Thought (one line, optional) · Enter = Save · Esc = cancel
//   Save → background does the HTTP → 已保存 / 已在素材库 / 错误(可重试)
//   quick save → toast: 已保存 ✓ · 补一句 Thought (PATCH) · 撤销 · 打开素材库
//
// Rendered inside shadow roots so page CSS cannot touch it. The hover UI has its own host so it
// can coexist with the overlay.
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
    hideHover();
    hidePill();
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
      if (el.id === HOST_ID || el.id === HOVER_HOST_ID) continue;
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

    const res = await doSave(cand, base);
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

  // The HTTP half of a save, shared by the overlay and quick save. Returns the background's
  // response ({ok, created, material, thoughtApplied} | {ok:false, error}).
  async function doSave(cand, base) {
    if (cand.kind === "text") {
      return send({ type: "MAGPIE_SAVE_TEXT", payload: { ...base, content: cand.content } });
    }
    let res = await send({ type: "MAGPIE_SAVE_IMAGE", payload: { ...base, resource_url: cand.url, image_url: cand.url } });
    if (!res.ok && res.error && res.error.kind === "image_fetch_failed") {
      // blob: urls and same-origin/cookie-gated images are reachable from the page itself
      const dataUrl = await fetchFromPage(cand.url);
      if (dataUrl) res = await send({ type: "MAGPIE_SAVE_IMAGE", payload: { ...base, resource_url: cand.url, data_url: dataUrl } });
    }
    return res;
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

  // ---------------------------------------------------------------- hover capture
  //
  // Always on (toggle: popup → chrome.storage.local.hoverMenu). Hovering an <img> shows a small
  // badge at its top-right corner; hovering the badge fans out an arc of actions. Finishing a
  // text selection shows a pill with the same save actions next to the cursor. Everything here
  // lives in its own shadow host (HOVER_HOST_ID) so it never wipes the overlay and vice versa.

  const HOVER_HOST_ID = "magpie-hover-host";
  const HOVER_MIN_SIDE = 64; // icons, avatars, spacer gifs never get a badge
  const HOVER_MIN_AREA = 12000;
  const HOVER_SHOW_MS = 160;
  const HOVER_HIDE_MS = 280; // grace to travel from the image to the badge / arc
  const BADGE = 28; // px
  const ARC_R = 46; // px, distance from badge centre to each action centre
  const TOAST_MS = 6000;

  const hover = { enabled: true, host: null, root: null, img: null, pendingImg: null, showTimer: null, hideTimer: null, pillText: null, toastTimer: null };
  state.hover = hover; // exposed with the rest of the state for tests / debugging

  const ICONS = {
    save: '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M10 3v14M3 10h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" fill="none"/></svg>',
    note: '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M4 16l1-4L14 3l3 3-9 9-4 1z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="none"/><path d="M12 5l3 3" stroke="currentColor" stroke-width="1.8"/></svg>',
    library: '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M3 4h5v5H3zM12 4h5v5h-5zM3 11h5v5H3zM12 11h5v5h-5z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/></svg>',
    // the Magpie logo (icons/icon128.png, listed in web_accessible_resources); falls back to a
    // plain star if the extension context is gone
    brand: brandImg("icons/icon128.png", "brand-img"),
    // white line-art version of the same bird, for dark surfaces (the text-selection pill)
    brandDark: brandImg("icons/logo-dark.png", "brand-img brand-dark"),
  };
  function brandImg(path, cls) {
    try {
      return `<img class="${cls}" src="${chrome.runtime.getURL(path)}" alt="" draggable="false">`;
    } catch (_) {
      return '<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M10 2.5l2.2 4.8 5.3.6-3.9 3.6 1.1 5.2L10 14.1l-4.7 2.6 1.1-5.2L2.5 7.9l5.3-.6z" fill="currentColor"/></svg>';
    }
  }
  // arc opens toward the inside of the image (badge sits at the top-right corner); screen
  // angles, y down: 180° = left, 90° = down
  const ACTIONS = [
    { id: "save", label: "保存", angle: 180 },
    { id: "note", label: "加批注保存", angle: 135 },
    { id: "library", label: "打开素材库", angle: 90 },
  ];

  function ensureHoverHost() {
    if (hover.host && hover.host.isConnected) return hover.root;
    const host = document.createElement("div");
    host.id = HOVER_HOST_ID;
    host.style.cssText = "position:fixed;top:0;left:0;width:0;height:0;z-index:2147483646;pointer-events:none;";
    (document.body || document.documentElement).appendChild(host);
    hover.host = host;
    hover.root = host.attachShadow({ mode: "open" });
    hover.root.innerHTML = `${HOVER_STYLE}
      <div class="anchor" hidden>
        <button class="badge" type="button" aria-label="Magpie 收集这张图" title="Magpie">${ICONS.brand}</button>
        <div class="arc">${ACTIONS.map((a) => {
          const rad = (a.angle * Math.PI) / 180;
          const x = Math.round(Math.cos(rad) * ARC_R);
          const y = Math.round(Math.sin(rad) * ARC_R);
          return `<button class="act" type="button" data-act="${a.id}" data-label="${a.label}" aria-label="${a.label}" style="--tx:${x}px;--ty:${y}px">${ICONS[a.id]}</button>`;
        }).join("")}</div>
        <div class="label" hidden></div>
      </div>
      <div class="pill" hidden>
        <span class="pbrand">${ICONS.brandDark}</span>
        <button class="pact" type="button" data-act="save">保存</button>
        <button class="pact" type="button" data-act="note">加批注保存</button>
      </div>
      <div class="toast" hidden></div>`;
    wireHoverHost(hover.root);
    return hover.root;
  }

  function wireHoverHost(root) {
    const anchor = root.querySelector(".anchor");
    const label = root.querySelector(".label");
    anchor.addEventListener("mouseenter", () => clearTimeout(hover.hideTimer));
    anchor.addEventListener("mouseleave", () => scheduleHideHover());
    const badge = root.querySelector(".badge");
    badge.addEventListener("mouseenter", () => anchor.classList.add("open"));
    badge.addEventListener("click", (e) => {
      e.preventDefault();
      anchor.classList.toggle("open"); // touch / pen: tap toggles the arc
    });
    // mousedown on our buttons must not move focus or collapse the page selection
    for (const b of root.querySelectorAll(".act, .pact, .badge")) b.addEventListener("mousedown", (e) => e.preventDefault());
    for (const b of root.querySelectorAll(".act")) {
      b.addEventListener("mouseenter", () => {
        label.textContent = b.dataset.label;
        label.hidden = false;
      });
      b.addEventListener("mouseleave", () => (label.hidden = true));
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        runHoverAction(b.dataset.act);
      });
    }
    for (const b of root.querySelectorAll(".pact")) {
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        runPillAction(b.dataset.act);
      });
    }
    const toast = root.querySelector(".toast");
    toast.addEventListener("mouseenter", () => clearTimeout(hover.toastTimer));
    toast.addEventListener("mouseleave", () => {
      if (!toast.querySelector(".tinput:focus")) scheduleHideToast();
    });
    // keep page shortcuts away from the toast's thought input
    toast.addEventListener("keydown", (e) => e.stopPropagation());
  }

  // ---- images

  function eligibleImage(img) {
    if (!img || !img.isConnected) return false;
    const r = img.getBoundingClientRect();
    if (r.width < HOVER_MIN_SIDE || r.height < HOVER_MIN_SIDE || r.width * r.height < HOVER_MIN_AREA) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) return false;
    return true;
  }

  // Tracked on mousemove (rAF-throttled) rather than mouseover: an <img> under a transparent
  // link/overlay never receives mouseover, and after our arc disappears Chrome does not reliably
  // re-fire it for the image underneath. composedPath() is only valid during dispatch, so the
  // "over our own UI" bit is captured synchronously.
  const mouse = { x: 0, y: 0, overHost: false, raf: 0 };
  function onHoverMove(e) {
    if (!hover.enabled || state.pick || state.candidate) return; // pick mode / overlay open: stay out of the way
    mouse.x = e.clientX;
    mouse.y = e.clientY;
    mouse.overHost = Boolean(hover.host && e.composedPath().includes(hover.host));
    if (mouse.raf) return;
    mouse.raf = requestAnimationFrame(() => {
      mouse.raf = 0;
      evaluateHover();
    });
  }

  function evaluateHover() {
    if (mouse.overHost) {
      clearTimeout(hover.hideTimer);
      return;
    }
    const img = imageAt(mouse.x, mouse.y);
    if (img && eligibleImage(img)) {
      clearTimeout(hover.hideTimer);
      if (img !== hover.img && hover.pendingImg !== img) {
        // short settle delay so sweeping across a page does not flash badges everywhere
        clearTimeout(hover.showTimer);
        hover.pendingImg = img;
        hover.showTimer = setTimeout(() => {
          hover.pendingImg = null;
          showHover(img);
        }, HOVER_SHOW_MS);
      }
    } else {
      clearTimeout(hover.showTimer);
      hover.pendingImg = null;
      scheduleHideHover();
    }
  }

  function showHover(img) {
    const root = ensureHoverHost();
    hover.img = img;
    const anchor = root.querySelector(".anchor");
    anchor.classList.remove("open");
    root.querySelector(".label").hidden = true;
    anchor.hidden = false;
    positionHover();
  }

  function positionHover() {
    if (!hover.img || !hover.root) return;
    if (!eligibleImage(hover.img)) return hideHover();
    const r = hover.img.getBoundingClientRect();
    const inset = 8;
    const left = Math.max(inset, Math.min(innerWidth - BADGE - inset, r.right - inset - BADGE));
    const top = Math.max(inset, Math.min(innerHeight - BADGE - inset, r.top + inset));
    const anchor = hover.root.querySelector(".anchor");
    anchor.style.left = `${Math.round(left)}px`;
    anchor.style.top = `${Math.round(top)}px`;
  }

  function scheduleHideHover() {
    clearTimeout(hover.hideTimer);
    hover.hideTimer = setTimeout(hideHover, HOVER_HIDE_MS);
  }

  function hideHover() {
    clearTimeout(hover.showTimer);
    clearTimeout(hover.hideTimer);
    hover.img = null;
    hover.pendingImg = null;
    if (!hover.root) return;
    const anchor = hover.root.querySelector(".anchor");
    anchor.hidden = true;
    anchor.classList.remove("open");
  }

  function runHoverAction(act) {
    const img = hover.img;
    hideHover();
    if (act === "library") return send({ type: "MAGPIE_OPEN_LIBRARY" });
    if (!img) return;
    const cand = imageCandidate(img, null);
    if (act === "note") return openOverlay(cand);
    if (act === "save") return quickSave(cand);
  }

  // ---- text selection pill

  function onPillMouseUp(e) {
    if (!hover.enabled || state.pick || state.candidate) return;
    if (hover.host && e.composedPath().includes(hover.host)) return;
    const x = e.clientX;
    const y = e.clientY;
    // the selection is final only after the browser has processed mouseup
    setTimeout(() => {
      const text = selectedText();
      if (!text) return hidePill();
      showPill(text, x, y);
    }, 0);
  }

  function showPill(text, x, y) {
    const root = ensureHoverHost();
    hover.pillText = text;
    const pill = root.querySelector(".pill");
    pill.hidden = false;
    const w = pill.offsetWidth || 200;
    const h = pill.offsetHeight || 32;
    pill.style.left = `${Math.round(Math.max(8, Math.min(innerWidth - w - 8, x + 10)))}px`;
    pill.style.top = `${Math.round(y + 14 + h > innerHeight - 8 ? y - h - 12 : y + 14)}px`;
  }

  function hidePill() {
    hover.pillText = null;
    if (hover.root) hover.root.querySelector(".pill").hidden = true;
  }

  function runPillAction(act) {
    const text = hover.pillText;
    hidePill();
    if (!text) return;
    const cand = { kind: "text", content: text };
    if (act === "note") return openOverlay(cand);
    if (act === "save") return quickSave(cand);
  }

  // ---- quick save (no overlay): toast with 补一句 Thought / 撤销 / 打开素材库

  async function quickSave(cand) {
    const base = { thought: "", page_url: location.href, page_title: document.title, captured_at: localIso() };
    showToast("info", cand.kind === "text" ? "保存文字中…" : "保存图片中…", { autoHide: 0 });
    const res = await doSave(cand, base);
    if (!res.ok) {
      // hand over to the overlay: it has 重试 and the canvas-render option for blocked images
      hideToast();
      openOverlay(cand);
      showError(state.root, res.error, cand, base);
      return;
    }
    if (res.created) {
      const id = res.material && res.material.id;
      showToast("ok", "已保存 ✓ 正在后台分析", { thoughtFor: id, undo: id, library: true });
    } else {
      const m = res.material || {};
      const when = fmtDate((m.source && m.source.captured_at) || m.created_at);
      showToast("warn", `已在素材库里${when ? `（${when} 收集）` : ""}`, { library: true });
    }
  }

  // opts: { thoughtFor: materialId, undo: materialId, library: true } | [{label, onClick}]
  function showToast(kind, text, opts) {
    const root = ensureHoverHost();
    const toast = root.querySelector(".toast");
    clearTimeout(hover.toastTimer);
    toast.className = `toast ${kind}`;
    toast.hidden = false;
    toast.textContent = "";
    const head = document.createElement("div");
    head.className = "thead";
    head.innerHTML = `<span class="tbrand">${ICONS.brand}</span>`;
    const msg = Object.assign(document.createElement("span"), { textContent: text });
    const x = Object.assign(document.createElement("button"), { className: "tx", type: "button", textContent: "×", title: "关闭" });
    x.addEventListener("click", hideToast);
    head.append(msg, x);
    toast.append(head);

    if (opts && opts.thoughtFor) {
      const input = document.createElement("input");
      input.className = "tinput";
      input.type = "text";
      input.maxLength = 500;
      input.placeholder = "补一句：为什么记下它？Enter 提交，可选";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.addEventListener("focus", () => clearTimeout(hover.toastTimer));
      input.addEventListener("blur", () => scheduleHideToast());
      input.addEventListener("keydown", async (e) => {
        if (e.key === "Escape") return hideToast();
        if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        const thought = input.value.trim();
        if (!thought) return;
        input.disabled = true;
        const r = await send({ type: "MAGPIE_SET_THOUGHT", payload: { id: opts.thoughtFor, thought } });
        if (toast.hidden) return;
        if (r.ok) {
          input.replaceWith(Object.assign(document.createElement("div"), { className: "tdone", textContent: `已补上 Thought：“${thought}”` }));
          scheduleHideToast(AUTO_CLOSE_MS);
        } else {
          input.disabled = false;
          input.focus();
          msg.textContent = `Thought 未保存：${(r.error && r.error.message) || "未知错误"}`;
        }
      });
      toast.append(input);
    }

    const actions = Array.isArray(opts) ? opts.slice() : [];
    if (opts && opts.undo) {
      actions.push({
        label: "撤销",
        onClick: async () => {
          clearTimeout(hover.toastTimer);
          const r = await send({ type: "MAGPIE_DELETE_MATERIAL", payload: { id: opts.undo } });
          if (r.ok) showToast("info", "已撤销，这条素材已从库里删除");
          else showToast("error", `撤销失败：${(r.error && r.error.message) || "未知错误"}`, [{ label: "重试", onClick: () => runUndo(opts.undo) }]);
        },
      });
    }
    if (opts && opts.library) actions.push({ label: "打开素材库", onClick: () => send({ type: "MAGPIE_OPEN_LIBRARY" }) });
    if (actions.length) {
      const row = document.createElement("div");
      row.className = "tactions";
      for (const a of actions) {
        const b = Object.assign(document.createElement("button"), { className: "btn", type: "button", textContent: a.label });
        b.addEventListener("click", a.onClick);
        row.append(b);
      }
      toast.append(row);
    }
    const autoHide = opts && opts.autoHide !== undefined ? opts.autoHide : kind === "ok" ? TOAST_MS : AUTO_CLOSE_MS;
    if (autoHide) scheduleHideToast(autoHide);
  }

  async function runUndo(id) {
    const r = await send({ type: "MAGPIE_DELETE_MATERIAL", payload: { id } });
    if (r.ok) showToast("info", "已撤销，这条素材已从库里删除");
    else showToast("error", `撤销失败：${(r.error && r.error.message) || "未知错误"}`);
  }

  function scheduleHideToast(ms = TOAST_MS) {
    clearTimeout(hover.toastTimer);
    hover.toastTimer = setTimeout(hideToast, ms);
  }

  function hideToast() {
    clearTimeout(hover.toastTimer);
    if (hover.root) hover.root.querySelector(".toast").hidden = true;
  }

  // ---- wiring

  document.addEventListener("mousemove", onHoverMove, { capture: true, passive: true });
  document.addEventListener("mouseleave", (e) => {
    // cursor left the window (Chrome also fires this synthetically on viewport changes → check the coords)
    if (e.clientX <= 0 || e.clientY <= 0 || e.clientX >= innerWidth || e.clientY >= innerHeight) scheduleHideHover();
  });
  document.addEventListener("mouseup", onPillMouseUp, true);
  document.addEventListener(
    "mousedown",
    (e) => {
      if (hover.host && e.composedPath().includes(hover.host)) return;
      hidePill();
    },
    true
  );
  document.addEventListener("selectionchange", () => {
    if (!hover.pillText || selectedText()) return;
    if (hover.root && hover.root.querySelector(".pill:hover")) return; // mid-click on the pill
    hidePill();
  });
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Escape") return;
      hidePill();
      hideHover();
    },
    true
  );
  const onViewport = () => {
    if (hover.img) positionHover();
    if (hover.pillText) hidePill();
  };
  addEventListener("scroll", onViewport, { capture: true, passive: true });
  addEventListener("resize", onViewport, { passive: true });

  try {
    chrome.storage.local.get("hoverMenu", (v) => {
      hover.enabled = !v || v.hoverMenu !== false;
    });
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local" || !changes.hoverMenu) return;
      hover.enabled = changes.hoverMenu.newValue !== false;
      if (!hover.enabled) {
        hideHover();
        hidePill();
      }
    });
  } catch (_) {
    /* extension context gone (reloaded); the page will be told on the next action */
  }

  const HOVER_STYLE = `<style>
    :host { all: initial; }
    .anchor, .pill, .toast, .anchor *, .pill *, .toast * {
      box-sizing: border-box;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", "Microsoft YaHei", sans-serif;
    }
    [hidden] { display: none !important; }
    .anchor { position: fixed; width: ${BADGE}px; height: ${BADGE}px; pointer-events: auto; z-index: 2147483646; }
    .badge {
      position: absolute; inset: 0; width: ${BADGE}px; height: ${BADGE}px; border-radius: 7px; border: 0; padding: 0;
      display: flex; align-items: center; justify-content: center; cursor: pointer; overflow: hidden;
      background: #fafaf8; box-shadow: 0 2px 10px rgba(0,0,0,.28), 0 0 0 1px rgba(0,0,0,.08);
      transition: transform .12s ease, box-shadow .12s ease;
    }
    .badge .brand-img { width: 100%; height: 100%; display: block; object-fit: cover; }
    .badge:hover, .anchor.open .badge { transform: scale(1.08); box-shadow: 0 4px 14px rgba(0,0,0,.32), 0 0 0 1px rgba(0,0,0,.12); }
    .tbrand .brand-img { width: 16px; height: 16px; border-radius: 4px; display: block; }
    .pbrand .brand-dark { height: 16px; width: auto; display: block; }
    .arc { position: absolute; left: ${BADGE / 2}px; top: ${BADGE / 2}px; width: 0; height: 0; }
    .act {
      position: absolute; left: -17px; top: -17px; width: 34px; height: 34px; border-radius: 50%; border: 0; padding: 0;
      display: flex; align-items: center; justify-content: center; cursor: pointer;
      background: #fff; color: #111; box-shadow: 0 4px 14px rgba(0,0,0,.22), 0 0 0 1px rgba(0,0,0,.06);
      transform: translate(0, 0) scale(.4); opacity: 0; pointer-events: none;
      transition: transform .16s cubic-bezier(.2,.8,.2,1), opacity .12s ease, background .12s ease;
    }
    .anchor.open .act { transform: translate(var(--tx), var(--ty)) scale(1); opacity: 1; pointer-events: auto; }
    .anchor.open .act:nth-child(2) { transition-delay: .03s; }
    .anchor.open .act:nth-child(3) { transition-delay: .06s; }
    .act:hover { background: #111; color: #fff; }
    .label {
      /* to the left of the leftmost action, vertically centred on the badge */
      position: absolute; right: ${BADGE / 2 + ARC_R + 17 + 8}px; top: ${BADGE / 2 - 12}px; white-space: nowrap;
      background: #111; color: #fff; font-size: 12px; line-height: 1; padding: 6px 9px; border-radius: 999px;
      box-shadow: 0 4px 14px rgba(0,0,0,.22); pointer-events: none;
    }
    .pill {
      position: fixed; z-index: 2147483646; pointer-events: auto;
      display: flex; align-items: center; gap: 2px; padding: 3px 4px 3px 8px;
      background: #111; color: #fff; border-radius: 999px; box-shadow: 0 6px 20px rgba(0,0,0,.28);
      font-size: 12px; line-height: 1;
    }
    .pbrand { display: flex; margin-right: 2px; }
    .tbrand .brand-img { box-shadow: 0 0 0 1px rgba(0,0,0,.1); }
    .pact { border: 0; background: transparent; color: #fff; font: inherit; font-size: 12px; padding: 6px 9px; border-radius: 999px; cursor: pointer; white-space: nowrap; }
    .pact:hover { background: rgba(255,255,255,.16); }
    .toast {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483646; pointer-events: auto;
      width: 340px; max-width: calc(100vw - 32px);
      background: #fff; color: #111; border: 1px solid #e5e7eb; border-radius: 12px;
      box-shadow: 0 12px 40px rgba(0,0,0,.18); padding: 10px 12px; font-size: 13px; line-height: 1.45; text-align: left;
    }
    .thead { display: flex; align-items: center; gap: 8px; }
    .tbrand { display: flex; color: #111; }
    .toast.ok .thead { color: #065f46; } .toast.warn .thead { color: #92400e; } .toast.error .thead { color: #991b1b; } .toast.info .thead { color: #374151; }
    .tx { margin-left: auto; border: 0; background: transparent; font-size: 18px; line-height: 1; color: #9ca3af; cursor: pointer; padding: 0 4px; }
    .tx:hover { color: #111; }
    .tinput {
      display: block; width: 100%; margin-top: 8px; padding: 7px 10px;
      border: 1px solid #d1d5db; border-radius: 8px; font: inherit; font-size: 13px; color: #111; background: #fff; outline: none;
    }
    .tinput:focus { border-color: #111; box-shadow: 0 0 0 3px rgba(17,17,17,.08); }
    .tinput[disabled] { color: #6b7280; background: #f9fafb; }
    .tdone { margin-top: 8px; font-size: 12px; color: #065f46; background: #ecfdf5; border-radius: 8px; padding: 6px 10px; }
    .tactions { display: flex; gap: 8px; margin-top: 8px; }
    .btn { border: 0; border-radius: 6px; padding: 4px 10px; font: inherit; font-size: 12px; cursor: pointer; background: rgba(0,0,0,.08); color: inherit; }
    .btn:hover { background: rgba(0,0,0,.14); }
  </style>`;

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
