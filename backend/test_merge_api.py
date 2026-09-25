"""API tests: a merge carrying unresolved conflicts or invalid adjudications
must never be persisted.

Preview (no ``saveAsProject`` / ``projectId``) keeps listing conflicts and
adjudication errors with a 200; a save-as or write-back request with a
non-empty ``unresolved`` or ``errors`` is rejected with 409 carrying the
problem details — no project is created and the base document is not
rewritten.  Fixing the adjudication and re-saving then succeeds.

Run with: python3 test_merge_api.py  (or via pytest).
"""
import os
import shutil
import tempfile

os.environ.setdefault("STATIC_DIR", "/nonexistent")
_TMP = tempfile.mkdtemp(prefix="merge-api-")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

BASE_DOC = {
    "segments": [
        {"id": "s1", "start": 0, "end": 5, "speaker": "甲",
         "text": "基准句子"},
    ],
    "annotations": [],
}
A_DOC = {
    "segments": [
        {"id": "s1", "start": 0, "end": 5, "speaker": "A版",
         "text": "基准句子"},
    ],
    "annotations": [],
}
B_DOC = {
    "segments": [
        {"id": "s1", "start": 0, "end": 5, "speaker": "B版",
         "text": "基准句子"},
    ],
    "annotations": [],
}
CONFLICT_ID = "segments:s1:modify"

# custom adjudication with empty time fields -> fails server validation
INVALID_CUSTOM = {
    "choice": "custom",
    "value": {"start": "", "end": "", "speaker": "丙", "text": "合并句"},
}
VALID_CUSTOM = {
    "choice": "custom",
    "value": {"start": 0, "end": 6, "speaker": "丙", "text": "合并句"},
}


def _merge_payload(**extra):
    payload = {"base": BASE_DOC, "a": A_DOC, "b": B_DOC}
    payload.update(extra)
    return payload


def _new_project(c, name="基准项目", document=None):
    p = c.post("/api/projects", json={"name": name}).json()
    pid = p["id"]
    if document is not None:
        r = c.put(f"/api/projects/{pid}",
                  json={"document": document,
                        "documentVersion": p["documentVersion"]})
        assert r.status_code == 200, r.text
    return pid


def test_preview_lists_conflicts_and_adjudication_errors():
    """Preview (no persist intent) keeps returning 200 with the problems."""
    with TestClient(app) as c:
        before = len(c.get("/api/projects").json())

        # unresolved conflict -> listed, item omitted from the document
        r = c.post("/api/merge", json=_merge_payload())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["unresolved"] == [CONFLICT_ID]
        assert body["errors"] == []
        assert len(body["conflicts"]) == 1
        assert body["document"]["segments"] == []
        assert "savedProject" not in body

        # invalid custom adjudication -> listed as an error, item omitted
        r = c.post("/api/merge", json=_merge_payload(
            resolutions={CONFLICT_ID: INVALID_CUSTOM}))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["unresolved"] == []
        assert len(body["errors"]) == 1
        assert body["errors"][0]["conflictId"] == CONFLICT_ID
        assert body["document"]["segments"] == []
        assert "savedProject" not in body

        # preview never persists anything
        assert len(c.get("/api/projects").json()) == before


def test_save_as_with_unresolved_conflict_returns_409_and_creates_nothing():
    with TestClient(app) as c:
        before = c.get("/api/projects").json()
        r = c.post("/api/merge", json=_merge_payload(saveAsProject="合并结果"))
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["unresolved"] == [CONFLICT_ID]
        assert detail["errors"] == []
        assert c.get("/api/projects").json() == before  # nothing created


def test_save_as_with_invalid_custom_resolution_returns_409():
    with TestClient(app) as c:
        before = c.get("/api/projects").json()
        r = c.post("/api/merge", json=_merge_payload(
            resolutions={CONFLICT_ID: INVALID_CUSTOM},
            saveAsProject="合并结果"))
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["unresolved"] == []
        assert len(detail["errors"]) == 1
        assert detail["errors"][0]["conflictId"] == CONFLICT_ID
        assert c.get("/api/projects").json() == before  # nothing created


def test_save_as_fully_resolved_creates_project():
    with TestClient(app) as c:
        r = c.post("/api/merge", json=_merge_payload(
            resolutions={CONFLICT_ID: {"choice": "b"}},
            saveAsProject="合并结果"))
        assert r.status_code == 200, r.text
        saved = r.json()["savedProject"]
        assert saved["name"] == "合并结果"
        created = c.get(f"/api/projects/{saved['id']}").json()
        assert created["document"]["segments"][0]["speaker"] == "B版"


def test_write_back_with_problems_returns_409_and_keeps_document():
    with TestClient(app) as c:
        pid = _new_project(c, document=BASE_DOC)
        v0 = c.get(f"/api/projects/{pid}").json()["documentVersion"]

        # unresolved conflict -> 409, base document untouched
        r = c.post("/api/merge", json={"projectId": pid,
                                       "a": A_DOC, "b": B_DOC})
        assert r.status_code == 409
        assert r.json()["detail"]["unresolved"] == [CONFLICT_ID]

        # invalid custom adjudication -> 409, base document untouched
        r = c.post("/api/merge", json={
            "projectId": pid, "a": A_DOC, "b": B_DOC,
            "resolutions": {CONFLICT_ID: INVALID_CUSTOM}})
        assert r.status_code == 409
        assert len(r.json()["detail"]["errors"]) == 1

        saved = c.get(f"/api/projects/{pid}").json()
        assert saved["document"] == BASE_DOC
        assert saved["documentVersion"] == v0


def test_write_back_fully_resolved_updates_base_project():
    with TestClient(app) as c:
        pid = _new_project(c, document=BASE_DOC)
        r = c.post("/api/merge", json={
            "projectId": pid, "a": A_DOC, "b": B_DOC,
            "resolutions": {CONFLICT_ID: {"choice": "a"}}})
        assert r.status_code == 200, r.text
        assert r.json()["savedProject"]["id"] == pid
        saved = c.get(f"/api/projects/{pid}").json()
        assert saved["document"]["segments"][0]["speaker"] == "A版"


def test_fixing_invalid_adjudication_allows_resave():
    """After a 409 the adjudication can be fixed, re-previewed and saved."""
    with TestClient(app) as c:
        before = len(c.get("/api/projects").json())

        # invalid custom value -> 409
        r = c.post("/api/merge", json=_merge_payload(
            resolutions={CONFLICT_ID: INVALID_CUSTOM},
            saveAsProject="合并结果"))
        assert r.status_code == 409

        # corrected custom value -> preview is clean, save succeeds
        r = c.post("/api/merge", json=_merge_payload(
            resolutions={CONFLICT_ID: VALID_CUSTOM}))
        assert r.status_code == 200
        assert r.json()["errors"] == [] and r.json()["unresolved"] == []

        r = c.post("/api/merge", json=_merge_payload(
            resolutions={CONFLICT_ID: VALID_CUSTOM},
            saveAsProject="合并结果"))
        assert r.status_code == 200, r.text
        saved = r.json()["savedProject"]
        created = c.get(f"/api/projects/{saved['id']}").json()
        seg = created["document"]["segments"][0]
        assert seg["speaker"] == "丙" and seg["end"] == 6
        assert len(c.get("/api/projects").json()) == before + 1


def run():
    test_preview_lists_conflicts_and_adjudication_errors()
    test_save_as_with_unresolved_conflict_returns_409_and_creates_nothing()
    test_save_as_with_invalid_custom_resolution_returns_409()
    test_save_as_fully_resolved_creates_project()
    test_write_back_with_problems_returns_409_and_keeps_document()
    test_write_back_fully_resolved_updates_base_project()
    test_fixing_invalid_adjudication_allows_resave()
    print("ALL MERGE API TESTS PASSED")


if __name__ == "__main__":
    run()
    shutil.rmtree(_TMP, ignore_errors=True)
