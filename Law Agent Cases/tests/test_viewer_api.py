"""Viewer round trip: edit in the UI, land in results.json, survive a resync.

Covers the seam where the three stores meet -- the FastAPI layer, the SQLite
working store and the canonical JSON -- including the staleness check that picks
up a results.json edited on another machine and synced in.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from court_viewer import db
from court_viewer.app import create_app
from court_viewer.backup import list_backups

from conftest import BOX


@pytest.fixture
def seeded(viewer_cfg, write_pipeline_case, write_page):
    write_page("p1.jpg", order=0, verbatim_text="machine transcript")
    write_page("p2.jpg", order=1, verbatim_text="second page")
    write_pipeline_case("c1", source_images=["p1.jpg", "p2.jpg"],
                        plaintiff="Machine Plaintiff", district="Natal")
    from court_viewer.build_results import build_results

    build_results(viewer_cfg)
    return viewer_cfg


@pytest.fixture
def client(seeded):
    with TestClient(create_app(seeded)) as c:
        yield c


def _results(cfg):
    return json.loads(cfg.results_json.read_text())


def _case(cfg, case_id="c1"):
    return next(c for c in _results(cfg)["cases"] if c["case_id"] == case_id)


def test_startup_builds_the_db_outside_the_project_tree(client, seeded):
    assert seeded.db_path.is_file()
    assert "CloudStorage" not in str(seeded.db_path)
    assert client.get("/api/meta").status_code == 200


def test_listing_and_detail(client):
    cases = client.get("/api/cases").json()["cases"]

    assert [c["case_id"] for c in cases] == ["c1"]
    detail = client.get("/api/cases/c1").json()
    assert detail["fields"]["plaintiff"]["gemini"] == "Machine Plaintiff"
    assert [p["filename"] for p in detail["pages"]] == ["p1.jpg", "p2.jpg"]


def test_unknown_case_is_404(client):
    assert client.get("/api/cases/nope").status_code == 404


def test_saving_an_edit_sets_edited_and_keeps_gemini(client, seeded):
    response = client.put("/api/cases/c1", json={"fields": {"plaintiff": "Corrected Name"}})

    assert response.status_code == 200
    plaintiff = _case(seeded)["fields"]["plaintiff"]
    assert plaintiff["edited"] == "Corrected Name"
    assert plaintiff["gemini"] == "Machine Plaintiff"


def test_saving_backs_up_the_previous_results_json(client, seeded):
    before = len(list_backups(seeded))

    client.put("/api/cases/c1", json={"fields": {"plaintiff": "Corrected Name"}})

    snapshots = list_backups(seeded)
    assert len(snapshots) == before + 1
    prior = json.loads(snapshots[-1].read_text())
    prior_case = next(c for c in prior["cases"] if c["case_id"] == "c1")
    assert prior_case["fields"]["plaintiff"]["edited"] is None


def test_clearing_an_edit_falls_back_to_the_machine_value(client, seeded):
    client.put("/api/cases/c1", json={"fields": {"plaintiff": "Corrected"}})
    client.put("/api/cases/c1", json={"fields": {"plaintiff": ""}})

    plaintiff = _case(seeded)["fields"]["plaintiff"]
    assert plaintiff["edited"] is None
    assert plaintiff["gemini"] == "Machine Plaintiff"


def test_page_transcript_edit_round_trips(client, seeded):
    client.put("/api/cases/c1", json={"pages": {"p1.jpg": "hand corrected"}})

    pages = {p["filename"]: p for p in _case(seeded)["pages"]}
    assert pages["p1.jpg"]["transcript"]["edited"] == "hand corrected"
    assert pages["p1.jpg"]["transcript"]["gemini"] == "machine transcript"
    assert pages["p2.jpg"]["transcript"]["edited"] is None


def test_review_status_and_notes_round_trip(client, seeded):
    client.put("/api/cases/c1", json={"review_status": "verified", "notes": "checked"})

    case = _case(seeded)
    assert case["review_status"] == "verified"
    assert case["notes"]["edited"] == "checked"


def test_search_finds_edited_values(client):
    client.put("/api/cases/c1", json={"fields": {"plaintiff": "Zzyzx Unique"}})

    hits = client.get("/api/search", params={"q": "Zzyzx"}).json()

    assert hits["count"] >= 1
    assert hits["hits"][0]["case_id"] == "c1"


def test_search_finds_transcript_text(client):
    hits = client.get("/api/search", params={"q": "machine", "scope": "transcript"}).json()

    assert hits["count"] >= 1


def test_externally_synced_results_json_is_picked_up(client, seeded):
    """The OneDrive case: another machine wrote results.json under us."""
    doc = _results(seeded)
    case = next(c for c in doc["cases"] if c["case_id"] == "c1")
    case["fields"]["plaintiff"]["edited"] = "Edited On Another Machine"
    time.sleep(0.01)
    seeded.results_json.write_text(json.dumps(doc), encoding="utf-8")
    # Push the mtime clearly past the DB's stored token.
    future = time.time() + 5
    os.utime(seeded.results_json, (future, future))

    detail = client.get("/api/cases/c1").json()

    assert detail["fields"]["plaintiff"]["edited"] == "Edited On Another Machine"


def test_our_own_export_is_not_mistaken_for_an_external_change(client, seeded):
    client.put("/api/cases/c1", json={"fields": {"plaintiff": "Ours"}})

    assert db.is_stale(seeded) is False


def test_rebuild_and_export_endpoints(client, seeded):
    assert client.post("/api/rebuild").json()["cases"] == 1
    assert client.post("/api/export").json()["ok"] is True


def test_image_endpoint_rejects_path_traversal(client):
    for box, filename in [("..", "x.jpg"), (BOX, "../secret.jpg"), ("a/b", "x.jpg")]:
        assert client.get("/api/image", params={"box": box, "filename": filename}).status_code == 400


def test_image_endpoint_404s_for_a_missing_file(client):
    response = client.get("/api/image", params={"box": BOX, "filename": "absent.jpg"})

    assert response.status_code == 404
