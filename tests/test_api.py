"""HTTP contract smoke tests (capture layer -> POST /materials, dev UI endpoints)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(env):
    from magpie.api import app

    with TestClient(app) as c:
        yield c


def test_capture_contract_json_text(client):
    body = {
        "modality": "text",
        "content": "Have nothing in your houses that you do not know to be useful, or believe to be beautiful.",
        "source": {"page_url": "https://en.wikipedia.org/wiki/William_Morris", "page_title": "William Morris"},
        "human": {"thought": "这句可以当产品原则"},
        "sync": True,
    }
    r = client.post("/materials", json=body)
    assert r.status_code == 201, r.text
    m = r.json()["material"]
    assert m["modality"] == "text" and m["human"]["thought"] == "这句可以当产品原则"
    assert m["processing"]["status"] == "ready"
    assert client.get(f"/materials/{m['id']}").json()["analysis"]["summary"]


def test_capture_contract_multipart_image(client, sample_image):
    r = client.post(
        "/materials",
        data={"modality": "image", "thought": "喜欢这个纸张质感", "page_url": "https://example.com/p", "sync": "true"},
        files={"file": ("poster.png", sample_image, "image/png")},
    )
    assert r.status_code == 201, r.text
    m = r.json()["material"]
    assert m["processing"]["status"] == "ready"
    assert client.get(f"/materials/{m['id']}/file").status_code == 200
    assert client.get(f"/materials/{m['id']}/thumb").headers["content-type"] == "image/jpeg"
    lst = client.get("/materials").json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == m["id"]

    # human thought edit only touches the human column
    r = client.patch(f"/materials/{m['id']}/thought", json={"thought": "改成：喜欢它的克制"})
    assert r.json()["human"]["thought"] == "改成：喜欢它的克制"
    assert r.json()["analysis"]["summary"] == m["analysis"]["summary"]


def test_background_analysis_path(client, sample_image):
    r = client.post("/materials", data={"modality": "image", "thought": "x"}, files={"file": ("a.png", sample_image, "image/png")})
    assert r.status_code == 201
    # TestClient runs background tasks before returning, so it is already analysed
    m = client.get(f"/materials/{r.json()['material']['id']}").json()
    assert m["processing"]["status"] == "ready"


def test_index_and_health(client):
    assert "MAGPIE MVP" in client.get("/").text
    h = client.get("/health").json()
    assert h["provider"] == "fake" and h["materials"] == 0
