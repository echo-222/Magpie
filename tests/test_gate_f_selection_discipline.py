"""Gate F — selection discipline (integration acceptance P1-a / P1-b):
vague requests keep Human Thoughts as history, packs stay a selection, judge inflation is capped."""

import pytest

from conftest import make_image_bytes
from magpie import agent as agent_mod
from magpie.agent import Judged, RetrievalAgent, cap_top_relevance, is_vague, trim_to_cap, vague_direction
from magpie.models import CapturePayload, Human, PackGroup, PackMember, Source, Task
from magpie.retrieval import Hit, Retriever


@pytest.fixture()
def big_library(ingestor):
    """Twenty on-theme materials with opinionated thoughts (the 'whole library shares the mood' case)."""
    ids = []
    for i in range(20):
        m = ingestor.ingest_image(
            make_image_bytes(color=(120 + i * 3, 110 + i * 2, 100 + i), text=f"m{i}"),
            f"m{i}.png",
            CapturePayload(modality="image", human=Human(thought=f"克制、有物质感，避免蓝紫科技感 {i}"), source=Source(page_title=f"m{i}")),
        )
        ids.append(m.id)
    return ids


@pytest.fixture()
def agent(db, ingestor):
    return RetrievalAgent(db=db, retriever=Retriever(db=db, llm=ingestor.llm), llm=ingestor.llm)


def _judged(n_three: int, n_two: int = 0, n_one: int = 0) -> list[Judged]:
    out = []
    for i, rel in enumerate([3] * n_three + [2] * n_two + [1] * n_one):
        h = Hit(material_id=f"mat_{i:010x}", score=1.0 - i * 0.01, via="machine")
        j = Judged(h, rel, "aspect", "why")
        j.final = round(agent_mod.W_JUDGE * rel / 3 + (1 - agent_mod.W_JUDGE) * (1 - i * 0.01), 4)
        out.append(j)
    return out


# --------------------------------------------------------------------------- P1-b: judge cap


def test_cap_top_relevance_demotes_excess_threes():
    judged = _judged(n_three=16, n_two=4)  # 80% threes: inflated
    demoted = cap_top_relevance(judged)
    threes = [j for j in judged if j.relevance == 3]
    assert demoted == 11 and len(threes) == 5  # ceil(20 * 0.25) = 5 keep their 3
    # the strongest by fused score keep the 3; demoted ones become 2 with a lower final
    assert all(j.final <= min(t.final for t in threes) for j in judged if j.relevance == 2)


def test_cap_keeps_a_minimum_for_small_batches():
    judged = _judged(n_three=3, n_two=1)
    assert cap_top_relevance(judged) == 0  # 3 threes out of 4 are allowed (TOP_MIN)


def test_cap_does_nothing_when_judge_was_strict():
    judged = _judged(n_three=2, n_two=5, n_one=10)
    assert cap_top_relevance(judged) == 0
    assert sum(1 for j in judged if j.relevance == 3) == 2


# --------------------------------------------------------------------------- P1-b: pack cap


def test_trim_to_cap_drops_weakest_from_least_central_group():
    groups = [
        PackGroup(name="核心", members=[PackMember(material_id=f"a{i}", relevance=3, score=0.9 - i * 0.01) for i in range(6)]),
        PackGroup(name="次要", members=[PackMember(material_id=f"b{i}", relevance=2, score=0.7 - i * 0.01) for i in range(6)]),
        PackGroup(name="边缘", members=[PackMember(material_id=f"c{i}", relevance=2, score=0.5 - i * 0.01) for i in range(4)]),
    ]
    dropped = trim_to_cap(groups, 12)
    assert len(dropped) == 4 and sum(len(g.members) for g in groups) == 12
    assert dropped == ["c3", "c2", "c1", "c0"]  # least central, weakest first; core group untouched
    assert [g.name for g in groups] == ["核心", "次要"]  # emptied group removed


def test_broad_request_pack_stays_a_selection(agent, db, big_library, monkeypatch):
    """FakeLLM judges everything high; the pack must still be capped and the leftovers explained."""
    real = agent.llm.chat_json

    def inflated(system, user, **kw):
        if kw.get("purpose") == "judge":
            import re

            ids = re.findall(r"\[(mat_[0-9a-f]{6,})\]", user)
            return {"judgments": [{"material_id": m, "relevance": 3, "aspect": "克制", "why": "都很克制"} for m in ids]}
        if kw.get("purpose") == "recompose":
            import re

            ids = list(dict.fromkeys(re.findall(r"mat_[0-9a-f]{6,}", user)))
            # composer ignores the cap and dumps everything into one group
            return {"human_direction": "x", "groups": [{"name": "全部", "purpose": "p", "members": [{"material_id": m, "role": "r", "reason": "why"} for m in ids]}], "excluded": [], "gaps": []}
        return real(system, user, **kw)

    monkeypatch.setattr(agent.llm, "chat_json", inflated)
    pack = agent.build_pack("找克制、有物质感、不要典型 AI 科技感的参考", limit=40)
    members = pack.member_ids()
    assert len(members) <= agent_mod.PACK_MAX_MEMBERS
    assert len(set(members)) == len(members)
    judged3 = sum(1 for c in pack.candidates if c.relevance == 3)
    assert judged3 <= max(agent_mod.TOP_MIN, -(-len(pack.candidates) * 25 // 100))  # ceil(25%)
    # everything that did not make the cut is explained, not silently dropped
    explained = {e["material_id"] for e in pack.excluded}
    assert set(big_library) - set(members) <= explained
    assert any("精选上限" in e["reason"] for e in pack.excluded)
    step = next(s for s in pack.generation["trace"]["steps"] if s["step"] == "compose")
    assert step["trimmed"] > 0 and step["stragglers_shown"] <= agent_mod.STRAGGLER_MAX


def test_stragglers_group_is_small(agent, db, big_library, monkeypatch):
    """Composer groups only two items; the judged-relevant rest must not all pile into 其他相关."""
    real = agent.llm.chat_json

    def sparse_compose(system, user, **kw):
        if kw.get("purpose") == "judge":
            import re

            ids = re.findall(r"\[(mat_[0-9a-f]{6,})\]", user)
            return {"judgments": [{"material_id": m, "relevance": 2, "aspect": "a", "why": "w"} for m in ids]}
        if kw.get("purpose") == "recompose":
            import re

            ids = list(dict.fromkeys(re.findall(r"mat_[0-9a-f]{6,}", user)))[:2]
            return {"human_direction": "x", "groups": [{"name": "只选两条", "purpose": "p", "members": [{"material_id": m, "role": "r", "reason": "why"} for m in ids]}], "excluded": [], "gaps": []}
        return real(system, user, **kw)

    monkeypatch.setattr(agent.llm, "chat_json", sparse_compose)
    pack = agent.build_pack("帮我找一些参考", limit=40)
    other = pack.find_group("其他相关")
    assert other is not None and len(other.members) <= agent_mod.STRAGGLER_MAX
    assert len(pack.member_ids()) == 2 + len(other.members)
    assert any("超出精选上限" in e["reason"] for e in pack.excluded)


# --------------------------------------------------------------------------- P1-a: vague requests


def test_is_vague_only_when_brief_is_empty():
    assert is_vague(Task(raw_request="我要做一个网页，帮我找一些有用的参考"))
    assert not is_vague(Task(raw_request="x", desired_qualities=["克制"]))
    assert not is_vague(Task(raw_request="x", avoid=["蓝紫科技感"]))
    assert not is_vague(Task(raw_request="x", constraints=["海报"]))


def test_vague_request_direction_restates_request_only(agent, db, big_library, monkeypatch):
    """The composer leaks the library's thoughts into the direction; the verify step must not let it through."""
    real = agent.llm.chat_json

    def leaky(system, user, **kw):
        if kw.get("purpose") == "task":
            return {"purpose": "制作一个网页，需要寻找有用的参考", "desired_qualities": [], "avoid": [], "constraints": [], "needed_reference_types": [], "search_queries": ["网页 参考"], "language": "zh", "facets": {"modality": "any"}}
        if kw.get("purpose") == "recompose":
            import re

            ids = list(dict.fromkeys(re.findall(r"mat_[0-9a-f]{6,}", user)))[:3]
            return {
                "human_direction": "寻找克制、有质感的网页参考，避免霓虹渐变、玻璃拟态和 Corporate Memphis 风格。",  # invented from thoughts
                "groups": [{"name": "组", "purpose": "p", "members": [{"material_id": m, "role": "r", "reason": "why"} for m in ids]}],
                "excluded": [],
                "gaps": [],
            }
        return real(system, user, **kw)

    monkeypatch.setattr(agent.llm, "chat_json", leaky)
    pack = agent.build_pack("我要做一个网页，帮我找一些有用的参考", limit=40)
    assert is_vague(pack.task)
    assert pack.human_direction == vague_direction(pack.task)
    for forbidden in ("避免", "霓虹", "玻璃拟态", "Corporate Memphis", "克制"):
        assert forbidden not in pack.human_direction
    assert "不代表用户本次的要求" in pack.human_direction
    step = next(s for s in pack.generation["trace"]["steps"] if s["step"] == "compose")
    assert step["vague"] is True and "霓虹" in step["model_direction"]  # kept for transparency, not exported


def test_specific_request_keeps_model_direction(agent, db, big_library):
    pack = agent.build_pack("找克制、有物质感、不要典型 AI 科技感的参考", limit=10)
    assert not is_vague(pack.task)
    assert pack.human_direction and "不代表用户本次的要求" not in pack.human_direction
