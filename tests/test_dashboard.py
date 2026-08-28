"""The director snapshot: this draft against last quarter, visually only."""

from app import dashboard, vocab
from app.schema import DraftRow, FieldProposal


def _proposal(value, **kwargs) -> FieldProposal:
    return FieldProposal(value=value, **kwargs)


def _row(**overrides) -> DraftRow:
    base = dict(
        Objective_ID="OBJ-TEST-01",
        Quarter="2026-Q3",
        Traffic_Light=_proposal("Amber"),
        Progress_Percent=_proposal(30),
        Key_Success=_proposal("One thing landed."),
        Key_Challenge=_proposal("Another did not."),
        Support_Needed=_proposal(None, needs_director_input=True),
        Support_From=_proposal(["Other"]),
    )
    base.update(overrides)
    return DraftRow(**base)


def _ctx(**overrides) -> dict:
    ctx = {
        "row": _row(),
        "prior_update": {
            "Quarter": "2026-Q2",
            "Traffic_Light": "Green",
            "Progress_Percent": "45",
            "Key_Success": "It was all going well.",
        },
        "conflicts": [{"severity": "medium", "acknowledged": False}],
        "gaps": [],
        "fields": [],
        "acknowledged_count": 0,
        "days_remaining": 34,
        "objective": {
            "Objective_ID": "OBJ-TEST-01",
            "Title": "Do a difficult thing",
            "Success_Measure": "Three of the thing",
        },
        "docs": {"E1": object(), "E2": object()},
    }
    ctx.update(overrides)
    return ctx


def test_snapshot_contrasts_prior_green_with_this_amber():
    snap = dashboard.from_review(_ctx())
    assert snap["prior_tl"] == "Green"
    assert snap["current_tl"] == "Amber"
    assert snap["prior_pg"] == 45
    assert snap["current_pg"] == 30
    lights = {row["field"]: row for row in snap["compare"]}
    assert lights["Traffic_Light"]["movement"] == "changed"
    assert lights["Progress_Percent"]["movement"] == "changed"


def test_snapshot_does_not_submit_a_trend_value():
    """Improved / Same / Deteriorated / New belong to Power BI, not this row."""
    snap = dashboard.from_review(_ctx())
    assert "Trend_vs_Prior_Quarter" not in snap
    assert "Power BI" in snap["trend_note"]
    for row in snap["compare"]:
        assert row["movement"] in {"changed", "same", "new", "none"}


def test_traffic_light_tips_use_the_closed_anchors():
    snap = dashboard.from_review(_ctx())
    assert vocab.TRAFFIC_LIGHTS["Amber"] in snap["tl_tip"]
    assert vocab.TRAFFIC_LIGHTS["Green"] in snap["prior_tl_tip"]
    assert {item["value"] for item in snap["legend"]} == set(vocab.TRAFFIC_LIGHTS)


def test_acknowledge_count_is_not_treated_as_a_comparison():
    snap = dashboard.from_review(_ctx())
    assert "human gate" in snap["review_tip"]
    assert "not a comparison" in snap["review_tip"]


def test_empty_support_from_is_valid_not_other():
    row = _row(Support_From=_proposal([]))
    snap = dashboard.from_review(_ctx(row=row, prior_update={
        "Quarter": "2026-Q2",
        "Support_From": "—",
    }))
    support = next(r for r in snap["compare"] if r["field"] == "Support_From")
    assert support["current"] == []
    assert support["prior"] == []


def test_from_index_marks_a_running_draft_as_pending():
    snap = dashboard.from_index(
        {"thread_id": "abc", "status": "running", "quarter": "2026-Q3"},
        pending=True,
    )
    assert snap["pending"] is True
    assert snap["quarter"] == "2026-Q3"
    assert snap["current_tl"] is None


def test_from_workspace_aggregates_without_inventing_a_trend():
    snap = dashboard.from_workspace(
        [
            {"status": "running", "traffic_light": "", "progress_percent": None},
            {"status": "review", "traffic_light": "Amber", "progress_percent": 30},
            {"status": "staged", "traffic_light": "Green", "progress_percent": 50},
        ],
        {"llm_calls": 4, "llm_input_tokens": 100, "llm_output_tokens": 20, "edits": 1},
    )
    assert snap["run_count"] == 3
    assert snap["running"] == 1
    assert snap["review"] == 1
    assert snap["approved"] == 1
    assert snap["mean_pg"] == 40
    assert "Trend_vs_Prior_Quarter" not in snap
    lights = {item["value"]: item["count"] for item in snap["lights"]}
    assert lights["Amber"] == 1
    assert lights["Green"] == 1
