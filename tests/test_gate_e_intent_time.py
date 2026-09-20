"""Gate E — one search box: intent routing (keyword vs request) + time as a first-class facet."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import make_image_bytes
from magpie.agent import RetrievalAgent, order_hits, recall, time_since
from magpie.intent import classify_by_rules, classify_intent
from magpie.llm import FakeLLM
from magpie.models import CapturePayload, Human, Source, Task
from magpie.retrieval import Retriever
from magpie.task import time_facets_from_text, understand_task


# --------------------------------------------------------------------------- intent rules


@pytest.mark.parametrize(
    "text",
    ["纸张质感", "brutalist", "克制", "红色 海报", "wabi sabi", "纸张质感 / 克制 / brutalist", "#red #poster"],
)
def test_short_tags_are_keyword_lookups(text):
    it = classify_by_rules(text)
    assert it is not None and it.mode == "keyword", text


@pytest.mark.parametrize(
    "text",
    [
        "我想做一个红色系的页面，请帮我找到红色系相关的参考图",
        "帮我找一些适合首页的粗糙材质",
        "I'm looking for references for a restrained, tactile landing page",
        "有没有那种不完美、被时间用过的东西",
        "做一个克制的网站首页，不要 AI 蓝紫渐变，要有物质感",
    ],
)
def test_sentences_are_requests(text):
    it = classify_by_rules(text)
    assert it is not None and it.mode == "request", text


def test_ambiguous_middle_goes_to_model_then_length_rule():
    assert classify_by_rules("红色参考") is None  # 4 chars but says 参考: rules abstain
    with_model = classify_intent("红色参考", FakeLLM())
    assert with_model.source == "model" and with_model.mode == "keyword"
    no_model = classify_intent("红色参考", None)
    assert no_model.source == "fallback" and no_model.mode == "keyword"


def test_intent_reads_time_preferences():
    it = classify_intent("最新存的红色海报", FakeLLM())
    assert it.mode == "request" and it.sort == "newest"
    it2 = classify_intent("这周 纸张", FakeLLM())
    assert it2.within_days == 7
    assert classify_intent("最早的 brutalist", FakeLLM()).sort == "oldest"


# --------------------------------------------------------------------------- time facet


def test_time_words_to_facets():
    assert time_facets_from_text("这周存的红色参考") == {"within_days": 7}
    assert time_facets_from_text("最新的海报") == {"order": "newest"}
    assert time_facets_from_text("今年最早收的材质")["order"] == "oldest"
    assert time_facets_from_text("纸张质感") == {}


def test_understand_carries_time_facet(ingestor):
    task = understand_task(ingestor.llm, "这周存的红色参考图，最新的排前面")
    assert task.facets["time"] == {"within_days": 7, "order": "newest"}


@pytest.fixture()
def dated_library(ingestor, db):
    """Three red images saved 1, 10 and 40 days ago (created_at rewritten directly)."""
    ids = []
    for i, days in enumerate((1, 10, 40)):
        m = ingestor.ingest_image(
            make_image_bytes(color=(190, 30, 40), text=f"red{i}"),
            f"red{i}.png",
            CapturePayload(modality="image", human=Human(thought=f"红色 {days} 天前"), source=Source(page_title=f"red{i}")),
        )
        ts = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
        with db._lock:
            db.conn.execute("UPDATE materials SET created_at=? WHERE id=?", (ts, m.id))
            db.conn.commit()
        ids.append(m.id)
    return ids  # newest -> oldest


def test_recall_time_window_filters_by_entry_time(db, ingestor, dated_library):
    retriever = Retriever(db=db, llm=ingestor.llm)
    task = Task(raw_request="这周存的红色", search_queries=["红色"], facets={"time": {"within_days": 7}})
    hits = recall(retriever, task, k=10)
    assert [h.material_id for h in hits] == [dated_library[0]]
    task30 = Task(raw_request="红色", search_queries=["红色"], facets={"time": {"within_days": 30}})
    assert {h.material_id for h in recall(retriever, task30, k=10)} == set(dated_library[:2])
    assert time_since({}) is None


def test_order_hits_by_entry_time(db, dated_library):
    mats = [db.get_material(i) for i in reversed(dated_library)]
    assert [m.id for m in order_hits(mats, "newest")] == dated_library
    assert [m.id for m in order_hits(mats, "oldest")] == list(reversed(dated_library))
    assert order_hits(mats, None) == mats


def test_order_hits_keeps_relevance_within_a_day():
    class R:  # ranked items saved the same day, minutes apart
        def __init__(self, ts, name):
            self.created_at, self.name = ts, name

    ranked = [R("2026-09-18T10:05:00+00:00", "best"), R("2026-09-18T10:09:00+00:00", "second"), R("2026-09-17T09:00:00+00:00", "older")]
    assert [r.name for r in order_hits(ranked, "newest")] == ["best", "second", "older"]
    assert [r.name for r in order_hits(ranked, "oldest")] == ["older", "best", "second"]


def test_list_materials_sort(db, dated_library):
    assert [m.id for m in db.list_materials(order="oldest")] == list(reversed(dated_library))
    assert [m.id for m in db.list_materials()] == dated_library


# --------------------------------------------------------------------------- HTTP: one box


def test_search_auto_routes_and_sorts(env, dated_library):
    from magpie.api import app

    with TestClient(app) as c:
        kw = c.get("/search", params={"q": "红色"}).json()
        assert kw["route"] == "keyword" and kw["mode"] == "fast" and kw["intent"]["source"] == "rules"
        assert kw["hits"] and "relevance" not in kw["hits"][0]

        req = c.get("/search", params={"q": "我想做一个红色系的页面，请帮我找到红色系相关的参考图"}).json()
        assert req["route"] == "request" and req["mode"] == "agent" and req["intent"]["mode"] == "request"
        assert req["hits"] and req["hits"][0]["relevance"] >= 1

        # user override: a sentence forced onto the keyword route
        forced = c.get("/search", params={"q": "我想做一个红色系的页面，请帮我找到红色系相关的参考图", "mode": "fast"}).json()
        assert forced["mode"] == "fast" and forced["intent"]["mode"] == "request"

        # time: explicit sort + window on the keyword route
        newest = c.get("/search", params={"q": "红色", "sort": "newest"}).json()
        assert [h["material_id"] for h in newest["hits"]] == dated_library
        oldest = c.get("/search", params={"q": "红色", "sort": "oldest"}).json()
        assert [h["material_id"] for h in oldest["hits"]] == list(reversed(dated_library))
        week = c.get("/search", params={"q": "红色", "within_days": 7}).json()
        assert [h["material_id"] for h in week["hits"]] == [dated_library[0]]

        # time words in the text are read by the intent layer, no parameters needed
        latest = c.get("/search", params={"q": "最新的红色"}).json()
        assert latest["sort"] == "newest" and [h["material_id"] for h in latest["hits"]][0] == dated_library[0]

        # agent route honours the brief's time order
        agent_newest = c.get("/search", params={"q": "我想找最新存的红色系参考图，最新的排前面"}).json()
        assert agent_newest["mode"] == "agent" and agent_newest["sort"] == "newest"
        ids = [h["material_id"] for h in agent_newest["hits"]]
        assert ids == sorted(ids, key=lambda i: dated_library.index(i))

        listing = c.get("/materials", params={"sort": "oldest"}).json()
        assert listing["sort"] == "oldest" and listing["items"][0]["id"] == dated_library[-1]
