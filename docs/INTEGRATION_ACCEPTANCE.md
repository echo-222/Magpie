# Magpie 集成验收：Capture + Retrieval Agent + 新 UI（PR #2 / PR #3）

日期：2026-09-21 · 验收人：local agent（受 陈彬 委托）· 结论：**可以合并，先合 #2 再合 #3，无需 rebase；两处 P1 建议合并后立即修**

验收环境：PR #3 head `820ce78` 在独立 worktree 运行，`.env` 沿用本机配置（chat = DeepSeek `deepseek-v4-pro`，thinking off；vision = 本地 `qwen2.5vl:7b`；embedding = 本地 `bge-m3`），指向**真实素材库** `data/magpie.db`（开始时 31 条素材、14 个素材包，验收前已做 SQLite 备份 `data/backup-before-pr3-acceptance/`）。扩展在 Brave 153（Chromium 153）以未打包方式加载并通过 CDP 驱动真实鼠标事件。所有 HTTP 结果都以 Core 数据库为准核对。复现脚本见 `scripts/acceptance/`。

---

## 1. 当前项目整体状态

| | commit | 状态 |
|---|---|---|
| `main` | `198788c` Merge Phase 1 | Core Phase 1 + DeepSeek + Capture API Contract；31 个 pytest |
| PR #2 `frontend/capture-mvp` → main | head `99cb203`（3 commits） | OPEN · MERGEABLE/CLEAN · 只增 `extension/`（10 文件，+1064） |
| PR #3 `guanguan/retrieval-agent-and-ui` → main | head `820ce78`（6 commits） | OPEN · MERGEABLE/CLEAN · +4047/−379，32 文件 |

事实核对：`git merge-base --is-ancestor` 确认 **#2 的 3 个 commit 原样是 #3 的前 3 个 commit**（`git cherry` 无差异）；两个 PR 都基于当前 `main`，均可 fast-forward，互相无冲突。PR #3 的 Python 侧 63 个 pytest 全绿（main 31 + 新增 32）。PR 分支上无 API Key 痕迹。

链路现状：`网页 → 扩展 Capture（悬停/选区/快捷键）→ POST /materials → 后台分析 → 素材库 UI → 自然语言检索（intent → agent）→ 素材包（understand → recall → judge → compose → verify）→ 人工编辑 → Copy for Agent` 已在真实环境**完整跑通一次**（§4）。

## 2. PR #2 完成了什么（`frontend/capture-mvp`）

Manifest V3 扩展，纯 JS，无构建：`background.js`（所有 Core HTTP、右键菜单、快捷键 ⌥⇧M、受限页面角标）、`content.js`（选区 / 点选模式 / Shadow DOM 浮层：预览 · 一行 Human Thought · Enter 保存 · Esc 取消）、`popup`（Core 状态、在当前页收集、Core 地址）。三个 commit：初版 Capture MVP；撤销 + 懒加载/`srcset`/`#fragment` URL 处理 + README + 图标；保存后锁表单防二次 Enter。严格按 `docs/CAPTURE_API_CONTRACT.md` 调用 `POST /materials`、`GET /materials/{id}`、`PATCH …/thought`、`DELETE …`、`GET /health`。

## 3. PR #3 完成了什么（`guanguan/retrieval-agent-and-ui`，含 #2）

- **扩展（`abeb959`）**：悬停收集——鼠标停在 ≥64×64 的 `<img>` 上出现 Magpie 圆点，悬停展开「保存 / 加批注保存 / 打开素材库」；选中文字松手出现「保存 / 加批注保存」小条；一键保存后右下角 toast 可当场补一句 Thought（PATCH）、撤销、打开素材库；喜鹊 logo。
- **Core（`f34e8c7`）**：`magpie/agent.py` 检索 Agent 五步：understand（brief + facets：颜色族/模态/时间）→ recall（混合向量 + FTS，宽召回 k≈40，颜色请求时融合入库时算好的色相族占比这一"事实"）→ judge（LLM 逐条 0–3 相关度 + aspect + 一句判词，分批）→ compose（任务专属分组 + 角色 + 理由 + 面向另一 Agent 的 direction + 诚实的 gaps）→ verify（schema/id 校验、判定 ≥2 却被漏掉的素材进「其他相关」组、trace 存进 pack）。`magpie/intent.py` 给搜索框做意图判断（关键词 vs 自然语言需求，规则优先），`/search` 一个入口两条路，带 `sort`/`within_days`。`db.prune_orphan_embeddings()`（启动时和每次分析后清理已删素材残留向量）、`ingest` 在素材被删时不再回写向量、`retrieval.search` 先挂素材再截断。`POST /packs/{id}/rename`。`recompose.Recomposer` 委托给 agent，CLI `magpie pack` 仍可用。新增 32 个测试（gate D agent、gate E intent/time）。`docs/RETRIEVAL_AGENT.md`。
- **UI（`820ce78`）**：`web/index.html` 重写为侧栏三视图「检索素材 / 加入素材 / 素材包」：masonry 素材墙（Human Thought 棕色、色板、入库时间、图/文筛选）、一个搜索框自动判断意图并显示"理解 / 事实条件 / 检索词 / 各步耗时"、结果卡带 ★ 相关度 + aspect + 判词、素材抽屉（原图/原文、可编辑 Thought、AI 理解 + 标签 + 色板、图中文字、来源、相似素材、重新分析 / 以此为任务找参考 / 删除）、加入素材（拖放/粘贴/文字 + Thought + 折叠的来源）、素材包（任务框、brief、direction、库里缺什么、trace、分组 ★ 角色 理由、移出 / 移到 / 找替代 / 备注、Copy for Agent / JSON / 改任务重跑、历史列表 + 筛选 + ✕ 删除、双击重命名包/组）。

## 4. 完整闭环实际跑通情况

一次真实连贯的链路（时间为北京时间 2026-09-21 15:30 前后）：

1. **Capture**：Brave 中打开 Wikipedia *Letterpress printing*，悬停第二张图 → 「加批注保存」→ 浮层输入 Human Thought「B: 老印刷机的金属感，想用在关于工艺的页面」→ Enter → 「已保存 ✓ 正在后台分析」→ Core 出现 `mat_579d5b5f78`（image，source.page_url / page_title / resource_url / captured_at 齐全，thought 原样）。同页选中一段正文 → 「加批注保存」→ 「D: 这一段讲压印的动作，是物质感的来源」→ `mat_c1ad120351`（text，`original.content` 与选区逐字一致）。
2. **AI Analysis**：图片 ~10 s 后 `ready`（qwen2.5vl 摘要「早期印刷场景，一位工人正在操作印刷机…」，标签 复古/黑白/印刷术…），文字 ~3 s `ready`。
3. **新任务**（UI 素材包视图）：「给一个讲活字印刷历史的小型展览做视觉方向：海报和展签，要有纸和金属的物质感，克制，不要博物馆式的严肃，也不要蓝紫科技感。」
4. **Retrieval Agent**：understand 5 s（desired 纸的物质感/金属的物质感/克制；avoid 博物馆式的严肃/蓝紫科技感；facets 颜色 warm neutral/black/white/grey prefer、只看图片）→ recall 0.1 s（25 候选）→ judge 26 s（7×3、3×2、12×1、3×0）→ compose 29 s。**刚采集的两张印刷机照片都被判 3**，进入「活字印刷工具与金属感」组（角色「老印刷机金属感参考」「印刷设备细节参考」）；同批采集的公众号聊天截图被判 0（「与活字印刷展览视觉方向完全无关」）。分组：纸与墨的物质感 / 活字印刷工具与金属感 / 传统中文排版与历史版面 / 克制的构图与留白。gaps：「缺少直接展示金属活字或铅字细节的特写图片」等 3 条，具体、不空泛。
5. **人工编辑**：移出 1 条（`removed_material_ids` 记录，导出中列为 Deliberately left out）、移到新组、加备注、找替代（替代里出现了同批采集的压印文字 `mat_c1ad120351`，理由「直接关联活字印刷的物质感来源」）、重命名素材包 → 全部持久化，`human_edits = [remove, move, note]`。
6. **Copy for Agent**：导出 10,014 字符 Markdown（任务、direction、gaps、每组每条的 Human Thought / Why selected / AI 分析 / 色板 / 来源 / 人工备注 / 人工移除清单）。
7. **喂给另一个 Agent**：把这段 Markdown 作为唯一上下文交给 DeepSeek（system：只能用上下文里的素材并引用 id），要求写《海报 + 展签视觉规范》。45 s 返回 3,097 字：引用了 10 条素材中的 9 条、全部 id 合法；11 个 hex 里 10 个直接来自 pack 色板（另 1 个是它禁止使用的 `#ffffff`）；逐字引用了 8 句 Human Thought 中的 5 句；遵守了「Deliberately left out」的 `mat_c35b3ff8c7` 不得使用；把 pack 的 3 条 gaps 原样列为需补充素材，还指出色板未单独提取朱印红。

结论：**Capture → Memory → Task → Recompose → Agent Context 这条链第一次在真实数据、真实模型、真实浏览器上完整成立**，而且新采集的素材确实被新任务找回并被下游 Agent 用上。证据：素材包 `pack_41ace7456d`（已改名「验收：活字印刷展览视觉方向」，保留在库中）、`mat_579d5b5f78`、`mat_c1ad120351`（保留）。

## 5. 真实测试结果

### 5.1 pytest
- `main`（`198788c`）：31 passed。
- PR #3（`820ce78`，独立 venv）：**63 passed**（含新增 `test_gate_d_agent.py`、`test_gate_e_intent_time.py`）。
- PR #2 无 Python 改动。

### 5.2 扩展 E2E（真实浏览器 + 真实 Core）
仓库里**没有** PR 描述所称的 E2E 代码（README 写了 61 项 + 21 项检查，但未提交，见 P1-c）。本次用 `scripts/acceptance/extension_acceptance.py` 在 Brave 153 跑，两轮合计覆盖 A–J；下表是 Core 数据核对后的结论（一轮里 A/F 的 4 项 FAIL 是脚本卫生问题：前一次被中断的运行已把同一张图存进库，所以变成了"已在素材库里"，扩展行为正确）。

| 项 | 结果 | 证据 |
|---|---|---|
| A 图片 hover → 直接保存 | ✅ | 圆点 → 环形菜单 → 保存 → toast「已保存 ✓ 正在后台分析」+ 补 Thought 输入框 + 撤销；Core `POST 201`，source 四字段正确；toast 里输入 Thought 回车 → `PATCH …/thought 200`，库中可见 |
| B hover → 加批注保存 | ✅ | 浮层 → Thought → Enter → 「已保存 ✓」；`mat_579d5b5f78` thought 原样 |
| C 选中文字 → 保存 | ✅ | 松手出现小条 → 保存 → toast；`original.content` 与选区一致 |
| D 选中文字 → 加批注保存 | ✅ | `mat_c1ad120351` 内容 + thought 正确；保存后再按 Enter 不重复提交（锁表单生效） |
| E 快捷键 fallback | ✅（路径级） | 通过 service worker 调用与 ⌥⇧M 相同的 `captureInTab`：有选区 → 打开文字浮层；无选区 → 进入点选模式「点击要收集的图片 · Esc 取消」→ 点图 → 浮层。真实按键未由人手按下（自动化无法触发浏览器级快捷键） |
| F duplicate | ✅ | 同图再存 → 「已在素材库里（9月21日 收集）」，不新建；浮层里带 Thought 重存已有 Thought 的素材 → 「原 Thought 保留：…，本次输入未保存」 |
| G undo | ✅ | toast 撤销 → 「已撤销，这条素材已从库里删除」，Core `GET` 404 |
| H Core 关闭 | ✅ | 停掉 Core 后保存 → 浮层「Magpie Core 未启动（http://127.0.0.1:8765），运行 magpie serve 后重试」+ 重试按钮；重启后点重试成功 |
| I 普通网页 | ✅ | Wikipedia（文字 + Wikimedia CDN 图） |
| J 微信公众号 | ✅ | 真实文章 `mp.weixin.qq.com/s/BGXeAXrEo6CM5-gtdQOuYA`：选区保存成功；`mmbiz.qpic.cn` 图片经 background fetch 保存为 `640.webp` 97 KB，分析 `ready` 640×3738 WEBP（真实字节，非占位图） |
| popup | ✅ | 「Core 已连接 · N 条素材 · v0.1.0」，开关/地址/快捷键入口齐全 |

无 silent failure：每一次保存都能在 Core 找到对应记录，失败路径都有可见提示。

### 5.3 素材库 UI（`scripts/acceptance/ui_acceptance.py`，截图见 `$MAGPIE_ACCEPT_OUT`）
U1 计数与 Core 一致 · U2 关键词搜索有结果 + 意图说明 · U3 自然语言请求走 agent（20 s，结果卡带 ★ 与判词）· U4 抽屉含 Thought/AI 理解/来源/相似素材/三个动作 · U5 抽屉里改 Thought 回车即存 · U6 重新分析：analyzing → ready，Thought 未被覆盖 · U7 加入文字 · U8 加入图片（文件选择）· U9 生成素材包（62 s）· U10 包视图有理由与全部动作 · U11 移出持久化 · U12 移到新组持久化 · U13 备注持久化 · U14 找替代给出带理由的候选 · U15 重命名 · U16 Copy for Agent · U17 历史列表 · U18 删除素材包（有 confirm）· U19 抽屉删除素材（有 confirm，Core 404）。**19/19 功能通过**（脚本报 17/19 是 prompt 自动应答的假阴性，已用 API 核实）。

### 5.4 Retrieval Agent（`scripts/acceptance/agent_requests.py`，5 个真实请求）

| 请求 | intent | recall/judge | 结果与判断 |
|---|---|---|---|
| R1 「找红色系的视觉参考」 | request；facets 红 must、邻近 pink/orange/brown、只看图 | 12 候选 → 5 相关 / 7 判 0 | ★3 Lissitzky 红楔子；★2 宋版书页（红印章）；★1 沙丘/纽伦堡/马远（"暖色但非红"）。7 条 0 分的判词全部正确（蓝卷帘门、Synthwave 蓝紫、Braun 中性色…）。**颜色请求用事实（色相族占比）而不是 vibe，判分克制。** 14 s |
| R2 「找克制、有物质感、不要典型 AI 科技感的参考」 | request；modality any | 37 候选 → 判 **20×3、14×2、1×1、1×0** | 分组和理由都对（材质/克制与留白/手工本真/反例/中文排版），Synthwave 与 Corporate Memphis 被正确用作反例。但 **判分严重通胀**：几乎整库都是 3/2，compose 后再加一个 16 条的「其他相关」组，素材包 ≈ 37 条——这不是"包"，是整库重贴标签。库本身就是围绕这个主题建的，所以召回没错，错在 judge 不够严、verify 把所有 ≥2 兜底进包。65 s |
| R3 「给一个手作陶器品牌的首页写文案，帮我找语气和态度上的参考」 | request；facets modality **text** | 12 → 11 相关 | ★3 Morris / 园冶「虽由人作，宛自天开」/ 长物志「宁朴无巧」/ Wabi-sabi；★2 Saint-Exupéry / Rams / 留白；技术类文本 1 分并被 compose 排除并给出理由；gaps「缺少直接来自陶艺家或手作匠人的语录」。**最好的一组结果。** 45 s |
| R4 「我要做一个网页，帮我找一些有用的参考」 | request；brief 的 desired/avoid 为空 | 37 → 7×3、22×2、8×1 | 分组本身可看（中文排版/留白极简/材质/设计哲学与文案/色彩氛围 + 8 条「其他相关」），但 **direction 写成「寻找能支撑一个克制、有质感、不落俗套的网页设计的参考… 避免霓虹渐变、玻璃拟态和 Corporate Memphis」——这些约束用户根本没说，全部来自素材的 Human Thought**。这正是 prompt 里明令禁止、也是这次要重点检查的错误：模糊任务时，历史 Thought 被当成了本次任务约束。77 s |
| R5 「找有历史感的排版和字体参考，但不要西方的东西」 | request；avoid 西方 | 37 → 2×3、5×2、11×1、19×0 | 纽伦堡/Kelmscott/瑞士风格/西方印刷机全部判 0 并说明「属于需要排除的西方风格」；★3 宋版书页、唐碑拓本；东方器物/理念作 2。**排除条件被严格执行。** 63 s |

其他检查：
- **Human Thought 作为历史信息**：R1/R3/R5 里理由引用 Thought 来解释"为什么有用"而不把它当约束，正确；R4 违反（见 P1-a）。
- **recall 遗漏**：五个请求里没有发现库中明显相关却未进候选的素材（R3 的 modality=text 过滤把所有文字都召回了；R1 只看图也合理）。
- **被删素材不回流**：对一条有向量的素材先 `reanalyze` 再立刻 `DELETE`（制造分析中删除的竞态），25 s 后 `materials`/`material_embeddings`/`materials_fts` 均无残留，库中 orphan 向量 0 条，fast/agent 两种搜索都不再出现。✅
- intent 边界：「最近存的粗糙材质」→ request + 最新优先 + 只看最近 N 天；「红色 海报」「brutalist」→ 关键词。合理。

### 5.5 真实 Core smoke
`GET /health`、`/materials`、`/search` 两种路由、`/packs` 全套编辑与导出、`/materials/{id}/similar`、`/static/logo.png` 均正常；PR #3 写入的 pack（新增 `gaps`/`relevance`/`why`/`facets` 字段）切回 `main` 的 Core 后仍能读取（pydantic 忽略未知字段），前向兼容无问题。

## 6. 问题分级

### P0（阻断核心链路 / 数据错误）
**未发现。** 没有做任何代码修改。

### P1（明显影响体验，但能完成任务）

> 跟进（2026-09-21 晚）：#2、#3 已按 §9 顺序合入 main；P1-a、P1-b 在分支 `chenbin/agent-p1-fixes` 修复
> （`agent.py` 精选纪律 + 测试 `tests/test_gate_f_selection_discipline.py`，见 `docs/RETRIEVAL_AGENT.md` §6），
> 同一分支把扩展 E2E 收进 `extension/e2e/` 并在 README 标明 Chrome ≥ 137 的加载限制（P1-c、P1-d）。
> 真实复测：R2 判 3 分从 20 条降到 9 条、素材包从 ≈37 条降到 12 条；R4 的 direction 只复述请求，不再出现库里 Thought 推导的偏好。
- **P1-a 模糊任务时 Human Thought 泄漏为任务约束**（R4）：`human_direction` 把库里 Thought 的偏好（克制、避免霓虹渐变、Corporate Memphis…）写成了用户的要求；judge 的判词里也有「正是中文网站想要的气质」这类源自 Thought 的框定。建议：brief 的 desired/avoid 为空时，compose 只允许 direction 复述请求本身 + 一句「以下是按你库里的整体方向挑的」；或对模糊请求先反问一句。
- **P1-b 宽泛/抽象请求下的判分通胀与兜底组膨胀**（R2、R4）：37 条候选里 20 条被判 3；verify 把所有 ≥2 都塞进「其他相关」，素材包变成整库（R2 ≈37 条、R4 ≈29 条）。建议：judge 加相对约束（每批 3 分数量上限或"只有 N% 可以是 3"），「其他相关」超过 3–5 条时改为折叠的"还可以看"而不是正式成员。
- **P1-c 扩展的 E2E 未入库**：`extension/README.md` 声称的 61 项 + 21 项无头 Chrome 检查在仓库里不存在，无法复现；本次的 `scripts/acceptance/extension_acceptance.py` 可作为起点。
- **P1-d Google Chrome ≥137 忽略 `--load-extension`**：手工「加载已解压的扩展程序」不受影响，但自动化必须用 Chromium / Brave / Chrome for Testing；README 应写明。
- **P1-e 「视觉方向」被推成 `modality: image`**：全闭环任务里 understand 自动只看图片，把文案类文字素材排除在 pack 之外（UI 只显示「只看图片」，没有改回来的入口；找替代时文字又能出现）。建议 facets 的 modality 只在用户明说时生效，或在包视图给一个开关。
- **P1-f 关键词搜索无阈值**：`纸张质感` 返回全部 36 条（只是重排），旁边自然语言模式却是「5 条结果」，两种模式的"结果"语义不一致；建议关键词模式按分数截断或至少标注"按相关度排序的全部素材"。
- **P1-g 生成耗时**：judge（25–45 s）+ compose（20–30 s）让一个包从 Phase 1 的 ~20 s 变成 45–77 s；UI 有进度文案，可接受，但 judge 分批并行或先出 recall 结果再补判定会更好。

### P2（样式 / 微交互 / 以后再说）
- 重命名素材包/组靠「双击 + 系统 prompt()」，仅 tooltip 提示，难被发现；删除用系统 confirm()。
- trace 行「understand 5s → recall 0.1s (25 候选) → judge 26.3s (22 相关)」把 relevance-1 也算成"相关"，且术语面向开发者。
- 移出成员后空组保留（可再移入，但视觉上是空框）。
- intent 的 `within_days=14` 与 brief 里的 `7` 不一致（「最近存的」）。
- 加入素材里"来源"折叠在 details 内，从扩展来的素材无此问题，手工加入时容易漏填。
- 结果卡上 aspect 文案偶有重复（R2 里多张「克制与物质感」）。
- 素材包成员卡的「移到…」下拉包含当前组（已 disabled）——可以，但和「＋ 新组」混排略乱。

## 7. 哪些地方真的有产品价值

- **判词 + 分级 + 事实融合**：R1 里"color=0 不能是 3"这种把色相族占比当证据的做法，让「找红色系」这种最常见的检索第一次可信；R5 的排除条件执行得比多数收藏工具的标签筛选更准。
- **理由建立在 Human Thought 上**：包里每条理由都在解释"你当时为什么记下它，现在为什么有用"（「白不是屏幕的白，有云一样的纹理，适合作为海报背景的纸感参考」），这是 Magpie 区别于书签的核心，并且下游 Agent 真的引用了这些句子。
- **gaps 诚实**：三次都指出库里缺什么（金属活字特写、匠人语录、实际印刷品样例），比一味给结果更有用。
- **Capture 的低打断**：悬停圆点 → 一键保存 → toast 里补一句 Thought，是"先存再想"的正确节奏；公众号图能拿到原字节。
- **一个搜索框两种意图**加上可见的"理解"行，让用户知道系统怎么读的请求，错了可以一键改。

## 8. 哪些地方仍然像普通收藏工具

- 素材墙、抽屉、加入素材、历史列表本身是标准的收藏/看板形态；不查任务，它就是一个有 AI 标签的图库。
- 关键词模式的搜索结果是"整库重排"，和普通图库搜索无差别。
- 模糊任务时（R4）系统退化为"把整库按主题贴标签"，价值取决于库本身而不是任务理解。
- 素材包的编辑动作（移出/移到/备注）是通用列表操作；差异化全在生成那一步。
- 尚无"跨包记忆"：每个包从零开始（这是当前有意的设计，也意味着系统不会越用越懂你）。

## 9. Merge 建议

Git 历史很干净：#3 ⊇ #2，且都 fast-forward 于 main，没有分叉，所以**不需要 rebase 收敛**。建议顺序：

1. **先 merge PR #2**（让前端同学的 PR 以 merged 身份关闭）；
2. **再 merge PR #3**（其中 #2 的 3 个 commit 与 main 上的完全相同，GitHub 会直接合并剩余 3 个 commit，无冲突）；
3. 合并后**立刻**开一个小 PR 修 P1-a / P1-b（都在 `agent.py` 的 prompt + verify 逻辑里，不动数据模型），顺手把 P1-d 写进 README、把 `scripts/acceptance/` 的 E2E 补进扩展 README（P1-c）。P1-e/f/g 可以随后。

不建议为 P1 卡住合并：主链路、数据正确性、删除回流、Core 兼容都通过了，P1 是质量问题而非正确性问题；但 P1-a 关系到 Magpie 的核心承诺（Thought 是历史不是约束），应视为合并后的第一件事。

本次没有执行任何 merge，也没有改动 PR 分支。

## 10. 下一阶段建议（仅建议，未开始）

1. 修 P1-a/b：judge 严格度（每批 3 分上限）、模糊请求的 direction 只准复述请求、兜底组折叠。
2. 用 Human 自己的素材和 Thought 替换/扩充演示库后，重跑 R1–R5 —— 这才是产品验证。
3. 把 `scripts/acceptance/` 收成扩展的正式 E2E（Chrome for Testing/Chromium），加进 PR 检查。
4. 素材包生成的渐进呈现：先出 recall + judge 前几条，再补 compose。
5. 之后再考虑跨包记忆（"上次这个任务你移出了 X"）、MCP 暴露 `search/get_pack`——这些超出本次范围。

---

附：验收后库的状态——素材 33 条（原 31 + 本次采集并保留的 `mat_579d5b5f78`、`mat_c1ad120351`），素材包 15 个（原 14 + `pack_41ace7456d`）。其余测试期间产生的素材/包（公众号截图、UI 上传测试图、R2–R5 与 CLI 生成的包）已删除；SQLite 备份保留在 `data/backup-before-pr3-acceptance/`。8765 端口已切回 `main` 的 Core。
