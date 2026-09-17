# Phase 1 follow-up: local Qwen3-8B vs DeepSeek v4-pro on the three real tasks

Same library (31 materials: the 30 demo items + one seating-chart screenshot the human added
during their first try), same retrieval (bge-m3 + FTS5, 12 candidates), same prompts. Only the
chat role changed. Vision (qwen2.5vl:7b), OCR and embeddings stayed local in every run.

| | local `qwen3:8b-16k` | `deepseek-v4-pro`, thinking **off** (new default) | `deepseek-v4-pro`, thinking **on** |
|---|---|---|---|
| evidence folder | `./` (SUMMARY.md) | `deepseek/` | `deepseek-thinking/` |
| task 1 total / recompose | ≈ 120 s / 102 s | **20 s** / 17 s | 170 s / 133 s |
| task 2 total / recompose | 141 s / 90 s | **21 s** / 17 s | 140 s / 117 s |
| task 3 total / recompose | 64 s / 43 s | **21 s** / 16 s | 126 s / 104 s |
| task understanding | 18–51 s | 3.5–4.7 s | 22–38 s |
| members placed (of 12) | 9 / 9 / 10 | 12 / 12 / 12 | 11 / 11 / 10 |
| schema retries needed | 0 (after the 16k-context fix; 4k context drifted) | 0 | 0 |
| fallback to retrieval-only | never | never | never |

## Task understanding

- Qwen: correct purpose, but `avoid` sometimes padded with things the task never said
  ("过度装饰性图案", "复杂视觉层次"); queries were mostly literal Chinese phrases.
- DeepSeek: tighter briefs (`avoid=['典型AI蓝紫色']` for task 1; `['网红滤镜', '营销腔']` for task 2)
  and bilingual, reference-shaped queries — e.g. `"smooth glowing AI blue purple gradient website 反例"`,
  `"anti-marketing brand voice sincere"`, `"letterpress Chinese characters old book"` — which is what
  the retrieval step actually needs.

## Selection

- Qwen kept 9–10 of 12 and excluded 1–2; some placements were off (Ando and Nuremberg filed
  under 排版/字体 for an Instagram brand; the Suzhou moon gate kept inside a group with the role
  "反例排除" instead of being excluded).
- DeepSeek thinking-off placed all 12 in each run. With 16 candidates it excluded 5 with sound
  reasons (Braun, sand ripples, Lissitzky, Ma Yuan, concrete for the typography booklet), so the
  "usefulness over similarity" step works; with a tightly retrieved top-12 from this curated library
  it simply found a use for everything. Weak spots: Lissitzky as "强调色参考" for a restrained homepage;
  the rusted shutter under "历史氛围与纹理" for the booklet.
- DeepSeek thinking-on was the most selective and the most precise about roles ("首屏背景白",
  "瑕疵／修复叙事参考", "内页正文版式母本", "底层网格参考，但不可单独使用"); it excluded the human's
  unrelated seating-chart screenshot when it surfaced as a candidate ("剧院座位图与中文字体历史小册子无关").

## Grouping

- Qwen: 3–5 groups, names from the suggested vocabulary (材质/质感, 色彩方向, 排版/字体, 反例).
- DeepSeek: 4–5 groups whose names carry the task ("器物触感与不完美叙事", "背景肌理与纸感底纹",
  "安静场景与光影", "封面框景与色彩隐喻", "克制与反例"); text clippings land in a 文案语气 group
  only when the task asks for a voice (task 2), and 《长物志》"宁朴无巧" finally shows up as a copy
  principle — Qwen never selected it.

## Match reasons

- Qwen: specific but short, occasionally wrong about the material (Braun SK4 described as "哑光黑与白").
- DeepSeek: consistently builds on the Human Thought and says what to do with the material —
  "设计师看重它'白但不是屏幕的白，有云一样的纹理'；可作封面/扉页底色", "适合封面大字或章节标题",
  "可用于产品描述、标题和 bio，用安静、留白的语气取代'强大''革命性'等词汇". No material
  mis-descriptions were found in the six DeepSeek runs. Kintsugi — whose gold repair the local
  vision model had missed — is used correctly via the thought ("瑕疵／修复叙事").

## Human edits / persistence / Copy for Agent

Re-checked over HTTP on a DeepSeek-built pack: remove, move (into a new group), note, alternatives
(DeepSeek reasons in ≈ 3 s; the removed material is not re-suggested), Markdown export, reload.
No behaviour change; 29 offline tests pass.

## Cost of the default choice

`DEEPSEEK_THINKING=off` is the default because a Task → Pack in ≈ 20 s is what the dev UI needs
for the Human to iterate. Switch to `on` in `.env` when quality matters more than the wait
(≈ 2–3 min per pack, roughly Qwen's speed with much better reasoning).
