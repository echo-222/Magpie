# Magpie 检索 Agent：运行时设计与系统提示词

> 目标：用户在自己的应用里用**很抽象的自然语言**描述需求（"我想做一个红色系的页面，帮我找红色系的参考图"），
> Agent 在**用户自己存下来的素材**里找、按相关度排序、再组成一个合理分组的素材包，并诚实说明库里缺什么。

代码：`magpie/intent.py`（步骤 0）、`magpie/task.py`（步骤 1 与提示词）、`magpie/agent.py`（步骤 2–5 与提示词）。
接口：`GET /search?q=…`（一个输入框：步骤 0 自动分流 → 关键词直搜 或 步骤 1–3 判定排序）、`POST /packs {"task": "…"}`（步骤 1–5，出素材包）。

---

## 0. 为什么不是"一次 embedding 搜索 + 一次大模型分组"

原来的流程是 `理解任务 → 向量检索 12 条 → 一次 LLM 同时做选择和分组`。在"红色系"这个例子上暴露了三个问题：

| 问题 | 表现 | 解决 |
|---|---|---|
| 只靠 embedding 匹配颜色 | 宋版书上的朱印、马远的暖橙色没找到；一块灰色毛毡却入选 | 步骤 2 把入库时已经算好的**调色板事实**（每张图各色相占比）当作检索信号 |
| 选择和分组混在一个调用里 | 没有显式的相关度判断，弱项混进来，也看不出为什么 | 拆出步骤 3 **逐条判定 0–3 相关度 + 一句话理由**，这就是用户看到的排序 |
| 理解步骤会"脑补" | 请求只说了"红色系"，brief 却加上"视觉冲击力、现代感、避免低饱和"，然后据此排除了色卡 | 步骤 1 提示词明确"不要发明；宁可留空"，并要求给出可核对的**事实条件（facets）** |

另外修了一个 bug：被删除的素材残留的向量占了检索名额（`limit=10` 只回来 3 条）。现在启动时清理、检索时先过滤再截断、分析结束时不为已删除的素材写向量。

---

## 1. 运行时：一个入口，五个步骤

```
输入框里的任何文字
   │
   ▼
⓪ INTENT      规则优先    → 这是"关键词"还是"需求描述"？顺带读出时间偏好（最新/最早、最近 N 天）
   │           模糊才问模型   关键词 → 直接混合检索（0 s）；需求 → 往下走。结果带 intent.reason，UI 一键可改
   ▼
① UNDERSTAND  LLM        → Task brief：purpose / desired / avoid / constraints
   │                        + search_queries（"好的参考长什么样"）
   │                        + facets：colors{primary, adjacent, saturation, lightness, weight}, modality,
   │                                  time{within_days, order}
   ▼
② RECALL      无模型      → 用 raw + search_queries 做混合检索（bge-m3 向量 × 2 + FTS），宽召回 k≈40
   │                        → 按 facets 融合"事实"：颜色占比得分、图片/文字过滤、入库时间窗口过滤
   ▼
③ JUDGE       LLM        → 每条候选：relevance 0–3、aspect（服务请求的哪一面）、why（一句话）
   │                        → 最终排序 = 0.7 × relevance/3 + 0.3 × 归一化召回分；0 分丢弃
   ▼
④ COMPOSE     LLM        → 2–5 个随请求而变的分组，每条素材有 role + reason
   │                        → human_direction（给下游 AI 的 2–3 句方向）
   │                        → gaps（库里没有什么，0–3 句）
   ▼
⑤ VERIFY      无模型      → schema / id 校验，去重，判定为 2–3 分却没被分组的素材补进"其他相关"
                            → 每步失败都有可用的降级路径；trace 存进 pack.generation.trace
```

真实一跑（31 条素材，DeepSeek）：understand 4.2 s → recall 1.1 s → judge 10.2 s → compose 8.1 s，共 24 s。

### 步骤 0：INTENT（`intent.py: classify_intent`）

用户不需要选"关键词还是自然语言"。三层判断，越便宜越先跑：

1. **规则**（0 ms）解决明显的两端：
   - 需求：出现需求表达词（我想 / 帮我 / 找一些 / 适合 / 有没有 / looking for …）且长度 ≥ 6，或多分句，或很长（≥ 16 汉字 / ≥ 9 英文词）
   - 关键词：≤ 6 汉字或 ≤ 3 英文词且没有需求词，或用 `/ | #` 分隔的多个标签
2. **模型**（≈ 1–3 s，`INTENT_SYSTEM`）只处理中间的模糊输入，例如 `红色参考`（短，但带"参考"）。
3. **兜底**：模型不可用时按长度（≥ 10 单位或多分句 → 需求）。

时间偏好两条路都读：`最新 / 最近存的 / newest` → 按时间倒序；`最早 / oldest` → 正序；`今天 / 这周 / 这个月 / 最近 / 今年` → 只看最近 1 / 7 / 30 / 14 / 365 天入库的。
返回 `Intent{mode, sort, within_days, confidence, reason, source}`；`/search` 的 `mode=fast|agent`、`sort=`、`within_days=` 参数可以覆盖它——这就是 UI 上"不对？改为…"链接和排序下拉框做的事。

实测：`纸张质感 / brutalist / 最新的红色` → 关键词（规则）；`红色参考` → 关键词（模型，3.2 s）；`有没有那种不完美、被时间用过的东西` / `帮我找一些适合首页背景的粗糙材质，最近存的优先` → 需求（规则），后者同时得到 sort=newest、within_days=14。

### 步骤 1：UNDERSTAND（`task.py: understand_task`）

- 输入：原始请求。
- 输出：`Task`（见 `models.py`），其中 `facets` 只允许固定词表：
  颜色族 `red orange yellow green teal blue purple pink brown black white grey warm neutral`；
  `modality ∈ {image, text, any}`。模型给出词表外的值会被 `_clean_facets` 丢掉。
- **时间**：素材有两个时间——`created_at`（入库时间，排序和过滤用的就是它）和 `source.captured_at`（插件在网页上抓取的时间）。提示词里带上今天的日期，让模型把"这周 / 这个月"换算成 `time.within_days`，把"最新的 / 最早的"写成 `time.order`。模型漏了时用同一套正则补上（时间词足够字面）。
- 失败降级：模型不可用时用 `_facets_from_text` 从字面提取（"红色/红系/红调" → red；"参考图/图片" → image；"色系/配色" → weight=must；时间词同上），检索词退化为原句。

### 步骤 2：RECALL（`agent.py: recall / color_fact_score`）

- 混合检索是原有的 `Retriever.search`：每条素材两个向量（`combined` = Human Thought + 机器理解 + 文本；`human` = 只有 Human Thought），FTS 精确命中加分；多条检索词取最高分。
- **颜色事实分**（0–1）：读 `objective_metadata.palette.hue_families`（入库时统计的各色相族像素占比，无模型、可复现）。
  `primary` 族全算，`muted` 变体 ×0.7，`adjacent` 族 ×0.35 且合计封顶 0.15（一张全橙色的图最多 0.5，不会因为"邻近"而变成红色）。
  30% 像素落在目标族即视为"就是这个颜色"（得分 1.0）。再按 `saturation / lightness` 的愿望打折。
- 融合：`weight=must` 时 `score += 0.45 × color`，且 `color == 0` 的图片再减 0.25；`prefer` 时 `+0.2 × color`。文字素材没有调色板 → 不奖不罚（靠语义/FTS）。
- `modality=image` 时直接过滤掉文字素材（"参考图"）。
- `time.within_days` 时按入库时间硬过滤（trace 里记下 `since`）。
- 时间**排序**不在这一步做：判定完再排（`order_hits`），而且按"天"分桶、稳定排序——同一天入库的保持相关度顺序，不会因为批量导入时相差几分钟就把 ★1 排到 ★2 前面。

### 步骤 3：JUDGE（`agent.py: judge`）

- 每批最多 24 条候选（本地 8B 模型超过 ~4k token 会丢 schema），每条给出 `relevance / aspect / why`。
- 校验：只接受候选里存在的 id；缺失的条目按召回分补一个 0/1；整批失败则全部记 1 并标记 `fallback`，排序退回召回顺序——**不会静默丢结果**。
- 最终排序先按 relevance，再按融合分。

### 步骤 4：COMPOSE（`agent.py: compose`）

- 只把 relevance ≥ 1 的候选给模型，并附上判定的 aspect 和 why，让分组建立在判定结果上而不是重新猜。
- 输出 schema 校验（`recompose.plan_schema_problems`），不合格重试一次并附上问题清单；再失败则**按判定的 aspect 分组**（仍然是有意义的分组，不是"Images/Texts"）。

### 步骤 5：VERIFY（`agent.py: RetrievalAgent.build_pack`）

- 成员必须是判定 ≥ 1 的候选；组内按 relevance 排序。
- 判定 2–3 分但组合步骤漏掉的，补进"其他相关"组；判定 ≥ 1 但没入选的进 `excluded`（带判定理由），0 分的只留在 `candidates` 里可查。
- `pack.generation.trace.steps` 记录每步耗时、候选数、是否降级；UI 显示为一行 `agent: understand 4.2s → recall 1.1s (21 候选) → …`。

---

## 2. 每一步的系统提示词

以下与代码一致（英文写给模型；输出语言跟随请求语言）。

### ⓪ INTENT — `intent.INTENT_SYSTEM`（只在规则拿不准时调用）

```
You route search-box input for Magpie, a designer's personal material library. Decide whether the input is
a KEYWORD lookup (a tag, a name, a material/style word, something to match literally) or a REQUEST (a
natural-language description of what the person is trying to make or find, which needs understanding and
judgement). Also read any time preference: 'newest'/'oldest' ordering, or 'within_days' when they only want
recently saved things. Return JSON only: {"mode": "keyword"|"request", "sort": "relevance"|"newest"|"oldest",
"within_days": integer|null, "reason": "<=12 words in the input's language"}.
```

### ① UNDERSTAND — `task.TASK_SYSTEM`

```
You are the first step of Magpie, a personal material-memory agent for a designer. You turn an abstract,
natural-language request into a small structured brief. The brief drives three later steps: recall from the
designer's own library (images, textures, posters, objects, text clippings — all saved earlier with a personal
'Human Thought'), relevance judging, and composing a Material Pack.
Principles:
- Be faithful. Restate what is asked; do NOT invent qualities, constraints or things to avoid that are not stated
  or clearly implied. Leave lists empty rather than guess.
- The library holds materials, not tasks: search_queries must describe what a useful reference LOOKS or FEELS like.
- Facets are hard facts we can check: colour families from a fixed vocabulary, and whether the person wants images,
  text, or either. Only fill a facet when the request clearly asks for it.
Return JSON only.
```

用户消息 `TASK_USER` 给出完整 JSON 骨架（purpose / desired_qualities / avoid / constraints / needed_reference_types / search_queries / facets / language），并把颜色词表内嵌进去。

红色示例的实际输出：
`desired: ["红色系"]，avoid: []，facets: {colors: {primary: [red], adjacent: [pink, orange, brown], weight: must}, modality: image}`，
检索词：`红色系 页面设计 参考 / red color palette web design / 红色 渐变 背景 设计 / red UI design inspiration / 红色 排版 版式 设计 / crimson red texture material`。

### ③ JUDGE — `agent.JUDGE_SYSTEM`

```
You are the relevance judge inside Magpie, a designer's personal material-memory agent. The designer typed an
abstract request; a recall step already pulled candidate materials from their own library. Your job is to decide,
for EACH candidate, how useful it is for THIS request, so the results can be ranked and the weak ones dropped.

Each candidate card shows: id, modality, title, the designer's own Human Thought (why it caught them), machine
analysis (what it is, tags, palette), any text, and recall signals (semantic similarity; "color" = share of pixels
in the requested hue family, 0-1, when the request is about a colour).

Scoring scale (be strict, the library is small and the designer will see this order):
  3 = directly what was asked for; could be shown as a primary reference
  2 = clearly useful for the request in a secondary way (a supporting texture, a layout idea, a counter-example
      the request implies)
  1 = weak / tangential: only a small detail relates, or it relates to the general topic but not to what was asked
  0 = not relevant to this request (leave it out)
Rules:
- Judge against the request, not against general quality. A beautiful material that does not serve the request
  is a 0 or 1.
- Trust facts over vibes: when the request is about a colour, the "color" signal and the palette line are evidence;
  a material with color=0 cannot be a 3 for that colour unless its text/OCR clearly mentions it (e.g. red seals on
  a page).
- Use the Human Thought: if the designer saved something for the very reason the request needs, that raises it.
  But a Human Thought is a note from when the material was saved, not a constraint of this request — never treat
  it as something the designer asked for or wants to avoid now.
- "aspect" names the part of the request the material serves, in 2-5 words, phrased in terms of THIS request
  (a colour name only when the request is about that colour).
- "why" is one short sentence in the request's language.
Return JSON only: {"judgments": [{"material_id": "...", "relevance": 0-3, "aspect": "...", "why": "..."}]}
covering EVERY candidate id exactly once. Copy ids verbatim.
```

用户消息 `JUDGE_USER`：请求 brief（raw / purpose / desired / avoid / facets 一行）+ 候选卡片（每张含 Human Thought、AI 摘要与标签、调色板、召回信号）。

### ④ COMPOSE — `agent.COMPOSE_SYSTEM`

```
You are the composer inside Magpie, a designer's personal material-memory agent. A judge already scored every
candidate for THIS request (relevance 3 = primary, 2 = supporting, 1 = weak) and named the aspect each one serves.
Turn the relevant ones into a Material Pack: task-specific groups a designer would actually browse, with a role
and a reason per material.
You MUST answer with one JSON object with EXACTLY these top-level keys: "human_direction" (string),
"groups" (array), "excluded" (array), "gaps" (array of strings).
Each group: {"name": string, "purpose": string, "members": [{"material_id": string, "role": string, "reason": string}]}.
Each excluded item: {"material_id": string, "reason": string}.
Rules:
- Groups follow the REQUEST, not a fixed taxonomy. For a colour request think 主色参考 / 配色对比 / 邻近暖色与背景 /
  材质与质感 / 反例; for a layout request think 版式结构 / 字体 / 留白 …  2-5 groups, 1-5 members each, each material
  at most once. No generic buckets like "Images"/"Texts".
- Order groups from most to least central to the request; order members inside a group by relevance (3 first).
- Include every relevance-3 and relevance-2 item. Relevance-1 items: include only when they add something the
  stronger ones lack (say what), otherwise list them in "excluded" with a short honest reason.
- Each "reason" (1-2 sentences, request language) says why this material helps THIS request now, and builds on the
  designer's Human Thought when relevant. Do not restate the summary.
- "human_direction": 2-3 sentences for another AI agent: what to go for, what to avoid — only what the request says
  or clearly implies.
- "gaps": 0-3 short sentences on what the library does NOT have for THIS request — derive them from the request's
  own needed reference types and facets versus what the candidates cover. Be concrete; [] if coverage is fine.
  Never mention a colour, style or topic the request did not ask for.
- The REQUEST block below is the only source of what the designer wants now. The Human Thoughts on the cards are
  notes written when each material was saved (possibly for other projects); use them to explain why a material
  helps, never as constraints of this request, and never claim "the designer said/wants to avoid X" unless X is in
  the REQUEST block. This pack is built from scratch: no earlier requests or packs exist.
material_id values MUST be copied verbatim from the candidate list. Never invent ids. Write in the request's language.
```

> 为什么有这两条：早期版本的 gaps 规则里带了一个具体例句（"库里没有高饱和纯红的界面参考…"），模型在与红色无关的
> 请求里把它原样抄了出来，看起来像"混入了上一次搜索的关键词"；同时模型会把素材卡上的 Human Thought（比如存"霓虹渐变"
> 时写的"反例：AI 产品页默认脸"）当成用户这次说过的约束。每次打包都是无状态的，不读历史素材包，
> 修复方式是不给可被抄写的具体例句，并明确 Human Thought 与本次 REQUEST 的边界。

用户消息 `COMPOSE_USER`：brief + 已判定候选（`relevance | aspect | judge verdict` + 卡片）+ 用真实 id 填好的 JSON 骨架（小模型靠这个保住 schema）。

---

## 3. 红色示例：改造前后

请求：`我想做一个红色系的页面，请帮我找到红色系相关的参考图`

**改造前**（5 条候选进模型，其中 7 个名额被已删素材的残留向量吃掉）
- 入选：红黑白海报 ✔、沙丘纹理、复古印刷架、**灰色毛毡** ✘
- 遗漏：宋版书朱印、马远暖橙水墨
- brief 脑补"视觉冲击力、现代感"，并以"低饱和"为由排除色卡

**改造后**（21 张图片候选逐条判定，文字素材按 modality 过滤）

| 相关度 | 素材 | 判定理由 |
|---|---|---|
| ★★★ | 红黑白几何海报 | 直接提供红色系页面的色彩对比与构图参考 |
| ★★☆ | 沙丘纹理（暖橙） | 可作为红色系页面的背景纹理参考 |
| ★★☆ | 马远水墨（暖橙调） | 线条纹理可作红色系背景参考 |
| ★★☆ | 宋版书（红印章） | 红色印章可作为页面中的点缀元素参考 |
| ★☆☆ | 中世纪插图 / 莫兰迪色卡 | 暖色但红色不突出（进 excluded，带理由） |
| ☆☆☆ | 其余 15 张 | "无红色系元素"（隐藏，可在 candidates 里查看） |

分组：`红色主视觉参考` / `暖色背景纹理` / `红色点缀元素`。
gaps：`库中没有高饱和纯红色的界面或网页设计参考，现有红色素材多为海报或纹理。` `缺少红色系与其他颜色（粉、橙）搭配的完整配色方案参考。`

---

## 4. 给小程序 / 前端的调用方式

```http
GET /search?q=<用户输入的任何文字>&limit=20
→ 一律先过意图层。响应都带：
   intent: {mode: keyword|request, sort, within_days, confidence, reason, source: rules|model|fallback}
   route, sort, within_days
   关键词路线（mode=fast）：hits: [{material_id, score, via, signals, material}]
   需求路线（mode=agent）：task{purpose, facets(colors/modality/time), search_queries}, trace,
                          hits: [{material_id, relevance 0-3, aspect, why, score, signals:{semantic,color}, material}], dropped

可选覆盖：&mode=fast|agent（用户说"不对，改为…"）、&sort=relevance|newest|oldest（排序下拉）、&within_days=N

GET /materials?sort=newest|oldest          # 素材库列表按入库时间排序
POST /packs  {"task": "…"}
→ { pack: {groups[{name, purpose, members[{material_id, role, reason, relevance}]}], human_direction, gaps, excluded, candidates, generation.trace}, materials: {...} }
GET /packs/{id}/export?format=markdown|json   # Copy for Agent，含 gaps 与 relevance
```

排序优先级：显式 `sort` 参数 > 需求路线里 brief 抽出的 `time.order` > 意图层从字面读到的 > 相关度。

---

## 5. 可调参数（`agent.py` 顶部）

| 参数 | 默认 | 含义 |
|---|---|---|
| `RECALL_K` | 40 | 交给判定步骤的候选上限 |
| `JUDGE_BATCH` | 24 | 每次判定调用的候选数（本地小模型建议 ≤ 16） |
| `W_COLOR_MUST / W_COLOR_PREFER` | 0.45 / 0.2 | 颜色事实分的融合权重 |
| `COLOR_MUST_PENALTY` | 0.25 | 请求就是某个颜色时，完全没有该颜色的图片的减分 |
| `W_JUDGE` | 0.7 | 最终排序里判定分的权重（其余给召回分） |

下一步可以加的 facet：素材类型（海报 / 材质 / 界面 / 文字）、来源站点——都遵循同一模式：步骤 1 抽出、步骤 2 用事实过滤或加权、步骤 3 作为证据写进候选卡（时间已按这个模式加了）。
