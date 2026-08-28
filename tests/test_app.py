"""The HTTP surface, driven end to end with a scripted model.

These go through the real routes, the real graph and the real SQLite
checkpointer — only the model is stubbed. The point is to catch the things
unit tests miss: that state survives between requests, that an edit is
recorded as a correction, and that no route submits anything.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main
from app import progress
from app.config import Settings
from tests.scripted import ScriptedClient


@pytest.fixture(autouse=True)
def no_leaked_runtime_key():
    """Settings chosen through the dialog are process-global.

    The key was always this way; the two model choices joined it when the
    Settings dialog gained dropdowns for them. All three have to be cleared
    around every test, or one test's choice silently becomes the next test's
    configuration and the failure surfaces somewhere unrelated.
    """
    app_config.clear_runtime_api_key()
    app_config.set_runtime_model("")
    app_config.set_runtime_reader_model("")
    yield
    app_config.clear_runtime_api_key()
    app_config.set_runtime_model("")
    app_config.set_runtime_reader_model("")


@pytest.fixture
def data_dir(tmp_path):
    data = tmp_path / "data"
    (data / "evidence").mkdir(parents=True)
    (data / "objective.md").write_text(
        "# Objective\n\n"
        "| Field | Value |\n|---|---|\n"
        "| `Objective_ID` | OBJ-TEST-01 |\n"
        "| `Title` | Do a difficult thing |\n"
        "| `Success_Measure` | Three of the thing |\n"
        "| `Target_Completion` | 2026-09-30 |\n",
        encoding="utf-8",
    )
    (data / "prior_update.md").write_text(
        "# Prior\n\n"
        "| Field | Value |\n|---|---|\n"
        "| `Quarter` | 2026-Q2 |\n"
        "| `Traffic_Light` | **Green** |\n"
        "| `Progress_Percent` | 45 |\n"
        "| `Key_Success` | It was all going well. |\n",
        encoding="utf-8",
    )
    (data / "evidence" / "early.md").write_text(
        "# Email — 1 May 2026\n\nAn optimistic early account.", encoding="utf-8"
    )
    (data / "evidence" / "late.md").write_text(
        "# Teams — 1 August 2026\n\nA more sober later account.", encoding="utf-8"
    )
    return data


@pytest.fixture
def client(data_dir, tmp_path, monkeypatch):
    """A client whose event loop outlives a single request.

    Runs are background tasks. TestClient tears its loop down per request
    unless entered as a context manager, which would orphan them.
    """
    settings = Settings(
        data_dir=data_dir,
        runs_dir=tmp_path / "runs",
        quarter="2026-Q3",
        anthropic_api_key="not-used-by-the-stub",
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "client", lambda: ScriptedClient())
    with TestClient(main.app) as http:
        yield http, settings


def start_run(http) -> str:
    """Kick off a run and wait for it.

    Runs are background tasks now, so the redirect arrives before the draft
    does. Polling the progress endpoint also gives the event loop the chance
    to advance the task between requests.
    """
    response = http.post("/runs", data={"quarter": "2026-Q3"}, follow_redirects=False)
    assert response.status_code == 303
    thread_id = response.headers["location"].removeprefix("/runs/")

    for _ in range(200):
        if http.get(f"/runs/{thread_id}/progress.json").json()["finished"]:
            return thread_id
    raise AssertionError(f"run {thread_id} never finished")


def acknowledge_all_fields(http, thread_id: str) -> None:
    """POST every draft field once so Approve and export can unlock."""
    row = http.get(f"/runs/{thread_id}/state.json").json()["row"]
    for field, proposal in row.items():
        if not isinstance(proposal, dict) or "edited_by_director" not in proposal:
            continue
        if proposal.get("edited_by_director"):
            continue
        value = proposal.get("value")
        if field == "Support_From":
            data = [("Support_From", item) for item in (value or [])]
        elif field == "Progress_Percent":
            data = {field: "" if value is None else str(value)}
        else:
            data = {field: "" if value is None else value}
        assert http.post(f"/runs/{thread_id}/field/{field}", data=data).status_code in {
            200,
            303,
        }


# --- landing -----------------------------------------------------------------


def test_runs_home_renders(client):
    http, _ = client
    body = http.get("/").text
    assert "CSF Personal Layer" in body
    assert "New run" in body


def test_inlined_assets_are_not_html_escaped(client):
    """Row clicks and child selectors break if Jinja escapes the inlined bundle."""
    http, _ = client
    body = http.get("/").text
    assert "tr.rowlink[data-href]" in body
    assert "&#34;tr.rowlink" not in body
    assert ".stack>*+*" in body
    assert ".stack&gt;*+*" not in body


def test_runs_table_rows_link_to_the_detail_page(client):
    http, _ = client
    body = http.get("/").text
    assert 'data-href="/runs/' in body or 'href="/runs/' in body


def test_a_running_run_shows_calculating_not_a_dash_for_the_traffic_light(client):
    """A dash in that cell reads as 'no light'. While the run is still
    deriving one, the list has to say so."""
    http, _ = client
    progress.registry.start("in-flight-test")
    try:
        body = http.get("/").text
        assert 'class="calculating"' in body
        assert "calculating" in body
        assert 'class="pill pill-status run">running' in body
        assert "in-flight-test" in body
    finally:
        progress.registry.forget("in-flight-test")


def test_new_run_lists_the_evidence_and_the_objective(client):
    http, _ = client
    body = http.get("/runs/new").text
    assert "OBJ-TEST-01" in body
    assert "Three of the thing" in body
    assert "early.md" in body and "late.md" in body


def test_new_run_shows_the_folder_as_a_folder(client):
    """The data folder is the whole input surface, so the page shows it."""
    http, _ = client
    body = http.get("/runs/new").text

    assert "Drop the quarter's files here" in body
    assert "objective.md" in body and "Objective record" in body
    assert "prior_update.md" in body and "Previous quarter row" in body
    assert "early.md" in body and "late.md" in body
    assert "Evidence" in body and "role-sel" in body


def test_the_as_of_date_is_recorded_on_the_run_and_drives_the_arithmetic(client):
    """A draft reviewed a week later must not silently change its own maths."""
    http, _ = client
    response = http.post(
        "/runs", data={"quarter": "2026-Q3", "as_of": "2026-09-20"}, follow_redirects=False
    )
    thread_id = response.headers["location"].removeprefix("/runs/")
    for _ in range(200):
        if http.get(f"/runs/{thread_id}/progress.json").json()["finished"]:
            break

    body = http.get(f"/runs/{thread_id}").text
    # Target completion in the fixture objective is 2026-09-30.
    assert "10 days remaining" in body
    assert "as of 20 Sep 2026" in body


def test_new_run_explains_a_missing_data_folder(client, tmp_path, monkeypatch):
    http, settings = client
    monkeypatch.setattr(main, "settings", settings.model_copy(update={"data_dir": tmp_path / "nope"}))
    response = http.get("/runs/new")
    assert response.status_code == 500
    assert "objective.md" in response.text


# --- generating --------------------------------------------------------------


def test_a_run_produces_a_draft_and_stages_nothing(client):
    http, settings = client
    thread_id = start_run(http)

    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["awaiting_review"] is True
    assert state["row"]["Traffic_Light"]["value"] == "Amber"
    assert state["row"]["submitted"] is False
    assert not (settings.run_dir(thread_id) / "staged_row.json").exists()


def test_review_page_shows_the_proposal_and_the_standing_notice(client):
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text
    assert "Nothing has been submitted" in body
    assert "Substrate-Drafted" in body
    # Trend_vs_Prior_Quarter is explained on the export page, as in index.html.


def test_review_page_contrasts_the_prior_quarter(client):
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text
    assert "Your submitted 2026-Q2 update" in body
    assert "Green" in body and "Amber" in body
    assert "At a glance" in body
    assert "dash-fill-Amber" in body


def test_dashboard_page_is_this_draft_not_the_institute_model(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}/dashboard").text
    assert body.count("dash-fill-") >= 4
    assert "On track" in body
    assert "Power BI" in body
    assert "Substrate-Drafted" in body
    assert "Trend_vs_Prior_Quarter" in body
    assert "not a field on this draft" in body


def test_run_audit_overview_separates_its_cards(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}/audit").text
    assert ".tab-panel .stack" in body
    overview = body.split('data-panel="overview"', 1)[1].split("data-panel=", 1)[0]
    assert 'class="stack"' in overview
    assert "This run" in overview
    assert "What this run cost" in overview
    edits = body.split('data-panel="edits"', 1)[1].split("data-panel=", 1)[0]
    assert 'class="stack"' in edits
    assert ".tab-panel.active{display:flex" in body


def test_a_running_run_snapshot_is_html_not_json_404(client):
    """Snapshot in the sidebar must render while the draft is still deriving.

    A JSON 404 there reads as the page itself being missing."""
    http, _ = client
    progress.registry.start("in-flight-test")
    try:
        response = http.get("/runs/in-flight-test/dashboard")
        assert response.status_code == 200
        assert "application/json" not in response.headers.get("content-type", "")
        assert "Draft snapshot" in response.text
        assert "calculating" in response.text
        assert '{"detail"' not in response.text
    finally:
        progress.registry.forget("in-flight-test")


def test_product_dashboard_covers_the_workspace(client):
    http, _ = client
    start_run(http)
    home = http.get("/").text
    assert 'href="/dashboard"' in home
    body = http.get("/dashboard").text
    assert "Every draft in this workspace" in body
    assert "Power BI" in body
    assert "OBJ-TEST-01" in body
    assert "Trend_vs_Prior_Quarter" not in body or "not a field" in body


def test_abstained_field_is_shown_as_needing_input(client):
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text
    assert "needs your input" in body


def test_unknown_run_is_a_404(client):
    http, _ = client
    assert http.get("/runs/does-not-exist").status_code == 404


# --- editing -----------------------------------------------------------------


def test_editing_a_field_records_a_correction_and_persists(client):
    http, _ = client
    thread_id = start_run(http)

    response = http.post(
        f"/runs/{thread_id}/field/Key_Success",
        data={"Key_Success": "Smaller than we said."},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert 'data-field-ack="1"' in response.text

    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Key_Success"]["value"] == "Smaller than we said."
    assert state["row"]["Key_Success"]["edited_by_director"] is True


def test_acknowledging_again_returns_the_field_to_normal_mode(client):
    http, _ = client
    thread_id = start_run(http)
    original = http.get(f"/runs/{thread_id}/state.json").json()["row"]["Progress_Percent"][
        "value"
    ]

    ack = http.post(
        f"/runs/{thread_id}/field/Progress_Percent",
        data={"Progress_Percent": "40"},
        headers={"X-Requested-With": "fetch"},
    )
    assert ack.status_code == 200
    assert 'data-field-ack="1"' in ack.text
    assert "✓ Acknowledged" in ack.text

    undo = http.post(
        f"/runs/{thread_id}/field/Progress_Percent",
        data={"Progress_Percent": "40"},
        headers={"X-Requested-With": "fetch"},
    )
    assert undo.status_code == 200
    assert 'data-field-ack="0"' in undo.text
    assert 'data-field-ack="0"' in undo.text

    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Progress_Percent"]["edited_by_director"] is False
    assert state["row"]["Progress_Percent"]["value"] == original
    assert not any(
        c["field"] == "Progress_Percent" for c in state.get("corrections", [])
    )

    acknowledge_all_fields(http, thread_id)
    assert (
        http.post(f"/runs/{thread_id}/approve", follow_redirects=False).status_code
        == 303
    )

    # Start a fresh run so we can prove clearing one ack locks export again.
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)
    value = http.get(f"/runs/{thread_id}/state.json").json()["row"]["Progress_Percent"][
        "value"
    ]
    cleared = http.post(
        f"/runs/{thread_id}/field/Progress_Percent",
        data={"Progress_Percent": "" if value is None else str(value)},
        headers={"X-Requested-With": "fetch"},
    )
    assert 'data-field-ack="0"' in cleared.text
    assert http.get(f"/runs/{thread_id}/state.json").json()["row"]["Progress_Percent"][
        "edited_by_director"
    ] is False

    blocked = http.post(f"/runs/{thread_id}/approve", follow_redirects=False)
    assert blocked.status_code == 400
    assert "Acknowledge every field" in blocked.text


def test_editing_the_same_field_twice_keeps_one_correction(client):
    http, settings = client
    thread_id = start_run(http)

    for value in ("First go.", "Second go."):
        http.post(
            f"/runs/{thread_id}/field/Key_Challenge",
            data={"Key_Challenge": value},
            headers={"X-Requested-With": "fetch"},
        )

    acknowledge_all_fields(http, thread_id)
    http.post(f"/runs/{thread_id}/stage", follow_redirects=False)
    lines = (settings.run_dir(thread_id) / "corrections.jsonl").read_text().strip().splitlines()
    key_challenge = [json.loads(line) for line in lines if json.loads(line)["field"] == "Key_Challenge"]
    assert len(key_challenge) == 1
    assert key_challenge[0]["director_value"] == "Second go."


def test_traffic_light_can_be_overridden_by_the_director(client):
    http, _ = client
    thread_id = start_run(http)

    http.post(
        f"/runs/{thread_id}/field/Traffic_Light",
        data={"Traffic_Light": "Red"},
        headers={"X-Requested-With": "fetch"},
    )
    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Traffic_Light"]["value"] == "Red"


def test_progress_is_clamped_to_the_valid_range(client):
    http, _ = client
    thread_id = start_run(http)

    http.post(
        f"/runs/{thread_id}/field/Progress_Percent",
        data={"Progress_Percent": "5000"},
        headers={"X-Requested-With": "fetch"},
    )
    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Progress_Percent"]["value"] == 100


def test_support_from_outside_the_vocabulary_is_dropped(client):
    http, _ = client
    thread_id = start_run(http)

    http.post(
        f"/runs/{thread_id}/field/Support_From",
        data={"Support_From": ["Finance", "NotAThing"]},
        headers={"X-Requested-With": "fetch"},
    )
    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Support_From"]["value"] == ["Finance"]


def test_editing_without_javascript_redirects_back(client):
    http, _ = client
    thread_id = start_run(http)

    response = http.post(
        f"/runs/{thread_id}/field/Key_Success",
        data={"Key_Success": "Posted without fetch."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/runs/{thread_id}")

    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Key_Success"]["value"] == "Posted without fetch."


def test_editing_an_unknown_field_is_a_404(client):
    http, _ = client
    thread_id = start_run(http)
    assert http.post(f"/runs/{thread_id}/field/Nonsense", data={}).status_code == 404


def test_trend_field_cannot_be_set_through_the_api(client):
    http, _ = client
    thread_id = start_run(http)
    response = http.post(
        f"/runs/{thread_id}/field/Trend_vs_Prior_Quarter",
        data={"Trend_vs_Prior_Quarter": "Deteriorated"},
    )
    assert response.status_code == 404

    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert "Trend_vs_Prior_Quarter" not in state["row"]


# --- staging -----------------------------------------------------------------


def test_staging_writes_a_file_that_is_not_submitted(client):
    http, settings = client
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)

    http.post(f"/runs/{thread_id}/stage", follow_redirects=False)

    staged = json.loads((settings.run_dir(thread_id) / "staged_row.json").read_text())
    assert staged["submitted"] is False
    assert staged["Source"] == "Substrate-Drafted"
    assert "Trend_vs_Prior_Quarter" not in staged
    assert staged["thread_id"] == thread_id


def test_acknowledge_all_marks_every_field_and_unlocks_export(client):
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}").text
    assert "Acknowledge all" in body
    assert "acknowledge-all" in body
    footer = body.split('id="approve-footer"', 1)[1]
    assert footer.index("acknowledge-all") < footer.index('data-action="open-approve"')

    response = http.post(
        f"/runs/{thread_id}/acknowledge-all", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{thread_id}"

    state = http.get(f"/runs/{thread_id}/state.json").json()
    for field, proposal in state["row"].items():
        if isinstance(proposal, dict) and "edited_by_director" in proposal:
            assert proposal["edited_by_director"] is True, field

    review = http.get(f"/runs/{thread_id}").text
    assert "✓ All acknowledged" in review
    approve_btn = re.search(r"<button\b[^>]*data-action=\"open-approve\"[^>]*>", review)
    assert approve_btn is not None
    assert "disabled" not in approve_btn.group(0)

    # Toggle off — clears every acknowledgement and locks export again.
    cleared = http.post(f"/runs/{thread_id}/acknowledge-all", follow_redirects=False)
    assert cleared.status_code == 303
    state = http.get(f"/runs/{thread_id}/state.json").json()
    for field, proposal in state["row"].items():
        if isinstance(proposal, dict) and "edited_by_director" in proposal:
            assert proposal["edited_by_director"] is False, field
    locked = http.get(f"/runs/{thread_id}").text
    assert "Acknowledge all" in locked
    approve_btn = re.search(r"<button\b[^>]*data-action=\"open-approve\"[^>]*>", locked)
    assert approve_btn is not None
    assert "disabled" in approve_btn.group(0)
    assert http.post(f"/runs/{thread_id}/approve", follow_redirects=False).status_code == 400


def test_staging_is_blocked_until_every_field_is_acknowledged(client):
    http, settings = client
    thread_id = start_run(http)

    response = http.post(f"/runs/{thread_id}/approve", follow_redirects=False)
    assert response.status_code == 400
    assert "Acknowledge every field" in response.text
    assert not (settings.run_dir(thread_id) / "staged_row.json").exists()

    body = http.get(f"/runs/{thread_id}").text
    assert "Acknowledge" in body, "every field offers acknowledgement"
    assert "Acknowledge every field above before export unlocks" in body
    approve_btn = re.search(r"<button\b[^>]*data-action=\"open-approve\"[^>]*>", body)
    assert approve_btn is not None
    assert "disabled" in approve_btn.group(0)


def test_staged_row_is_downloadable_after_staging(client):
    http, _ = client
    thread_id = start_run(http)
    assert http.get(f"/runs/{thread_id}/staged.json").status_code == 404

    acknowledge_all_fields(http, thread_id)
    http.post(f"/runs/{thread_id}/stage", follow_redirects=False)
    assert http.get(f"/runs/{thread_id}/staged.json").json()["submitted"] is False


def test_the_export_page_renders_the_approved_row(client):
    """The export page holds a flattened dict where review holds a DraftRow."""
    http, _ = client
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)
    http.post(f"/runs/{thread_id}/stage", follow_redirects=False)

    response = http.get(f"/runs/{thread_id}/export")
    assert response.status_code == 200
    body = response.text
    assert "Row approved" in body
    assert "OBJ-TEST-01" in body and "2026-Q3" in body
    assert "Substrate-Drafted" in body
    # Trend_vs_Prior_Quarter is explained on the export page, as in index.html.


def test_review_page_says_staged_not_submitted(client):
    http, _ = client
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)
    http.post(f"/runs/{thread_id}/stage", follow_redirects=False)

    body = http.get(f"/runs/{thread_id}").text
    assert "ready for you to submit yourself" in body
    export_btn = re.search(r"<a\b[^>]*\bdata-open-export\b[^>]*>", body)
    assert export_btn is not None
    assert "aria-disabled" not in export_btn.group(0)


def test_open_export_stays_locked_until_every_field_is_acknowledged(client):
    http, settings = client
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)
    assert http.post(f"/runs/{thread_id}/stage", follow_redirects=False).status_code == 303

    # Un-acknowledge one field — Open export must lock and export routes refuse.
    value = http.get(f"/runs/{thread_id}/state.json").json()["row"]["Progress_Percent"][
        "value"
    ]
    cleared = http.post(
        f"/runs/{thread_id}/field/Progress_Percent",
        data={"Progress_Percent": "" if value is None else str(value)},
        headers={"X-Requested-With": "fetch"},
    )
    assert 'data-field-ack="0"' in cleared.text

    body = http.get(f"/runs/{thread_id}").text
    export_btn = re.search(r"<a\b[^>]*\bdata-open-export\b[^>]*>", body)
    assert export_btn is not None
    assert 'aria-disabled="true"' in export_btn.group(0)
    assert "Acknowledge every field above before export unlocks" in body

    blocked = http.get(f"/runs/{thread_id}/export", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == f"/runs/{thread_id}"
    assert http.get(f"/runs/{thread_id}/staged.json").status_code == 400
    assert http.get(f"/runs/{thread_id}/export.csv").status_code == 400
    assert (settings.run_dir(thread_id) / "staged_row.json").exists()


# --- evidence ----------------------------------------------------------------


def test_evidence_page_highlights_the_cited_block(client):
    http, _ = client
    thread_id = start_run(http)

    state = http.get(f"/runs/{thread_id}/state.json").json()
    claim = state["claims"][0]

    body = http.get(
        f"/runs/{thread_id}/evidence/{claim['doc_id']}?claim={claim['claim_id']}"
    ).text
    assert "codeline hl" in body, "the cited lines should be marked"
    assert claim["claim_id"] in body


def test_evidence_page_without_a_claim_highlights_nothing(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}/evidence/E1").text
    assert "codeline" in body
    # Class applied to a block — not the inlined stylesheet rule name alone.
    assert "codeline hl" not in body
    assert "ev-docname" in body
    assert "overflow-wrap:anywhere" in body


def test_unknown_document_is_a_404(client):
    http, _ = client
    thread_id = start_run(http)
    assert http.get(f"/runs/{thread_id}/evidence/E99").status_code == 404


def test_evidence_page_shows_the_document_with_line_numbers(client):
    """A citation that cannot be located in the source is decoration."""
    http, _ = client
    thread_id = start_run(http)

    state = http.get(f"/runs/{thread_id}/state.json").json()
    claim = state["claims"][0]

    body = http.get(
        f"/runs/{thread_id}/evidence/{claim['doc_id']}?claim={claim['claim_id']}"
    ).text
    assert 'class="codeline hl"' in body, "the cited lines should be marked in the source"
    assert 'id="L1"' in body, "lines should carry the document's own numbers"


def test_a_citation_chip_carries_its_statement_and_a_way_back(client):
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}").text
    assert "data-tip=" in body, "the chip should preview the statement on hover"
    assert "back=/runs/" in body, "the chip should know the field it was clicked from"
    assert "%23field-" in body, "and the field within it"

    # The back link is honoured, and only ever points back into this app.
    page = http.get(f"/runs/{thread_id}/evidence/E1?back=https://elsewhere.example").text
    assert "elsewhere.example" not in page
    assert f'href="/runs/{thread_id}"' in page


# --- the review screen -------------------------------------------------------


def test_the_traffic_light_carries_a_glyph_not_only_a_colour(client):
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text
    # Amber is what the scripted assessment proposes.
    assert '<span class="glyph" aria-hidden="true">◐</span>Amber' in body


def test_the_header_states_the_target_and_the_time_left(client):
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text
    assert "Target completion 30 Sep 2026" in body, "dates should read as dates"
    assert "days remaining" in body


def test_a_derived_field_says_who_decided_it(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text
    assert "derived by the tool" in body

    http.post(
        f"/runs/{thread_id}/field/Traffic_Light",
        data={"Traffic_Light": "Red", "reason": "The Ethiopia annex is not startable."},
        headers={"X-Requested-With": "fetch"},
    )
    body = http.get(f"/runs/{thread_id}").text
    assert "you changed this" in body
    assert "tool proposed" in body, "an override should show what it replaced"
    assert "The Ethiopia annex is not startable." in body, "the reason travels with the row"


def test_an_override_reason_reaches_the_audit_trail(client):
    from app import audit

    http, settings = client
    thread_id = start_run(http)
    http.post(
        f"/runs/{thread_id}/field/Traffic_Light",
        data={"Traffic_Light": "Red", "reason": "No mitigation inside the quarter."},
        headers={"X-Requested-With": "fetch"},
    )

    edits = audit.of_kind(audit.read(settings.run_dir(thread_id)), "edit")
    override = next(e for e in edits if e["field"] == "Traffic_Light")
    assert override["reason"] == "No mitigation inside the quarter."


def test_a_finding_can_be_acknowledged_and_unacknowledged(client):
    from app import audit

    http, settings = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}").text
    match = re.search(r"/finding/(conflict-[0-9a-f]+)", body)
    assert match, "each finding should offer an acknowledgement"
    ident = match.group(1)

    http.post(f"/runs/{thread_id}/finding/{ident}", data={"title": "A disagreement"})
    assert audit.acknowledged(audit.read(settings.run_dir(thread_id))) == {ident}
    assert "✓ Acknowledged" in http.get(f"/runs/{thread_id}").text

    # Pressing again withdraws it, and both facts stay in the trail.
    http.post(f"/runs/{thread_id}/finding/{ident}", data={"title": "A disagreement"})
    rows = audit.read(settings.run_dir(thread_id))
    assert audit.acknowledged(rows) == set()
    assert [r["acked"] for r in audit.of_kind(rows, "ack")] == [True, False]


def test_acknowledging_a_finding_does_not_unlock_export(client):
    """The gate is field-level. A second one here would be clickable-through."""
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}").text
    ident = re.search(r"/finding/(conflict-[0-9a-f]+)", body).group(1)
    http.post(f"/runs/{thread_id}/finding/{ident}", data={"title": "A disagreement"})

    assert http.post(f"/runs/{thread_id}/stage", follow_redirects=False).status_code == 400


def test_a_citation_inside_a_sentence_is_followable(client):
    """A rationale citing [E1.1] in prose gets a real chip under it."""
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    # The scripted rationale is "...the measure has receded [E1.1]."
    assert "has receded [E1.1]" in body
    after = body.split("has receded [E1.1]", 1)[1][:600]
    assert f'href="/runs/{thread_id}/evidence/E1?claim=E1.1' in after, (
        "the bracketed reference should be followable, not just readable"
    )


def test_every_reference_in_a_sentence_gets_a_chip(client):
    """The chip row must agree with the sentence above it.

    A note citing four sources with one chip under it reads as three broken
    links, and the reader cannot tell which. Deduplicating against citations
    shown elsewhere in the card produced exactly that.
    """
    from tests.scripted import ScriptedClient, default_conflicts
    from app.schema import Conflict, Reconciliation

    http, _ = client
    dense = Reconciliation(
        conflicts=[
            Conflict(
                topic="Category coverage",
                winning_claim_id="E2.1",
                superseded_claim_ids=["E1.1"],
                rule_applied="later_supersedes_earlier",
                note=(
                    "The May email said five categories [E1.1] and the outbound text "
                    "asserts five [E2.1]. The August chat reports two of five [E1.1], "
                    "and the meeting note repeats two of five [E2.1]."
                ),
            )
        ],
        gaps=[],
        reconciled_position="As stated [E1.1] and [E2.1].",
    )
    main.client = lambda: ScriptedClient(conflicts=dense)
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}").text
    note_start = body.index("The May email said five categories")
    after = body[note_start : note_start + 2500]

    # Both cited documents are reachable from under the note.
    assert 'href="/runs/' + thread_id + '/evidence/E1?claim=E1.1' in after
    assert 'href="/runs/' + thread_id + '/evidence/E2?claim=E2.1' in after


def test_each_superseded_claim_is_paired_with_its_own_citation(client):
    """A conflict can overturn several claims at once.

    Stacking the quotes and then running the chips together leaves the reader
    unable to tell which chip belongs to which quote — the one thing this
    panel exists to make obvious.
    """
    from app.schema import Conflict, Reconciliation
    from tests.scripted import ScriptedClient

    http, _ = client
    main.client = lambda: ScriptedClient(
        conflicts=Reconciliation(
            conflicts=[
                Conflict(
                    topic="Scope: five categories or two",
                    winning_claim_id="E2.1",
                    superseded_claim_ids=["E1.1", "E2.1"],
                    rule_applied="later_supersedes_earlier",
                    note="Two earlier accounts said five.",
                )
            ],
            gaps=[],
            reconciled_position="Two of five.",
        )
    )
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}").text
    # Slice the superseded cell only — the superseding cell starts right after.
    cell = body.split('class="vs-cell super"', 1)[1].split('class="vs-cell"', 1)[0]
    assert cell.count('class="vs-claim"') == 2, "one block per superseded claim"
    assert "2 claims" in cell, "the count should say how many were overturned"
    # Each chip sits in its own row rather than running into the next.
    assert cell.count('class="chips"') == 2





def test_severity_comes_from_the_reconcile_pass_not_from_us(client):
    """Nothing in this codebase decides how loud a finding is.

    The reconcile pass is the only step that has read every account of the same
    event, so it is the only one that can tell a scope change from a routine
    correction. A rule here would be guessing from the shape of its output.
    """
    from app.schema import Conflict, Reconciliation
    from tests.scripted import ScriptedClient

    http, _ = client
    main.client = lambda: ScriptedClient(
        conflicts=Reconciliation(
            conflicts=[
                Conflict(
                    topic="Scope shrank",
                    winning_claim_id="E2.1",
                    superseded_claim_ids=["E1.1"],
                    rule_applied="participant_supersedes_outbound",
                    note="n",
                    severity="high",
                    severity_reason="signed scope is smaller than the May draft said",
                )
            ],
            gaps=[],
            reconciled_position="Two of five [E2.1].",
        )
    )
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    assert "pill-sev-high" in body
    assert "signed scope is smaller than the May draft said" in body


def test_a_medium_finding_is_not_shown_as_high(client):
    """The default scripted conflict is a routine correction."""
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text

    assert "pill-sev-medium" in body
    assert "routine correction, no change to coverage" in body
    assert "pill-sev-high" not in body.split('id="ag-div"', 1)[1].split("</section>", 1)[0]


def test_a_finding_with_no_severity_from_the_model_still_renders(client):
    """A model that omits it must not blank the badge or fail the run."""
    from app.schema import Conflict, Reconciliation
    from tests.scripted import ScriptedClient

    http, _ = client
    main.client = lambda: ScriptedClient(
        conflicts=Reconciliation(
            conflicts=[
                Conflict(
                    topic="t",
                    winning_claim_id="E2.1",
                    superseded_claim_ids=["E1.1"],
                    rule_applied="other",
                    note="n",
                )
            ],
            gaps=[],
            reconciled_position="p [E2.1].",
        )
    )
    body = http.get(f"/runs/{start_run(http)}").text
    assert "pill-sev-medium" in body, "the schema default carries it"


def test_a_field_shows_each_citation_once(client):
    """A rationale cites what its field rests on, so the two lists overlap.

    Rendered as separate rows they put the same ids on screen twice, one line
    under the other, which reads as two different sets of evidence.
    """
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    for field in ("Traffic_Light", "Progress_Percent", "Key_Success"):
        # The whole card: from its id up to whatever field comes next.
        card = body.split(f'id="field-{field}"', 1)[1].split('id="field-', 1)[0]
        rows = [
            re.findall(r'\?claim=([^&"]+)', row)
            for row in card.split('<div class="chips"')[1:]
        ]
        assert any(rows), f"{field} shows no citations at all"
        seen: set[str] = set()
        for row in rows:
            assert not seen & set(row), f"{field} repeats {sorted(seen & set(row))}"
            seen |= set(row)


def test_the_reconciled_position_cites_clickably(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    assert "The position is as stated" in body
    supports = body.split("What the evidence supports", 1)[1]
    assert "cite-mark" in supports
    assert f'href="/runs/{thread_id}/evidence/E2?claim=E2.1' in supports
    assert "cited-quote" in supports


def test_why_in_full_is_a_button_not_a_caption(client):
    """A bare summary reads as part of the argument. It has to look clickable."""
    http, _ = client
    body = http.get(f"/runs/{start_run(http)}").text
    assert "why-full-btn" in body
    assert body.count("Why, in full") >= 2
    assert "why-full-body" in body
    assert "cited-rich" in body
    why = body.split("why-full-body", 1)[1]
    assert "cite-mark" in why


def test_the_running_page_has_a_live_stream(client):
    http, _ = client
    progress.registry.start("in-flight-test")
    try:
        body = http.get("/runs/in-flight-test").text
        assert "Preparing your draft" in body
        assert 'id="run-stream"' in body
        assert "What's happening" in body
        assert "stage-spin" in body or "stage-row active" in body
    finally:
        progress.registry.forget("in-flight-test")


def test_support_from_says_why_each_function_was_picked(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    flat = re.sub(r"\s+", " ", body)
    assert "Why each was picked" in flat
    assert "Needs a legal drafter seconded" in flat
    # Other has to explain itself, or the missing vocabulary value is lost.
    assert "has no value in the closed vocabulary" in flat


def test_both_derived_fields_offer_an_override(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    for field in ("Traffic_Light", "Progress_Percent"):
        assert f'data-action="open-override" data-field="{field}"' in body, field
        assert f'data-override-for="{field}"' in body, field
    assert "Why are you changing it?" in body


def test_an_override_records_the_value_and_the_reason(client):
    http, _ = client
    thread_id = start_run(http)

    http.post(
        f"/runs/{thread_id}/field/Traffic_Light",
        data={"Traffic_Light": "Red", "reason": "No mitigation inside the quarter."},
    )
    body = http.get(f"/runs/{thread_id}").text
    assert "you changed this" in body
    assert "tool proposed" in body
    assert "No mitigation inside the quarter." in body


# --- the audit trail ---------------------------------------------------------


def test_the_audit_trail_records_stages_model_calls_and_edits(client):
    http, settings = client
    thread_id = start_run(http)
    http.post(
        f"/runs/{thread_id}/field/Key_Success",
        data={"Key_Success": "A rather different sentence."},
        headers={"X-Requested-With": "fetch"},
    )

    body = http.get(f"/runs/{thread_id}/audit").text
    assert "Run events" in body
    assert "Model calls" in body
    assert "Director edits" in body
    assert "Key_Success" in body
    assert "scripted" in body, "each model call should name the model that served it"


def test_the_audit_trail_outlives_the_in_memory_progress_stream(client):
    """A restart empties the registry. The trail is the copy still there."""
    from app import progress

    http, settings = client
    thread_id = start_run(http)
    progress.registry.forget(thread_id)

    body = http.get(f"/runs/{thread_id}/audit").text
    assert "Reconciling what the documents disagree about" in body
    assert "No events recorded for this run" not in body


def test_director_edits_are_append_only(client):
    """Ten edits to one field are ten rows — the corrections list keeps one."""
    from app import audit

    http, settings = client
    thread_id = start_run(http)
    for text in ("First attempt.", "Second attempt.", "Third attempt."):
        http.post(
            f"/runs/{thread_id}/field/Key_Success",
            data={"Key_Success": text},
            headers={"X-Requested-With": "fetch"},
        )

    edits = audit.of_kind(audit.read(settings.run_dir(thread_id)), "edit")
    key_success = [e for e in edits if e["field"] == "Key_Success"]
    assert len(key_success) == 3
    assert [e["value_after"] for e in key_success] == [
        "First attempt.",
        "Second attempt.",
        "Third attempt.",
    ]
    assert all(e["char_distance"] is not None for e in key_success)

    # The row itself keeps only the latest value — that is what the trail is for.
    state = http.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Key_Success"]["value"] == "Third attempt."


def test_overriding_a_derived_field_is_logged_as_an_override(client):
    from app import audit

    http, settings = client
    thread_id = start_run(http)
    http.post(
        f"/runs/{thread_id}/field/Traffic_Light",
        data={"Traffic_Light": "Red"},
        headers={"X-Requested-With": "fetch"},
    )

    edits = audit.of_kind(audit.read(settings.run_dir(thread_id)), "edit")
    override = next(e for e in edits if e["field"] == "Traffic_Light")
    assert override["edit_kind"] == "override"
    assert override["value_before"] == "Amber"
    assert override["value_after"] == "Red"


def test_the_downloadable_trail_carries_the_model_call_ledger(client):
    http, _ = client
    thread_id = start_run(http)

    response = http.get(f"/runs/{thread_id}/audit.md")
    assert response.status_code == 200
    body = response.text
    assert "# Evidence trail" in body
    assert "## Model calls" in body
    assert "## Pipeline events" in body
    assert "No prompt or response body is stored" in body


def test_the_trail_records_no_prompt_or_response_text(client):
    """The audit says what happened, not the evidence text a second time."""
    from app import audit

    http, settings = client
    thread_id = start_run(http)

    rows = audit.read(settings.run_dir(thread_id))
    assert rows
    for row in audit.of_kind(rows, "llm_call"):
        assert set(row) <= {
            "at",
            "kind",
            "stage",
            "prompt_name",
            "model",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "stop_reason",
        }


# --- persistence -------------------------------------------------------------


def test_state_survives_a_new_client(client, data_dir, tmp_path, monkeypatch):
    """A director can close the tab and come back to the same draft."""
    http, settings = client
    thread_id = start_run(http)
    http.post(
        f"/runs/{thread_id}/field/Key_Success",
        data={"Key_Success": "Edited before closing the tab."},
        headers={"X-Requested-With": "fetch"},
    )

    with TestClient(main.app) as fresh:
        state = fresh.get(f"/runs/{thread_id}/state.json").json()
    assert state["row"]["Key_Success"]["value"] == "Edited before closing the tab."


# --- the key is required -----------------------------------------------------


def test_generating_without_a_key_fails_cleanly(data_dir, tmp_path, monkeypatch):
    """There is no offline mode. A canned draft that looked real would be worse."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        main,
        "settings",
        Settings(data_dir=data_dir, runs_dir=tmp_path / "runs", anthropic_api_key=""),
    )
    http = TestClient(main.app, raise_server_exceptions=False)

    assert "LLM not configured" in http.get("/runs/new").text

    # A rendered explanation, not a bare JSON error: this is the first thing
    # someone hits if they open the app before setting a key.
    response = http.post("/runs", data={"quarter": "2026-Q3"})
    assert response.status_code == 400
    assert "No API key loaded" in response.text


def test_missing_key_raises_rather_than_falling_back():
    from app.config import MissingAPIKey
    from app.llm import AnthropicClient

    with pytest.raises(MissingAPIKey):
        AnthropicClient(Settings(anthropic_api_key=""))


def test_empty_support_from_is_not_flagged_as_missing_evidence(client):
    """An empty multi-select is a real answer, not a defect."""
    body = http_body(client)
    assert "No supporting evidence was cited" not in body


def http_body(client):
    http, _ = client
    return http.get(f"/runs/{start_run(http)}").text


# --- entering a key through the interface ------------------------------------


@pytest.fixture
def keyless(data_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        main,
        "settings",
        Settings(data_dir=data_dir, runs_dir=tmp_path / "runs", anthropic_api_key=""),
    )
    return TestClient(main.app, raise_server_exceptions=False)


def accept_key(monkeypatch):
    async def ok(self):
        return None

    monkeypatch.setattr("app.llm.AnthropicClient.check", ok)


def reject_key(monkeypatch, message="authentication_error: invalid x-api-key"):
    async def fail(self):
        raise RuntimeError(message)

    monkeypatch.setattr("app.llm.AnthropicClient.check", fail)


def test_a_working_key_is_accepted_and_unlocks_drafting(keyless, monkeypatch):
    accept_key(monkeypatch)
    body = keyless.post("/settings/key", data={"api_key": "sk-ant-test-0123456789abcd"}).text

    assert "Key accepted" in body
    assert "Held in this server process" not in body
    assert "No model connected yet" not in body, "drafting should be unblocked"
    assert app_config.runtime_api_key() == "sk-ant-test-0123456789abcd"


def test_the_key_is_never_rendered_back_in_full(keyless, monkeypatch):
    accept_key(monkeypatch)
    secret = "sk-ant-test-0123456789abcd"
    body = keyless.post("/settings/key", data={"api_key": secret}).text

    assert secret not in body, "the key must not be echoed into the page"
    assert "sk-ant-…abcd" in body, "a masked form should be shown instead"


def test_a_rejected_key_is_not_kept(keyless, monkeypatch):
    reject_key(monkeypatch)
    response = keyless.post("/settings/key", data={"api_key": "sk-ant-wrong"})

    assert response.status_code == 400
    assert "rejected" in response.text
    assert app_config.runtime_api_key() == "", "a key that failed must not stay loaded"


def test_error_messages_are_actionable(keyless, monkeypatch):
    for raised, expected in [
        ("authentication_error", "rejected"),
        ("403 permission denied", "not permitted"),
        ("insufficient credit balance", "no available credit"),
    ]:
        reject_key(monkeypatch, raised)
        assert expected in keyless.post("/settings/key", data={"api_key": "sk-x"}).text


def test_an_empty_submission_is_rejected(keyless):
    response = keyless.post("/settings/key", data={"api_key": "   "})
    assert response.status_code == 400
    assert "Paste a key first" in response.text


def test_a_key_can_be_cleared(keyless, monkeypatch):
    accept_key(monkeypatch)
    keyless.post("/settings/key", data={"api_key": "sk-ant-test-0123456789abcd"})

    body = keyless.post("/settings/key/clear").text
    assert "cleared" in body
    assert "LLM not configured" in body
    assert app_config.runtime_api_key() == ""


def test_entered_key_takes_precedence_over_the_environment(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-environment")
    settings = Settings(data_dir=data_dir, runs_dir=tmp_path / "runs", anthropic_api_key="")
    assert settings.resolved_api_key() == "sk-ant-from-the-environment"

    app_config.set_runtime_api_key("sk-ant-entered-in-the-browser")
    assert settings.resolved_api_key() == "sk-ant-entered-in-the-browser"


def test_masking_never_reveals_a_short_key():
    assert app_config.mask("sk-ant-0123456789abcdef").endswith("cdef")
    assert "0123456789" not in app_config.mask("sk-ant-0123456789abcdef")
    assert app_config.mask("short") == "…hort"
    assert app_config.mask("") == ""


def test_drafting_without_a_key_explains_itself(keyless):
    """A dead button is indistinguishable from a broken one."""
    response = keyless.post("/runs", data={"quarter": "2026-Q3"})
    assert response.status_code == 400
    assert "No API key loaded" in response.text



def test_a_failed_run_shows_a_page_not_a_stack_trace(client, monkeypatch):
    """A director must never be shown a traceback."""
    http, _ = client

    class Exploding(ScriptedClient):
        async def structured(self, system, instruction, schema):
            raise RuntimeError("something deep in the graph went wrong")

    monkeypatch.setattr(main, "client", lambda: Exploding())
    redirect = http.post("/runs", data={"quarter": "2026-Q3"}, follow_redirects=False)
    thread_id = redirect.headers["location"].removeprefix("/runs/")
    for _ in range(200):
        if http.get(f"/runs/{thread_id}/progress.json").json()["finished"]:
            break

    response = http.get(f"/runs/{thread_id}")
    assert response.status_code == 500
    assert "could not be completed" in response.text
    assert "no partial draft has been created" in response.text.lower()
    assert "Traceback" not in response.text


# --- putting data in through the interface -----------------------------------


def test_evidence_can_be_pasted_in_and_is_read_on_the_next_run(client):
    http, settings = client

    response = http.post(
        "/evidence/add",
        data={
            "title": "Email — 30 September 2026",
            "date": "",
            "body": "The second agreement was signed yesterday.",
        },
    )
    assert response.status_code == 200
    assert "Added" in response.text
    assert "3 evidence documents" in response.text, "the list should reflect the new document"

    # And the workflow actually reads it.
    state = http.get(f"/runs/{start_run(http)}/state.json").json()
    assert len({c["doc_id"] for c in state["claims"]}) == 3


def test_a_pasted_document_lands_in_date_order(client):
    http, _ = client
    http.post(
        "/evidence/add",
        data={"title": "A note", "date": "1 June 2026", "body": "Something."},
    )
    body = http.get("/runs/new").text
    assert "2026-06-01" in body


def test_an_upload_is_accepted(client):
    http, _ = client
    response = http.post(
        "/evidence/add",
        files={"files": ("extra.md", "# Extra — 5 July 2026\n\nBody text.".encode(), "text/markdown")},
    )
    assert "Added" in response.text
    assert "3 evidence documents" in response.text


def test_adding_nothing_says_so(client):
    http, _ = client
    response = http.post("/evidence/add", data={"title": "", "body": ""})
    assert response.status_code == 400
    assert "Nothing to add" in response.text


def test_evidence_can_be_edited_in_place(client, data_dir):
    http, _ = client
    response = http.get("/evidence/early.md/edit")
    assert response.status_code == 200
    assert "An optimistic early account" in response.text
    assert 'id="md-preview"' in response.text
    assert "data-md-source" in response.text

    saved = http.post(
        "/evidence/early.md/edit",
        data={"text": "# Email — 1 May 2026\n\nCorrected account."},
    )
    assert "Saved" in saved.text
    assert "Corrected account." in (data_dir / "evidence" / "early.md").read_text()


def test_the_objective_markdown_can_be_edited_from_the_workspace(client, data_dir):
    http, _ = client
    response = http.get("/evidence/objective.md/edit")
    assert response.status_code == 200
    assert "Do a difficult thing" in response.text
    assert "Objective record" in response.text
    assert 'id="md-preview"' in response.text
    assert "<h1" in response.text

    saved = http.post(
        "/evidence/objective.md/edit",
        data={"text": "# Objective\n\nEdited in place from the workspace.\n"},
    )
    assert "Saved" in saved.text
    text = (data_dir / "objective.md").read_text(encoding="utf-8")
    assert "Edited in place from the workspace." in text
    assert not (data_dir / "evidence" / "objective.md").exists()


def test_markdown_preview_returns_sanitised_html(client):
    http, _ = client
    response = http.post("/evidence/preview", data={"text": "# Hello\n\nA paragraph."})
    assert response.status_code == 200
    html = response.json()["html"]
    assert "<h1" in html
    assert "Hello" in html
    assert "md-block" in html


def test_evidence_while_a_run_is_in_flight_is_html_not_json_404(client):
    http, _ = client
    progress.registry.start("in-flight-test")
    try:
        response = http.get("/runs/in-flight-test/evidence/E1")
        assert response.status_code == 200
        assert "Still reading" in response.text
        assert '{"detail"' not in response.text
    finally:
        progress.registry.forget("in-flight-test")


def test_evidence_can_be_removed(client, data_dir):
    http, _ = client
    response = http.post("/evidence/late.md/delete")

    assert "Removed" in response.text
    assert not (data_dir / "evidence" / "late.md").exists()
    assert "1 evidence document" in response.text


def test_a_run_can_be_deleted(client, data_dir):
    http, settings = client
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)
    http.post(f"/runs/{thread_id}/stage", follow_redirects=False)
    assert (settings.run_dir(thread_id) / "staged_row.json").exists()

    home = http.get("/").text
    assert f'action="/runs/{thread_id}/delete"' in home
    assert 'aria-label="Delete run' in home

    response = http.post(f"/runs/{thread_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    assert not settings.run_dir(thread_id).exists()
    assert http.get(f"/runs/{thread_id}").status_code == 404
    assert thread_id not in http.get("/").text
    assert (data_dir / "evidence" / "early.md").exists()
    assert (data_dir / "evidence" / "late.md").exists()


def test_run_delete_refuses_reserved_ids(client, tmp_path):
    http, settings = client
    settings.understanding_dir.mkdir(parents=True, exist_ok=True)
    marker = settings.understanding_dir / "keep.txt"
    marker.write_text("stay", encoding="utf-8")

    response = http.post("/runs/understanding/delete", follow_redirects=False)
    assert response.status_code == 400
    assert marker.exists()


def test_traversal_through_the_url_is_refused(client, data_dir):
    http, _ = client
    response = http.post("/evidence/..%2Fobjective.md/delete")

    assert response.status_code in (400, 404)
    assert (data_dir / "objective.md").exists(), "the objective must survive"


def test_the_objective_can_be_edited(client, data_dir):
    http, _ = client
    response = http.post(
        "/objective",
        data={
            "Objective_ID": "OBJ-TEST-01",
            "Title": "A revised objective",
            "Success_Measure": "Two of the thing, not three",
            "Target_Completion": "2026-12-31",
        },
    )
    assert "Objective updated" in response.text
    assert "Two of the thing, not three" in response.text
    assert "Two of the thing" in (data_dir / "objective.md").read_text()


def test_an_empty_evidence_folder_is_explained_not_crashed(client, data_dir):
    http, _ = client
    for name in ("early.md", "late.md"):
        http.post(f"/evidence/{name}/delete")

    body = http.get("/runs/new").text
    assert "Nothing to read yet" in body
    assert "No evidence to read" in body


# --- watching a run happen ---------------------------------------------------


def test_progress_reports_each_stage_in_order(client):
    http, _ = client
    thread_id = start_run(http)

    events = http.get(f"/runs/{thread_id}/progress.json").json()
    assert events["finished"] is True

    stages = [e["stage"] for e in events["events"]]
    assert stages[0] == "load"
    assert stages.count("read_document") == 2, "one event per document"
    for expected in ("reconcile", "assess", "compose", "validate", "review"):
        assert expected in stages, expected
    assert stages[-1] == "done"


def test_progress_events_carry_readable_labels_and_detail(client):
    http, _ = client
    thread_id = start_run(http)
    events = http.get(f"/runs/{thread_id}/progress.json").json()["events"]

    by_stage = {e["stage"]: e for e in events}
    assert by_stage["reconcile"]["label"] == "Reconciling what the documents disagree about"
    assert by_stage["reconcile"]["detail"] == "1 conflict, 0 gaps"
    assert "1 statement," not in by_stage["read_document"]["detail"]
    assert "statement" in by_stage["read_document"]["detail"]
    assert by_stage["validate"]["detail"] == "no problems"


def test_the_page_shows_progress_while_a_run_is_in_flight(client):
    http, _ = client
    redirect = http.post("/runs", data={"quarter": "2026-Q3"}, follow_redirects=False)
    thread_id = redirect.headers["location"].removeprefix("/runs/")

    body = http.get(f"/runs/{thread_id}").text
    assert "Reading the evidence" in body or "Nothing here has been submitted" in body


def test_the_event_stream_is_server_sent_events(client):
    http, _ = client
    thread_id = start_run(http)

    with http.stream("GET", f"/runs/{thread_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payload = "".join(response.iter_text())

    assert payload.startswith("data: ")
    assert '"stage": "done"' in payload


def test_events_for_an_unknown_run_are_a_404(client):
    http, _ = client
    assert http.get("/runs/nope/events").status_code == 404
    assert http.get("/runs/nope/progress.json").status_code == 404


def test_a_failed_run_publishes_a_readable_failure(client, monkeypatch):
    http, _ = client

    class Exploding(ScriptedClient):
        async def structured(self, system, instruction, schema):
            raise RuntimeError("deep failure")

    monkeypatch.setattr(main, "client", lambda: Exploding())
    redirect = http.post("/runs", data={"quarter": "2026-Q3"}, follow_redirects=False)
    thread_id = redirect.headers["location"].removeprefix("/runs/")
    for _ in range(200):
        if http.get(f"/runs/{thread_id}/progress.json").json()["finished"]:
            break

    events = http.get(f"/runs/{thread_id}/progress.json").json()["events"]
    assert events[-1]["stage"] == "failed"
    assert "could not be completed" in events[-1]["label"]


def test_counts_in_progress_detail_are_not_mangled():
    from app.progress import plural

    assert plural(1, "gap") == "1 gap"
    assert plural(0, "gap") == "0 gaps"
    assert plural(3, "statement") == "3 statements"


# --- the evidence rail -------------------------------------------------------




def test_citation_chips_are_real_links_so_they_work_without_javascript(client):
    http, _ = client
    thread_id = start_run(http)
    body = http.get(f"/runs/{thread_id}").text

    assert f'href="/runs/{thread_id}/evidence/' in body
    assert "data-tip=" in body, "the chip previews its statement on hover"


def test_an_unknown_document_fragment_is_a_404(client):
    http, _ = client
    thread_id = start_run(http)
    assert http.get(f"/runs/{thread_id}/source/E99").status_code == 404
