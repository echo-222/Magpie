"""Gate D — retrieval agent: abstract request -> facets -> fact-aware recall -> judged ranking -> pack with gaps."""

import pytest
from fastapi.testclient import TestClient

from conftest import make_image_bytes
from magpie import agent as agent_mod
from magpie.agent import RetrievalAgent, color_fact_score, recall
from magpie.models import CapturePayload, Human, Source, Task
from magpie.retrieval import Retriever
from magpie.task import _clean_facets, _facets_from_text


@pytest.fixture()
def library(ingestor):
    """Two red images, one blue image, one grey image, one text clipping."""
    ids = {}
    for key, color, thought in [
        ("red_poster", (190, 30, 40), "红三角，一个白圆，力量全靠几何"),
        ("red_seal", (170, 40, 50), "朱印压在宋版书上"),
        ("blue", (40, 60, 200), "反例：AI 蓝紫"),
        ("grey", (120, 120, 120), "剁斧混凝土，粗糙"),
    ]:
        m = ingestor.ingest_image(
            make_image_bytes(color=color, text=key),
            f"{key}.png",
            CapturePayload(modality="image", human=Human(thought=thought), source=Source(page_title=key)),
        )
        ids[key] = m.id
    t = ingestor.ingest_text(CapturePayload(modality="text", content="红色是最有力量的颜色。", human=Human(thought="红色文案")))
    ids["text"] = t.id
    return ids


@pytest.fixture()
def agent(db, ingestor):
    return RetrievalAgent(db=db, retriever=Retriever(db=db, llm=ingestor.llm), llm=ingestor.llm)


RED_REQUEST = "我想做一个红色系的页面，请帮我找到红色系相关的参考图"


# --------------------------------------------------------------------------- facets


def test_facets_fallback_from_text():
    f = _facets_from_text(RED_REQUEST)
    assert f["colors"]["primary"] == ["red"] and f["colors"]["weight"] == "must"
    assert "pink" in f["colors"]["adjacent"]
    assert f["modality"] == "image"
    # 留白 / 黑体 are not colour requests
    assert "colors" not in _facets_from_text("首页要留白，用黑体")


def test_facets_from_model_are_validated():
    f = _clean_facets({"colors": {"primary": ["red", "neon"], "saturation": "vivid", "weight": "must"}, "modality": "video"})
    assert f["colors"]["primary"] == ["red"]
    assert f["colors"]["saturation"] == "any"
    assert f["modality"] == "any"
    assert _clean_facets({"colors": {"primary": []}}) == {"modality": "any"}


# --------------------------------------------------------------------------- recall with facts


def test_color_fact_score_uses_palette_facts(db, library):
    colors = {"primary": ["red"], "adjacent": ["pink", "orange"], "saturation": "any", "lightness": "any", "weight": "must"}
    red = color_fact_score(db.get_material(library["red_poster"]), colors)
    blue = color_fact_score(db.get_material(library["blue"]), colors)
    text = color_fact_score(db.get_material(library["text"]), colors)
    assert red is not None and red > 0.8
    assert blue == 0.0
    assert text is None  # no palette -> no fact, neither reward nor penalty


def test_recall_applies_modality_filter_and_color_boost(db, ingestor, library):
    retriever = Retriever(db=db, llm=ingestor.llm)
    task = Task(raw_request=RED_REQUEST, search_queries=["红色"], facets=_facets_from_text(RED_REQUEST))
    hits = recall(retriever, task, k=10)
    ids = [h.material_id for h in hits]
    assert library["text"] not in ids  # 参考图 -> images only
    assert set(ids[:2]) == {library["red_poster"], library["red_seal"]}  # colour facts outrank the fake embeddings
    assert all("color" in h.signals for h in hits)


def test_orphan_embeddings_do_not_eat_result_slots(db, ingestor, library):
    retriever = Retriever(db=db, llm=ingestor.llm)
    # simulate a delete racing a background analysis: material row gone, vectors left behind
    victim = library["grey"]
    with db._lock:
        db.conn.execute("DELETE FROM materials WHERE id=?", (victim,))
        db.conn.commit()
    db._vec_cache.clear()
    hits = retriever.search("红色", limit=2)
    assert len(hits) == 2 and all(h.material is not None for h in hits)
    assert db.prune_orphan_embeddings() > 0
    assert db.prune_orphan_embeddings() == 0


# --------------------------------------------------------------------------- judge + compose


def test_find_returns_judged_ranking_with_verdicts(agent, library):
    res = agent.find(RED_REQUEST)
    assert res.task.facets["colors"]["primary"] == ["red"]
    assert [s["step"] for s in res.trace["steps"]] == ["understand", "recall", "judge"]
    kept = [j for j in res.judged if j.relevance >= 1]
    assert kept and kept[0].hit.material_id in (library["red_poster"], library["red_seal"])
    assert all(j.why for j in kept)
    # sorted by relevance first, then fused score
    rels = [j.relevance for j in res.judged]
    assert rels == sorted(rels, reverse=True)


def test_build_pack_has_relevance_gaps_and_trace(agent, db, library):
    pack = agent.build_pack(RED_REQUEST)
    assert pack.groups and all(g.members for g in pack.groups)
    for g in pack.groups:
        assert [m.relevance for m in g.members] == sorted((m.relevance for m in g.members), reverse=True)
        for m in g.members:
            assert m.reason and m.relevance and m.relevance >= 1
    assert pack.gaps == ["fake gap"]
    assert pack.generation["pipeline"] == "agent-v2"
    assert [s["step"] for s in pack.generation["trace"]["steps"]] == ["understand", "recall", "judge", "compose"]
    assert all(c.relevance is not None for c in pack.candidates)
    assert db.get_pack(pack.id).gaps == ["fake gap"]


def test_judge_failure_falls_back_to_recall_order(agent, library, monkeypatch):
    real = agent.llm.chat_json

    def flaky(system, user, **kw):
        if kw.get("purpose") == "judge":
            raise RuntimeError("model down")
        return real(system, user, **kw)

    monkeypatch.setattr(agent.llm, "chat_json", flaky)
    res = agent.find(RED_REQUEST)
    assert res.trace["steps"][2]["fallback"] is True
    assert res.judged and all(j.relevance == 1 for j in res.judged)  # nothing silently dropped


def test_compose_failure_groups_by_judged_aspect(agent, library, monkeypatch):
    real = agent.llm.chat_json

    def flaky(system, user, **kw):
        if kw.get("purpose") == "recompose":
            raise RuntimeError("model down")
        return real(system, user, **kw)

    monkeypatch.setattr(agent.llm, "chat_json", flaky)
    pack = agent.build_pack(RED_REQUEST)
    assert pack.generation["fallback"] is True
    assert pack.groups and pack.groups[0].name == "fake aspect"


def test_judge_batches(agent, library, monkeypatch):
    monkeypatch.setattr(agent_mod, "JUDGE_BATCH", 2)
    res = agent.find(RED_REQUEST)
    assert res.trace["steps"][2]["batches"] >= 2
    assert res.trace["steps"][2]["judged"] == res.trace["steps"][1]["candidates"]


# --------------------------------------------------------------------------- HTTP


def test_search_agent_mode_http(env, library):
    from magpie.api import app

    with TestClient(app) as c:
        r = c.get("/search", params={"q": RED_REQUEST, "mode": "agent", "limit": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "agent" and body["task"]["facets"]["modality"] == "image"
        assert body["hits"] and {"relevance", "why", "aspect"} <= set(body["hits"][0])
        assert body["hits"][0]["relevance"] >= body["hits"][-1]["relevance"]
        fast = c.get("/search", params={"q": "红色"}).json()
        assert fast["mode"] == "fast" and "relevance" not in fast["hits"][0]

        p = c.post("/packs", json={"task": RED_REQUEST})
        assert p.status_code == 201
        pack = p.json()["pack"]
        assert pack["gaps"] == ["fake gap"] and pack["generation"]["pipeline"] == "agent-v2"
        md = c.get(f"/packs/{pack['id']}/export").text
        assert "gaps" in md

        # history list carries what the UI needs (task, covers), and packs can be renamed
        items = c.get("/packs").json()["items"]
        me = next(i for i in items if i["id"] == pack["id"])
        assert me["task"] == RED_REQUEST and me["cover_ids"] and me["created_at"]
        renamed = c.post(f"/packs/{pack['id']}/rename", json={"name": "红色系参考"}).json()["pack"]
        assert renamed["name"] == "红色系参考"
        assert c.get("/packs").json()["items"][0]["name"] == "红色系参考"
        assert c.post(f"/packs/{pack['id']}/rename", json={"name": "  "}).status_code == 400
        assert c.get("/static/logo.png").status_code == 200
        assert c.get("/static/../index.html").status_code in (404, 200)  # no traversal: resolves to a flat name
