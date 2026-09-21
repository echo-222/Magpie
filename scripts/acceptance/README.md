# Integration acceptance harness (2026-09-21)

The scripts that produced the evidence in `docs/INTEGRATION_ACCEPTANCE.md`. They are **not**
CI tests: they need a running Core with real models, a Chromium-based browser started with the
unpacked extension and remote debugging, and they create/delete materials and packs in the
library they point at. Run them against a scratch `MAGPIE_DATA_DIR` unless you want that.

```bash
# 0. Core under test (any branch), real models
.venv/bin/magpie serve --port 8765

# 1. a Chromium that still honours --load-extension (Brave / Chromium / Chrome for Testing;
#    Google Chrome ≥ 137 ignores the flag — load the folder manually via chrome://extensions instead)
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --remote-debugging-port=9222 --user-data-dir=/tmp/magpie-browser-profile \
  --load-extension="$PWD/extension" --disable-extensions-except="$PWD/extension" \
  --no-first-run --no-default-browser-check --window-size=1400,1000 about:blank

# 2. python deps (Playwright is only used as a CDP client, no browser download needed)
uv pip install --python .venv/bin/python playwright

# 3. run
.venv/bin/python scripts/acceptance/extension_acceptance.py   # A–J: hover save, thought, pill, undo, shortcut path, Core down, WeChat
.venv/bin/python scripts/acceptance/ui_acceptance.py          # U1–U19: 素材库 UI incl. drawer, add, pack edits, export (screenshots in $MAGPIE_ACCEPT_OUT)
.venv/bin/python scripts/acceptance/agent_requests.py         # R1–R5: five real requests through /search (agent) and /packs
.venv/bin/python scripts/acceptance/agent_context_check.py    # feed the exported pack to a second agent (DeepSeek) and measure grounding
```

Environment: `MAGPIE_CORE` (default `http://127.0.0.1:8765`), `MAGPIE_CDP` (default
`http://localhost:9222`), `MAGPIE_ACCEPT_OUT` (screenshots / json, default `/tmp` or `/tmp/ui`),
`MAGPIE_TEST_IMAGE`, `MAGPIE_RESTART_CMD` (how `extension_acceptance.py` restarts the Core after
the "Core down" check), `MAGPIE_ENV` (the `.env` holding `DEEPSEEK_API_KEY` for
`agent_context_check.py`).

Known harness quirks: `extension_acceptance.py` expects the first big image on the Wikipedia
page not to be in the library yet (delete leftovers from earlier runs first); `ui_acceptance.py`
answers every `prompt()` with the same string, so the "move to new group" and "note" checks
report FAIL even though the API shows both edits persisted — verify via `/packs/{id}`.
