# Magpie Capture（浏览器扩展）

Collect inspiration. Create with it. 在网页上选中文字或点选一张图，加一句 Human Thought，保存到本地 Magpie Core。

```
网页 → ⌥⇧M（Alt+Shift+M）或右键菜单 → 选中文字 | 点选 <img>
→ 一行 Human Thought（可选）→ Enter 保存 → POST /materials → 已保存 / 已在素材库 / 错误(可重试)
```

接口以 [`docs/CAPTURE_API_CONTRACT.md`](../docs/CAPTURE_API_CONTRACT.md) 为准；扩展只用 `POST /materials`、`GET /materials/{id}`、`PATCH /materials/{id}/thought`、`DELETE /materials/{id}` 和 `GET /health`。

## 加载

Manifest V3，纯 JS，没有构建步骤。

1. Chrome / Edge 打开 `chrome://extensions`，开启右上角「开发者模式」
2. 「加载已解压的扩展程序」→ 选择这个 `extension/` 目录
3. 点工具栏图标，popup 里应显示「Core 已连接」；没连上就先起 Core（见下）

改了代码后在 `chrome://extensions` 点刷新；已打开的页面里旧的 content script 会提示「扩展已重新加载，请刷新页面」。

## 用法

- **快捷键 ⌥⇧M**：有选中文字就收文字；没有就进入点选模式，鼠标移到图片上高亮，点击选中，Esc 取消。快捷键冲突时到 `chrome://extensions/shortcuts` 改（popup 里有入口）
- **右键菜单**：选中文字上「收集到 Magpie：选中的文字」；图片上「收集到 Magpie：这张图片」
- **popup**：Core 状态、「在当前页面收集」（快捷键的备选）、打开 Dev UI、改 Core 地址（默认 `http://127.0.0.1:8765`）
- 浮层里 Enter 保存、Esc 关闭；中文输入法组合中的 Enter 不会触发保存。保存成功后可「撤销」（删掉刚存的这条）
- 同样内容再次保存会提示「已在素材库里」：若旧素材没有 Thought，会把这次输入补上去；若已有，保留旧的并提示本次输入未保存（Core 按内容哈希去重，见合同 §11）

## 本地 Core

Core 必须在本机运行（扩展只请求 `127.0.0.1`）。只想测采集、不想装 Ollama 的话，用 fake provider：

```bash
# 仓库根目录，Python ≥ 3.11
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cat > .env <<'EOF'
MAGPIE_LLM_PROVIDER=fake
MAGPIE_CHAT_PROVIDER=local
MAGPIE_OCR=off
EOF
.venv/bin/magpie serve          # http://127.0.0.1:8765
```

fake provider 下 Save → pending → ready 整条链和真实环境一致，只是分析结果是假的。要真实分析按根目录 `README.md` 配 Ollama / DeepSeek，`.env` 已在 `.gitignore` 里，key 不要写进任何会提交的文件。

## 发送了什么

| 字段 | 来源 |
|---|---|
| `content`（文字）| 选区原文，含 `<input>/<textarea>` 内的选区 |
| `file`（图片）| 扩展自己取到的图片字节，文件名扩展名以真实 MIME 为准（Core 信扩展名）|
| `human.thought` / `thought` | 浮层输入，空则为 `null` |
| `source.page_url` / `page_title` | `location.href` / `document.title` |
| `source.resource_url` | 图片 URL：srcset 里最大的候选 > `currentSrc` > `src`；`src` 还是懒加载占位 `data:` 时取 `data-src` 等；去掉 `#fragment` |
| `source.captured_at` | 本地时区 ISO 8601，如 `2026-09-18T15:47:34+08:00` |

## 图片怎么拿到

Core 不会自己下载 `resource_url`，扩展按顺序尝试：

1. **background service worker 直接 fetch**（扩展源 + `<all_urls>` 权限，带站点 cookie）。绝大多数站点走这里，包括公众号 `mmbiz.qpic.cn`（它只拒外站 Referer，扩展请求不带 Referer，拿到的是原图而不是占位图）
2. **页面内 fetch**：覆盖 `blob:` URL 和只有页面上下文能拿的图
3. **canvas 渲染版本**：前两步都失败时不会静默保存，浮层明确询问，确认后保存 PNG 并标注「非原始文件」；跨域图 canvas 会被污染，此时提示无法读取像素

## 已验证

- 无头 Chrome for Testing 对本地 Core 的端到端：59 项检查，覆盖文字/textarea 选区、字段逐项比对、重复与补 Thought、srcset 最大候选、懒加载占位、`blob:`、防盗链跨域图的 canvas 询问、撤销、Core 未启动 → 重试、chrome:// 页面报错
- 真实站点：Wikipedia（文字 + CDN 图）、微信公众号文章（文字 + `mmbiz` 图，字节与原图一致）

## 已知限制

- iframe 里的选区和图片收不到（只注入顶层 frame）；右键菜单会用浏览器给的 `selectionText` 兜底
- 只支持 `<img>`；CSS 背景图、`<video>` 封面、SVG 内联图不在范围内
- 要求 Referer 必须匹配的站点（如 pixiv）会走到 canvas 询问；需要时再加 `declarativeNetRequest` 改 Referer
- 上传软上限 25 MB（Core 不限制）
- 公众号图存的是页面展示的 `/640` 尺寸；`/0` 原尺寸是站点特定规则，未加
- 音视频、截图：Core 目前只有 `image` / `text` 两种 modality

## 文件

- `manifest.json` — MV3；权限说明：`<all_urls>` 是为了在 background 取第三方站点的图片字节，`scripting` 按需注入 content script，`contextMenus` 右键入口，`storage` 存 Core 地址
- `background.js` — 所有 Core HTTP、右键菜单、快捷键、受限页面提示（工具栏角标 `!`）
- `content.js` — 选区 / 点选模式 / 浮层（Shadow DOM，不受页面样式影响）
- `popup.html` `popup.js` — 工具栏弹窗
- `icons/` — 占位图标，等正式设计稿替换同名文件即可
