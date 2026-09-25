"""End-to-end API tests for the three-way merge save guards.

Persisting a merge (``saveAsProject`` or writing back to the base project)
must be all-or-nothing: whenever the adjudication leaves conflicts
unresolved or contains invalid custom values, ``POST /api/merge`` answers
**409** with the problem details, creates no project and never modifies the
base document. The plain preview still returns the conflicts and errors.

Run with: python3 test_merge_api.py  (or via pytest).
"""
import os
import tempfile

import pytest

os.environ.setdefault("STATIC_DIR", "/nonexistent")
_TMP = tempfile.mkdtemp(prefix="merge-api-")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # entering the context triggers the startup event, which opens the SQLite DB
    with TestClient(app) as c:
        yield c

SEG = lambda sid, s, e, sp="", t="x": {
    "id": sid, "start": s, "end": e, "speaker": sp, "text": t}


def doc(segs=()):
    return {"segments": list(segs), "annotations": []}


def make_conflict_docs():
    base = doc([SEG("s1", 0, 1, "A", "hello")])
    a = doc([SEG("s1", 0, 1, "Alice", "hello")])
    b = doc([SEG("s1", 0, 1, "Bob", "hello")])
    return base, a, b


# ---------- preview ----------

def test_preview_lists_unresolved_and_does_not_persist(client):
    base, a, b = make_conflict_docs()
    r = client.post("/api/merge", json={"base": base, "a": a, "b": b})
    assert r.status_code == 200
    data = r.json()
    assert len(data["conflicts"]) == 1
    assert data["unresolved"] == [data["conflicts"][0]["id"]]
    assert data["errors"] == []
    assert "savedProject" not in data
    # conflicting item is omitted from the preview document
    assert data["document"]["segments"] == []


def test_preview_lists_validation_errors_for_invalid_custom(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom",
                              "value": {"start": "", "end": ""}}},
    })
    assert r.status_code == 200
    data = r.json()
    assert data["unresolved"] == []
    assert data["errors"] and data["errors"][0]["conflictId"] == cid
    # the invalid entry stays out of the preview document
    assert data["document"]["segments"] == []


# ---------- save as new project ----------

def test_save_as_with_unresolved_conflict_returns_409_and_creates_nothing(client):
    base, a, b = make_conflict_docs()
    before = client.get("/api/projects").json()
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b, "saveAsProject": "残缺项目"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["unresolved"]
    assert isinstance(detail["errors"], list)
    # no project was created
    after = client.get("/api/projects").json()
    assert [p["id"] for p in after] == [p["id"] for p in before]
    assert all(p["name"] != "残缺项目" for p in after)


def test_save_as_with_invalid_custom_returns_409_and_creates_nothing(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom",
                              "value": {"start": 5, "end": 1}}},
        "saveAsProject": "残缺项目",
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["unresolved"] == []
    assert [e["conflictId"] for e in detail["errors"]] == [cid]
    assert all(p["name"] != "残缺项目"
               for p in client.get("/api/projects").json())


def test_save_as_with_valid_resolution_creates_project(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "b"}},
        "saveAsProject": "完整合并项目",
    })
    assert r.status_code == 200
    saved = r.json()["savedProject"]
    assert saved["name"] == "完整合并项目"
    project = client.get(f"/api/projects/{saved['id']}").json()
    assert project["document"]["segments"][0]["speaker"] == "Bob"


def test_fixing_invalid_resolution_then_save_succeeds(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    bad = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom", "value": {"start": 9,
                                                            "end": 1}}},
        "saveAsProject": "修正后项目",
    })
    assert bad.status_code == 409
    good = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom", "value": {
            "id": "s1", "start": 1, "end": 9,
            "speaker": "X", "text": "ok"}}},
        "saveAsProject": "修正后项目",
    })
    assert good.status_code == 200
    saved = good.json()["savedProject"]
    project = client.get(f"/api/projects/{saved['id']}").json()
    assert project["document"]["segments"][0]["end"] == 9


# ---------- write back to base project ----------

def test_write_back_with_unresolved_conflict_returns_409_and_keeps_base(client):
    created = db.create_project("基准项目", doc([SEG("s1", 0, 1, "A", "hello")]))
    pid = created["id"]
    _, a, b = make_conflict_docs()
    version_before = db.get_project(pid)["updatedAt"]

    r = client.post("/api/merge", json={"projectId": pid, "a": a, "b": b})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["unresolved"]
    # base document untouched
    fresh = db.get_project(pid)
    assert fresh["updatedAt"] == version_before
    assert fresh["document"]["segments"][0]["speaker"] == "A"


def test_direct_api_write_back_with_invalid_custom_is_refused(client):
    created = db.create_project("基准项目2", doc([SEG("s1", 0, 1, "A", "hello")]))
    pid = created["id"]
    _, a, b = make_conflict_docs()
    cid = client.post("/api/merge",
                      json={"projectId": pid, "a": a, "b": b})
    # the 409 response already names the unresolved conflict id
    cid = cid.json()["detail"]["unresolved"][0]
    before = db.get_project(pid)

    r = client.post("/api/merge", json={
        "projectId": pid, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom",
                              "value": {"start": "", "end": ""}}},
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["errors"] and detail["errors"][0]["conflictId"] == cid
    after = db.get_project(pid)
    assert after["updatedAt"] == before["updatedAt"]
    assert after["document"] == before["document"]


def test_write_back_with_valid_resolution_updates_base(client):
    created = db.create_project("基准项目3", doc([SEG("s1", 0, 1, "A", "hello")]))
    pid = created["id"]
    _, a, b = make_conflict_docs()
    cid = client.post("/api/merge",
                      json={"projectId": pid, "a": a, "b": b}) \
        .json()["detail"]["unresolved"][0]
    r = client.post("/api/merge", json={
        "projectId": pid, "a": a, "b": b,
        "resolutions": {cid: {"choice": "a"}},
    })
    assert r.status_code == 200
    assert r.json()["savedProject"]["id"] == pid
    assert db.get_project(pid)["document"]["segments"][0]["speaker"] == "Alice"
