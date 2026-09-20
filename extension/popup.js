// Magpie Capture — toolbar popup: Core health, click-to-capture fallback, Core URL.
const $ = (sel) => document.querySelector(sel);

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, (res) => resolve(res || { ok: false, error: { message: "扩展无响应" } })));
}

async function refresh() {
  const dot = $("#dot");
  const status = $("#status");
  const res = await send({ type: "MAGPIE_HEALTH" });
  if (res.ok) {
    const h = res.health || {};
    dot.className = "dot ok";
    status.textContent = `Core 已连接 · ${h.materials ?? "?"} 条素材 · v${h.version || "?"}`;
    $("#coreUrl").placeholder = res.coreUrl;
  } else {
    dot.className = "dot bad";
    status.textContent = (res.error && res.error.message) || "Core 未启动";
  }
  const { coreUrl, hoverMenu } = await chrome.storage.local.get(["coreUrl", "hoverMenu"]);
  if (coreUrl) $("#coreUrl").value = coreUrl;
  $("#hoverMenu").checked = hoverMenu !== false; // default on

  const commands = await chrome.commands.getAll();
  const cmd = commands.find((c) => c.name === "capture");
  $("#shortcut").textContent = cmd && cmd.shortcut ? cmd.shortcut : "未设置";
}

$("#capture").addEventListener("click", async () => {
  const res = await send({ type: "MAGPIE_CAPTURE_ACTIVE_TAB" });
  if (res.ok) window.close();
  else {
    $("#dot").className = "dot bad";
    $("#status").textContent = (res.error && res.error.message) || "无法在这个页面收集";
  }
});

$("#open").addEventListener("click", () => send({ type: "MAGPIE_OPEN_LIBRARY" }).then(() => window.close()));
// content scripts on open pages pick this up via chrome.storage.onChanged
$("#hoverMenu").addEventListener("change", (e) => chrome.storage.local.set({ hoverMenu: e.target.checked }));
$("#shortcuts").addEventListener("click", () => chrome.tabs.create({ url: "chrome://extensions/shortcuts" }));
$("#saveUrl").addEventListener("click", async () => {
  await send({ type: "MAGPIE_SET_CORE_URL", payload: { coreUrl: $("#coreUrl").value } });
  refresh();
});

refresh();
