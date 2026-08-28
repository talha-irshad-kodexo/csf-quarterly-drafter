"""The workspace: the index over every run, and the pages that read it.

Driven through the real routes with a scripted model, like test_app.py, and
sharing its fixtures — these are the behaviours that only exist once a
workspace holds more than one draft, so they need the same end-to-end setup.
"""

import pytest

from app import config as app_config
from app import db, main, render
from app.config import Settings

from tests.test_app import (  # noqa: F401 - fixtures used by name
    acknowledge_all_fields,
    client,
    data_dir,
    no_leaked_runtime_key,
    start_run,
)


# --- the index ---------------------------------------------------------------


def test_the_index_lives_in_the_runs_folder(client):
    """Not the project root, and not wherever the process was started."""
    _, settings = client
    assert settings.workspace_db.parent == settings.runs_dir
    assert settings.workspace_db.name == db.FILENAME


def test_a_run_is_indexed_before_it_finishes(client):
    """A run that fails in its first pass is still findable by quarter.

    The staged row is the only place the quarter and objective lived, and it
    is not written until approval — so a run under review showed a row of
    dashes for exactly as long as a director was likely to be looking for it.
    """
    http, _ = client
    thread_id = start_run(http)

    row = db.run(thread_id)
    assert row is not None
    assert row["quarter"] == "2026-Q3"
    assert row["objective_id"] == "OBJ-TEST-01"
    assert row["evidence_count"] == 2
    assert row["model"]


def test_the_runs_list_names_a_run_before_it_is_approved(client):
    http, _ = client
    thread_id = start_run(http)

    body = http.get("/").text
    assert "OBJ-TEST-01" in body
    assert thread_id in body


def test_every_trail_row_is_indexed(client):
    """The JSONL file is the record; the index must not be a subset of it."""
    from app import audit

    http, settings = client
    thread_id = start_run(http)

    on_disk = audit.read(settings.run_dir(thread_id))
    indexed = db.audit_rows(thread_id, limit=10_000)
    assert len(indexed) == len(on_disk)
    assert {row["kind"] for row in indexed} == {row["kind"] for row in on_disk}


def test_an_unwritable_index_does_not_fail_a_run(client, monkeypatch):
    """The trail is worth having and never worth failing a draft over."""
    import sqlite3

    http, settings = client

    def refuse(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_open", refuse)
    thread_id = start_run(http)

    assert http.get(f"/runs/{thread_id}").status_code == 200
    from app import audit

    assert audit.read(settings.run_dir(thread_id)), "the file trail still wrote"


# --- draft history ----------------------------------------------------------


def test_the_draft_is_snapshotted_when_it_is_written(client):
    http, _ = client
    thread_id = start_run(http)

    versions = db.drafts(thread_id)
    assert versions, "the first draft should be recorded"
    assert versions[0]["reason"] == "drafted"
    assert versions[0]["row"]["Objective_ID"] == "OBJ-TEST-01"


def test_an_edit_keeps_the_value_it_replaced(client):
    """What the checkpoint throws away: the row as it stood before the edit."""
    http, _ = client
    thread_id = start_run(http)

    before = db.latest_draft(thread_id)["row"]["Key_Success"]
    http.post(
        f"/runs/{thread_id}/field/Key_Success",
        data={"Key_Success": "A sentence the director wrote instead."},
    )

    versions = db.drafts(thread_id)
    assert len(versions) >= 2
    assert versions[0]["row"]["Key_Success"] == "A sentence the director wrote instead."
    assert versions[1]["row"]["Key_Success"] == before
    assert "Key_Success" in versions[0]["reason"]


def test_approval_is_its_own_version(client):
    http, _ = client
    thread_id = start_run(http)
    acknowledge_all_fields(http, thread_id)
    http.post(f"/runs/{thread_id}/approve", follow_redirects=False)

    assert db.latest_draft(thread_id)["reason"] == "approved for export"
    assert db.run(thread_id)["status"] == "staged"


def test_deleting_a_run_takes_its_index_rows_with_it(client):
    http, _ = client
    thread_id = start_run(http)
    assert db.run(thread_id) is not None

    http.post(f"/runs/{thread_id}/delete", follow_redirects=False)

    assert db.run(thread_id) is None
    assert db.audit_rows(thread_id) == []
    assert db.drafts(thread_id) == []


# --- delete all -------------------------------------------------------------


def test_delete_all_clears_every_run(client):
    http, settings = client
    first = start_run(http)
    second = start_run(http)

    response = http.post("/runs/delete-all", follow_redirects=False)
    assert response.status_code == 303

    body = http.get("/").text
    assert "No runs yet" in body
    assert 'data-action="open-run"' not in body
    for thread_id in (first, second):
        assert not settings.run_dir(thread_id).exists()
        assert db.run(thread_id) is None


def test_delete_all_leaves_the_evidence_alone(client, data_dir):
    """The runs folder is the tool's state. The data folder is the director's."""
    http, _ = client
    start_run(http)
    http.post("/runs/delete-all", follow_redirects=False)

    assert (data_dir / "objective.md").exists()
    assert (data_dir / "prior_update.md").exists()
    assert sorted(p.name for p in (data_dir / "evidence").glob("*.md")) == [
        "early.md",
        "late.md",
    ]


def test_delete_all_on_an_empty_workspace_is_not_an_error(client):
    http, _ = client
    assert http.post("/runs/delete-all", follow_redirects=False).status_code == 303


def test_the_runs_page_offers_delete_all_beside_new_run(client):
    http, _ = client
    start_run(http)

    body = http.get("/").text
    assert 'action="/runs/delete-all"' in body
    assert 'href="/runs/new"' in body
    assert "delete-all-dialog" in body, "it must be confirmed before it happens"


# --- the workspace audit page -----------------------------------------------


def test_the_audit_page_is_reachable_before_any_run_exists(client):
    """It is in the sidebar from the first page load, so it must render empty."""
    http, _ = client
    response = http.get("/audit")
    assert response.status_code == 200
    assert "No runs recorded" in response.text


def test_the_sidebar_offers_the_audit_trail_without_a_run(client):
    http, _ = client
    body = http.get("/").text
    assert 'href="/audit"' in body


def test_the_audit_page_totals_every_run(client):
    http, _ = client
    start_run(http)
    start_run(http)

    body = http.get("/audit").text
    assert "Runs recorded" in body
    assert "Model calls" in body
    assert "Draft history" in body


def test_the_audit_page_can_be_narrowed_to_one_run(client):
    http, _ = client
    kept = start_run(http)
    other = start_run(http)

    body = http.get(f"/audit?run={kept}").text
    # Both ids appear in the runs table on the overview tab; the ledgers below
    # are what the filter narrows, so look at a row only a ledger produces.
    assert f'href="/runs/{kept}/audit">{kept}' in body
    assert f'href="/runs/{other}/audit">{other}' not in body


def test_a_trail_written_before_the_index_is_imported(client):
    """Runs drafted by an earlier build must not vanish from the audit page."""
    http, _ = client
    thread_id = start_run(http)

    # Whatever the run wrote to the index, thrown away — the JSONL trail on
    # disk is untouched, which is the state an older run directory is in.
    db.forget_all()
    main._reconciled.clear()

    assert db.audit_rows(thread_id) == []
    body = http.get("/audit").text

    assert db.has_audit(thread_id), "the trail on disk should have been imported"
    assert thread_id in body


def test_importing_a_trail_twice_does_not_double_it(client):
    http, _ = client
    thread_id = start_run(http)

    before = len(db.audit_rows(thread_id, limit=10_000))
    main._reconciled.clear()
    http.get("/audit")

    assert len(db.audit_rows(thread_id, limit=10_000)) == before


# --- the per-run audit page -------------------------------------------------


def test_the_run_audit_page_is_tabbed(client):
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}/audit").text
    for tab in ("overview", "events", "calls", "edits", "findings", "drafts"):
        assert f'data-tab="{tab}"' in body
        assert f'data-panel="{tab}"' in body


def test_every_panel_is_rendered_not_fetched(client):
    """Tabs are a layout, not a loading state.

    Every ledger is in the HTML, so the page reads whole without JavaScript
    and a browser's find-in-page still finds what is on it.
    """
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}/audit").text
    assert "Model calls" in body
    assert "Draft history" in body
    assert "Pipeline events" in body


def test_the_run_audit_page_lists_what_the_run_read(client):
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}/audit").text
    assert "early.md" in body and "late.md" in body


# --- the demo pack ----------------------------------------------------------


@pytest.fixture
def empty_workspace(tmp_path, monkeypatch):
    """A data folder with nothing in it, which is what a new install has."""
    data = tmp_path / "data"
    (data / "evidence").mkdir(parents=True)
    settings = Settings(
        data_dir=data,
        runs_dir=tmp_path / "runs",
        quarter="2026-Q3",
        anthropic_api_key="not-used",
    )
    monkeypatch.setattr(main, "settings", settings)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as http:
        yield http, settings


def test_the_demo_pack_fills_an_empty_workspace(empty_workspace):
    http, settings = empty_workspace
    http.post("/evidence/demo", follow_redirects=False)

    assert settings.objective_file.exists()
    assert settings.prior_update_file.exists()
    assert len(list(settings.evidence_dir.glob("*.md"))) >= 3


def test_loading_the_demo_pack_twice_adds_nothing(empty_workspace):
    http, settings = empty_workspace
    http.post("/evidence/demo")
    after_first = sorted(p.name for p in settings.evidence_dir.glob("*.md"))

    body = http.post("/evidence/demo").text

    assert sorted(p.name for p in settings.evidence_dir.glob("*.md")) == after_first
    assert "already loaded" in body


def test_the_demo_pack_never_overwrites_an_edited_objective(empty_workspace):
    """A mis-click must not cost the director their success measure."""
    http, settings = empty_workspace
    settings.objective_file.write_text("# Mine\n\nHands off.\n", encoding="utf-8")

    http.post("/evidence/demo")

    assert "Hands off." in settings.objective_file.read_text(encoding="utf-8")


def test_an_empty_workspace_offers_the_pack_on_the_page_it_can_reach(empty_workspace):
    """Without an objective record the workspace cannot render at all.

    That page used to offer a retry and two links to itself. The way out of an
    empty data folder was a text editor, which is the one thing this tool is
    supposed to make unnecessary.
    """
    http, settings = empty_workspace
    body = http.get("/runs/new").text

    assert 'action="/evidence/demo"' in body
    assert "Load the demo pack" in body

    http.post("/evidence/demo")
    assert settings.objective_file.exists()
    assert "Drop the quarter's files here" in http.get("/runs/new").text


def test_the_dropzone_offers_the_pack_instead_of_a_folder_path(client, data_dir):
    http, _ = client
    body = http.get("/runs/new").text

    assert "Load the demo pack instead" in body
    assert str(data_dir) not in body, "the storage path is not the director's business"


# --- the model dropdown -----------------------------------------------------


def test_settings_offers_models_rather_than_a_text_box(client):
    http, _ = client
    body = http.get("/").text

    assert '<select class="selectbox mono" id="set-model" name="model">' in body
    assert 'value="claude-opus-5"' in body
    assert 'value="claude-sonnet-5"' in body


def test_the_configured_model_is_always_in_the_list(client):
    """A model set in .env that predates the list must still be selectable."""
    options = app_config.model_options("claude-something-unreleased")
    assert options[0][0] == "claude-something-unreleased"
    assert len(options) == len(app_config.MODEL_CHOICES) + 1


def test_both_models_can_be_chosen(client):
    http, _ = client
    http.post(
        "/settings/key",
        data={"api_key": "", "model": "claude-sonnet-5", "reader_model": "claude-haiku-4-5"},
    )

    assert app_config.runtime_model() == "claude-sonnet-5"
    assert app_config.runtime_reader_model() == "claude-haiku-4-5"


def test_a_model_that_is_not_offered_is_refused(client):
    """The dropdown is the allowlist. A crafted post must not get past it."""
    http, _ = client
    http.post("/settings/key", data={"api_key": "", "model": "gpt-4"})

    assert app_config.runtime_model() == ""


# --- rendering evidence -----------------------------------------------------


def test_a_table_renders_as_a_table(client, data_dir):
    http, _ = client
    (data_dir / "evidence" / "table.md").write_text(
        "# Report — 2 August 2026\n\n"
        "| Milestone | Status |\n|---|---|\n| Kenya | signed |\n| Ethiopia | stalled |\n",
        encoding="utf-8",
    )
    thread_id = start_run(http)

    doc_id = _doc_for(http, thread_id, "table.md")
    body = http.get(f"/runs/{thread_id}/evidence/{doc_id}").text

    assert "<table>" in body
    assert "<td>signed</td>" in body
    assert "| Milestone | Status |" in body, "the source is still one tab away"


def test_the_document_and_the_source_are_both_on_the_page(client):
    http, _ = client
    thread_id = start_run(http)

    body = http.get(f"/runs/{thread_id}/evidence/E1").text
    assert 'data-panel="document"' in body
    assert 'data-panel="source"' in body
    assert "codeline" in body, "the numbered source is what a citation is checked against"


def test_markup_in_an_uploaded_document_cannot_execute(client, data_dir):
    """Evidence arrives through a file picker, and markdown passes HTML through."""
    http, _ = client
    (data_dir / "evidence" / "nasty.md").write_text(
        "# Note — 3 August 2026\n\nA line.\n\n<script>alert(1)</script>\n",
        encoding="utf-8",
    )
    thread_id = start_run(http)

    doc_id = _doc_for(http, thread_id, "nasty.md")
    body = http.get(f"/runs/{thread_id}/evidence/{doc_id}").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body, "it is shown as text, not silently dropped"


def _doc_for(http, thread_id: str, filename: str) -> str:
    """The doc id the run gave a file. Ids are positional, so ask."""
    for doc in http.get(f"/runs/{thread_id}/state.json").json()["claims"]:
        pass  # claims carry doc ids but not filenames; walk the documents instead
    for row in db.documents(thread_id):
        if row["filename"] == filename:
            return row["doc_id"]
    raise AssertionError(f"{filename} was not read by {thread_id}")


# --- the renderer, on its own ----------------------------------------------


def test_blocks_keep_the_lines_they_came_from():
    """A citation is a line range, so a rendered block has to carry one."""
    blocks = render.blocks("# Title\n\nFirst para.\n\nSecond para.\n")

    assert [(b["line_start"], b["line_end"]) for b in blocks] == [(1, 1), (3, 3), (5, 5)]
    assert "<h1>Title</h1>" in blocks[0]["html"]


def test_a_cited_line_marks_the_block_it_falls_in():
    blocks = render.blocks("Alpha.\n\nBravo\nline two.\n", cited_lines={4})

    assert [b["cited"] for b in blocks] == [False, True]


def test_a_javascript_url_is_dropped_from_a_link():
    html = render.to_html("[click](javascript:alert(1))")
    assert "javascript:" not in html


def test_ordinary_markdown_survives():
    html = render.to_html("**bold** and *italic* and `code`")
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html


def test_an_event_handler_attribute_does_not_survive():
    assert "onerror" not in render.sanitise('<span onerror="x()">hi</span>')


def test_an_empty_document_renders_to_nothing():
    assert render.blocks("") == []
    assert render.blocks("\n\n   \n") == []
