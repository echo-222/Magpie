"""Section 6: use the Copy-for-Agent Markdown as the ONLY context for a second agent (DeepSeek) and
check that the answer is grounded in the pack (cites material ids / thoughts / palette)."""
import os, re, time
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(os.environ.get("MAGPIE_ENV", ".env"))
md = open(OUT + "/copy_for_agent.md", encoding="utf-8").read()
ids = set(re.findall(r"mat_[0-9a-f]{10}", md))
c = OpenAI(base_url=os.environ["DEEPSEEK_BASE_URL"], api_key=os.environ["DEEPSEEK_API_KEY"], timeout=300, max_retries=0)
system = "你是一个独立的设计助理 Agent。你只能依据用户提供的 Material Pack 上下文工作，不得引入上下文之外的素材；引用素材时必须写出其 mat_ id。"
user = f"""下面是 Magpie 导出的 Material Pack（Copy for Agent 的原文）。请据此为这个活字印刷小型展览写一份可执行的《海报 + 展签视觉规范》，包含：
1) 主色/辅色 hex（只能来自上下文里的 Palette）；2) 材质与背景处理；3) 字体与排版方向；4) 展签版式；5) 明确禁止事项。
每一条都要标注依据的素材 id，并说明用到了设计师的哪句 Human Thought。最后单独列出"上下文没有覆盖、需要补充的素材"。

=== MATERIAL PACK ===
{md}
"""
t = time.time()
r = c.chat.completions.create(model=os.environ.get("DEEPSEEK_CHAT_MODEL", "deepseek-v4-pro"), messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.3, max_tokens=6000, extra_body={"thinking": {"type": "disabled"}})
ans = r.choices[0].message.content
dt = time.time() - t
cited = set(re.findall(r"mat_[0-9a-f]{10}", ans))
hexes = set(h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", ans))
pack_hexes = set(h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", md))
thoughts = [l.strip("> ").strip() for l in md.splitlines() if l.startswith("> ")]
quoted = [t for t in thoughts if t and any(frag in ans for frag in [t[:8], t[-8:]])]
print(f"{dt:.0f}s | answer {len(ans)} chars | pack ids {len(ids)} | cited ids {len(cited)} (all valid: {cited <= ids}) | hex used {len(hexes)} (from pack: {len(hexes & pack_hexes)}/{len(hexes)}) | thoughts echoed {len(quoted)}/{len(thoughts)}")
print("invalid ids:", cited - ids)
open(OUT + "/agent_context_answer.md", "w", encoding="utf-8").write(ans)
print("\n--- answer head ---\n" + ans[:1800])
