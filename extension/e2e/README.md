# Magpie Capture — E2E

`e2e.py` drives the real extension in a real Chromium with real mouse events and checks every
save against the Core's HTTP API and database. It is the harness that produced the Capture
section of `docs/INTEGRATION_ACCEPTANCE.md`.

## What it checks

| | |
|---|---|
| A | hover an `<img>` → Magpie badge → arc → **保存** → toast「已保存 ✓ 正在后台分析」→ type a Thought in the toast → `PATCH …/thought` lands → analysis reaches `ready` |
| B | hover → **加批注保存** → overlay → Thought → Enter → material with that Thought in Core |
| C | select text → pill → **保存** → text material equals the selection |
| D | select text → **加批注保存** → Thought → Enter; a second Enter does not create a duplicate |
| E | the shortcut/command path (`captureInTab`, same code ⌥⇧M runs): selection → text overlay; no selection → pick mode → click image → overlay |
| F | same image again → 「已在素材库里（… 收集）」, no new material |
| G | **撤销** in the toast → Core returns 404 for that id |
| H | Core stopped → 「Magpie Core 未启动（…），运行 magpie serve 后重试」+ 重试; Core restarted → retry succeeds |
| P | popup shows 「Core 已连接 · N 条素材」 |
| J | a real 公众号 article: text pill save; `mmbiz.qpic.cn` image saved through the background fetch and analysed with real dimensions |

## Browser: which Chromium can load the extension

**Google Chrome ≥ 137 ignores `--load-extension` / `--disable-extensions-except`** (Google removed
the flags from the branded build). Loading the folder by hand in `chrome://extensions` →
「加载已解压的扩展程序」 still works for people, but automation needs a build that honours the
flag: **Chromium**, **Brave**, **Microsoft Edge**, or **Chrome for Testing**. Verified here with
Brave 153 (Chromium 153).

```bash
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --remote-debugging-port=9222 --user-data-dir=/tmp/magpie-e2e-profile \
  --load-extension="$PWD/extension" --disable-extensions-except="$PWD/extension" \
  --no-first-run --no-default-browser-check --window-size=1400,1000 about:blank
```

## Run

```bash
.venv/bin/magpie serve --port 8765                  # Core with real models (saves get analysed)
uv pip install --python .venv/bin/python playwright # CDP client only; no `playwright install` needed
.venv/bin/python extension/e2e/e2e.py --reset --cleanup
```

- `--reset` deletes materials saved from the two test pages by earlier runs (otherwise quick-save
  legitimately reports 「已在素材库里」 and A/F are judged as failures).
- `--cleanup` removes what the run created. Without it the materials stay in the library for inspection.
- `--no-wechat` skips J when `mp.weixin.qq.com` is not reachable.
- Env: `MAGPIE_CORE` (default `http://127.0.0.1:8765`), `MAGPIE_CDP` (default `http://localhost:9222`),
  `MAGPIE_E2E_PAGE` / `MAGPIE_E2E_WECHAT` (test pages), `MAGPIE_ACCEPT_OUT` (results json),
  `MAGPIE_RESTART_CMD` (how step H brings the Core back; default `nohup .venv/bin/magpie serve …`).
- Step H stops the Core on purpose. If your shell kills background children on exit, start the
  Core again yourself afterwards.

Exit code is 1 when any check fails; the per-check log says which and why. Run it against a
scratch `MAGPIE_DATA_DIR` if you do not want test materials in your library.
