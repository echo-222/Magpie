"""Gate B — small Material Memory searchable via Human Thought + machine understanding."""

from magpie.models import CapturePayload, Human, Source
from magpie.retrieval import Retriever, build_embedding_texts
from conftest import make_image_bytes


def seed(ingestor):
    items = [
        ("image", "paper.png", (210, 200, 185), "喜欢这个粗糙的纸张质感，像手工纸"),
        ("image", "neon.png", (90, 40, 200), "反例：蓝紫渐变霓虹科技感，避开"),
        ("image", "concrete.png", (120, 120, 118), "剁斧混凝土表面，粗糙但克制"),
        ("text", None, None, "少但更好，这句可以当首页的态度"),
        ("text", None, None, "网格排版和左对齐，瑞士风格骨架"),
    ]
    ids = {}
    for i, (mod, fname, color, thought) in enumerate(items):
        if mod == "image":
            m = ingestor.ingest_image(make_image_bytes(color=color, text=fname), fname, CapturePayload(modality="image", human=Human(thought=thought)))
        else:
            content = ["Good design is as little design as possible. Less, but better.", "The Swiss Style uses a grid, sans-serif type and flush-left text."][i - 3]
            m = ingestor.ingest_text(CapturePayload(modality="text", content=content, human=Human(thought=thought), source=Source(page_title=f"t{i}")))
        assert m.processing.status == "ready", m.processing.error
        ids[fname or f"text{i}"] = m.id
    return ids


def test_search_hits_human_thought_first(ingestor, db):
    ids = seed(ingestor)
    ret = Retriever(db=db, llm=ingestor.llm)
    hits = ret.search("粗糙的纸张质感", limit=5)
    assert hits and hits[0].material_id == ids["paper.png"]
    assert hits[0].via == "human_thought"
    assert hits[0].signals.get("human") is not None


def test_search_hits_machine_side_for_text_material(ingestor, db):
    ids = seed(ingestor)
    ret = Retriever(db=db, llm=ingestor.llm)
    hits = ret.search("Swiss Style grid sans-serif", limit=3)
    assert hits[0].material_id == ids["text4"]


def test_fts_exact_phrase_bonus_and_via(db, ingestor):
    ids = seed(ingestor)
    fts = db.fts_search("混凝土")
    assert any(mid == ids["concrete.png"] and via == "human_thought" for mid, _, via in fts)
    fts = db.fts_search("flush-left")
    assert any(mid == ids["text4"] and via == "machine" for mid, _, via in fts)


def test_embedding_text_contains_both_voices(ingestor):
    m = ingestor.ingest_text(CapturePayload(modality="text", content="Less, but better.", human=Human(thought="首页的态度")))
    texts = build_embedding_texts(m)
    assert "Human thought: 首页的态度" in texts["combined"]
    assert "Text: Less, but better." in texts["combined"]
    assert texts["human"] == "首页的态度"


def test_similar_to_excludes_self(ingestor, db):
    ids = seed(ingestor)
    ret = Retriever(db=db, llm=ingestor.llm)
    hits = ret.similar_to(ids["paper.png"], limit=3)
    assert hits and all(h.material_id != ids["paper.png"] for h in hits)


def test_search_with_query_expansion_takes_best(ingestor, db):
    ids = seed(ingestor)
    ret = Retriever(db=db, llm=ingestor.llm)
    hits = ret.search(["首页的态度", "瑞士风格骨架"], limit=2)
    assert {h.material_id for h in hits} == {ids["text3"], ids["text4"]}
