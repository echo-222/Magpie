"""Gate C — differentiated loop: Task -> Pack with reasons -> human edits persist -> Copy for Agent."""

import pytest
from fastapi.testclient import TestClient

from conftest import make_image_bytes
from magpie import pack as pack_ops
from magpie.export import pack_to_json, pack_to_markdown
from magpie.models import CapturePayload, Human, Source
from magpie.recompose import Recomposer
from magpie.retrieval import Retriever


@pytest.fixture()
def library(ingestor):
    ids = []
    for i, (color, thought) in enumerate(
        [
            ((210, 200, 185), "喜欢这个粗糙的纸张质感"),
            ((120, 120, 118), "剁斧混凝土表面，粗糙但克制"),
            ((90, 40, 200), "反例：蓝紫渐变科技感"),
            ((200, 190, 170), "汝窑天青色，代替科技蓝"),
        ]
    ):
        m = ingestor.ingest_image(make_image_bytes(color=color, text=f"img{i}"), f"img{i}.png", CapturePayload(modality="image", human=Human(thought=thought), source=Source(page_url=f"https://example.com/{i}", page_title=f"Ref {i}")))
        ids.append(m.id)
    m = ingestor.ingest_text(CapturePayload(modality="text", content="Less, but better.", human=Human(thought="首页态度"), source=Source(page_url="https://www.vitsoe.com/us/about/good-design")))
    ids.append(m.id)
    return ids


@pytest.fixture()
def recomposer(db, ingestor):
    return Recomposer(db=db, retriever=Retriever(db=db, llm=ingestor.llm), llm=ingestor.llm)


TASK = "我要做一个克制、有物质感、不要典型 AI 蓝紫色的网站首页。"


def test_task_to_pack_with_reasons_persists(recomposer, db, library):
    pack = recomposer.build(TASK, limit=8)
    assert pack.id.startswith("pack_")
    assert pack.task.raw_request == TASK and pack.task.search_queries
    assert pack.groups and all(g.members for g in pack.groups)
    for g in pack.groups:
        for mem in g.members:
            assert mem.material_id in library
            assert mem.reason  # task-specific reason per selected item
    assert len(set(pack.member_ids())) == len(pack.member_ids())  # no duplicates
    assert pack.candidates and pack.generation["fallback"] is False
    assert db.get_pack(pack.id).model_dump() == pack.model_dump()


def test_human_edits_persist_and_are_not_undone(recomposer, db, library):
    pack = recomposer.build(TASK, limit=8)
    victim = pack.groups[0].members[0].material_id
    n_before = len(pack.member_ids())

    pack = pack_ops.remove_member(db, pack, victim)
    assert victim not in pack.member_ids() and victim in pack.removed_material_ids
    assert len(pack.member_ids()) == n_before - 1

    mover = pack.member_ids()[0]
    pack = pack_ops.move_member(db, pack, mover, "Typography")
    assert pack.find_member(mover)[0].name == "Typography"

    pack = pack_ops.set_note(db, pack, mover, "用作字体气质参考")
    assert pack.find_member(mover)[1].note == "用作字体气质参考"

    reloaded = db.get_pack(pack.id)
    assert [e.op for e in reloaded.human_edits] == ["remove", "move", "note"]
    assert victim in reloaded.removed_material_ids

    # alternatives never re-suggest the removed material or current members
    alts = recomposer.alternatives(reloaded, material_id=mover, group="Typography", limit=5)
    suggested = {a["material_id"] for a in alts}
    assert victim not in suggested and not (suggested & set(reloaded.member_ids()))
    assert all(a["reason"] for a in alts)

    if alts:
        pack = pack_ops.add_member(db, reloaded, alts[0]["material_id"], "Typography", reason=alts[0]["reason"])
        assert pack.find_member(alts[0]["material_id"])[1].added_by == "human"


def test_copy_for_agent_markdown_and_json(recomposer, db, library):
    pack = recomposer.build(TASK, limit=8)
    first = pack.groups[0].members[0]
    pack = pack_ops.set_note(db, pack, first.material_id, "首屏主材质")
    md = pack_to_markdown(pack, db)
    assert md.startswith("# Task\n" + TASK)
    assert "## Human Direction" in md
    for g in pack.groups:
        assert f"## {g.name}" in md
    m = db.get_material(first.material_id)
    assert m.human.thought in md  # Human Thought is carried into agent context
    assert first.reason in md  # why selected
    assert "首屏主材质" in md  # human note
    assert (m.source.page_url or m.original.filename) in md  # source reference
    assert "Less, but better." in md or "Ref" in md

    js = pack_to_json(pack, db)["magpie_pack"]
    assert js["id"] == pack.id and js["groups"] and js["groups"][0]["members"][0]["material"]["human_thought"]


def test_removed_items_listed_for_agent(recomposer, db, library):
    pack = recomposer.build(TASK, limit=8)
    victim = pack.groups[0].members[0].material_id
    pack = pack_ops.remove_member(db, pack, victim)
    md = pack_to_markdown(pack, db)
    assert "Deliberately left out by the human" in md and victim in md


def test_pack_http_flow(env, library):
    from magpie.api import app

    with TestClient(app) as c:
        r = c.post("/packs", json={"task": TASK, "candidates": 8})
        assert r.status_code == 201, r.text
        pack = r.json()["pack"]
        pid = pack["id"]
        assert r.json()["materials"]
        mid = pack["groups"][0]["members"][0]["material_id"]

        r = c.post(f"/packs/{pid}/move", json={"material_id": mid, "to_group": "Mood"})
        assert r.status_code == 200 and any(g["name"] == "Mood" for g in r.json()["pack"]["groups"])

        r = c.post(f"/packs/{pid}/alternatives", json={"material_id": mid, "group": "Mood"})
        assert r.status_code == 200 and "suggestions" in r.json()

        r = c.post(f"/packs/{pid}/remove", json={"material_id": mid})
        assert mid in r.json()["pack"]["removed_material_ids"]

        md = c.get(f"/packs/{pid}/export").text
        assert md.startswith("# Task")
        js = c.get(f"/packs/{pid}/export?format=json").json()
        assert js["magpie_pack"]["id"] == pid
        assert c.get("/packs").json()["items"][0]["id"] == pid
        assert c.get(f"/packs/{pid}").json()["pack"]["human_edits"]
