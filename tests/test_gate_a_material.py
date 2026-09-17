"""Gate A — one-material vertical slice: ingest -> persist -> analyse -> read back -> export."""

from magpie.models import CapturePayload, Human, Source


def test_image_material_roundtrip(ingestor, db, env, sample_image):
    payload = CapturePayload(
        modality="image",
        source=Source(page_url="https://example.com/poster", page_title="Poster"),
        human=Human(thought="喜欢这个粗糙的纸张质感"),
    )
    m = ingestor.ingest_image(sample_image, "poster01.png", payload)

    assert m.id.startswith("mat_")
    assert m.processing.status == "ready", m.processing.error
    # original file persisted separately from the row
    assert (env.files_dir / m.original.file_path).exists()
    assert (env.thumbs_dir / f"{m.id}.jpg").exists()
    # human thought is first-class and untouched by analysis
    assert m.human.thought == "喜欢这个粗糙的纸张质感"
    assert "human.thought" in m.provenance.human_fields
    # file-derived metadata + colours
    assert m.objective_metadata["width"] == 320 and m.objective_metadata["height"] == 200
    assert m.analysis.colors and abs(sum(c.ratio for c in m.analysis.colors) - 1.0) < 0.05
    assert m.objective_metadata["palette"]["descriptors"]
    # machine analysis present and attributed
    assert m.analysis.summary and m.analysis.keywords
    assert "analysis.summary" in m.provenance.machine_fields
    # embeddings: combined + human
    assert db.get_embedding(m.id, "combined") is not None
    assert db.get_embedding(m.id, "human") is not None

    again = db.get_material(m.id)
    assert again.model_dump() == m.model_dump()


def test_text_material_roundtrip(ingestor, db):
    payload = CapturePayload(
        modality="text",
        content="Less, but better. 好的设计是尽可能少的设计。",
        source=Source(page_url="https://www.vitsoe.com/us/about/good-design", page_title="Good design"),
        human=Human(thought="这句可以直接当首页的态度"),
    )
    m = ingestor.ingest_text(payload)
    assert m.processing.status == "ready", m.processing.error
    assert m.original.content.startswith("Less, but better")
    assert m.objective_metadata["char_count"] > 0
    assert m.analysis.summary and m.analysis.keywords
    assert db.get_embedding(m.id, "combined") is not None


def test_duplicate_import_is_idempotent(ingestor, db, sample_image):
    p = CapturePayload(modality="image", human=Human(thought="x"))
    a = ingestor.ingest_image(sample_image, "a.png", p)
    b = ingestor.ingest_image(sample_image, "a.png", p)
    assert a.id == b.id
    assert db.count_materials() == 1


def test_reanalysis_never_overwrites_human_thought(ingestor, db, sample_image):
    p = CapturePayload(modality="image", human=Human(thought="我的想法"))
    m = ingestor.ingest_image(sample_image, "a.png", p)
    ingestor.analyze(m.id)
    assert db.get_material(m.id).human.thought == "我的想法"


def test_failed_analysis_is_recorded_not_lost(ingestor, db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(ingestor.llm, "chat_json", boom)
    m = ingestor.ingest_text(CapturePayload(modality="text", content="hello world material"))
    assert m.processing.status == "failed"
    assert "model down" in (m.processing.error or "")
    assert db.get_material(m.id) is not None
