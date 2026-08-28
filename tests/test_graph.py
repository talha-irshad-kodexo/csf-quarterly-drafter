"""Graph behaviour, with a scripted model. No API key needed.

The interesting cases are the ones where LangGraph's semantics could bite:
the graph must actually stop at the interrupt, the corrections node must not
run twice when the review node restarts on resume, and a repairable validation
failure must go round again while a blocking one must not.
"""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.config import Settings
from app.graph.build import build_graph
from app.graph.state import initial_state
from app.schema import NarrativeField, ValidationIssue
from app.evidence import load_document
from tests.scripted import (
    ScriptedClient,
    default_assessment,
    default_narratives,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs", quarter="2026-Q3")


@pytest.fixture
def docs(tmp_path):
    folder = tmp_path / "evidence"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("# Email — 1 May 2026\n\nAn early account.", encoding="utf-8")
    (folder / "b.md").write_text("# Teams — 1 August 2026\n\nA later account.", encoding="utf-8")
    return [load_document(folder / "a.md", "E1"), load_document(folder / "b.md", "E2")]


@pytest.fixture
def inputs(docs):
    return initial_state(
        quarter="2026-Q3",
        objective={"Objective_ID": "OBJ-TEST-01", "Success_Measure": "Do the thing"},
        prior_update={"Traffic_Light": "Green", "Progress_Percent": "45"},
        docs=docs,
    )


def config(thread_id="t1"):
    return {"configurable": {"thread_id": thread_id}}


async def run_to_review(client, settings, inputs, thread="t1"):
    graph = build_graph(client, settings, checkpointer=InMemorySaver())
    result = await graph.ainvoke(inputs, config=config(thread), durability="sync")
    return graph, result


# --- reading -----------------------------------------------------------------


async def test_every_document_is_read_and_claims_are_scoped_to_it(settings, inputs):
    client = ScriptedClient()
    _, result = await run_to_review(client, settings, inputs)

    assert client.calls.count("read") == 2, "one reading pass per document"
    claim_ids = {c.claim_id for c in result["claims"]}
    assert claim_ids == {"E1.1", "E2.1"}, "claim ids carry their source document"


async def test_uncited_reading_produces_no_claims(settings, inputs):
    client = ScriptedClient(reading=[{"type": "text", "text": "Uncited commentary."}])
    _, result = await run_to_review(client, settings, inputs)
    assert result["claims"] == []


# --- the interrupt -----------------------------------------------------------


async def test_graph_stops_at_review_and_stages_nothing(settings, inputs):
    _, result = await run_to_review(ScriptedClient(), settings, inputs)

    assert "__interrupt__" in result, "the graph must pause for the director"
    assert result.get("staged_path") is None
    assert not list(settings.runs_dir.glob("**/staged_row.json"))


async def test_interrupt_payload_carries_the_row_for_review(settings, inputs):
    _, result = await run_to_review(ScriptedClient(), settings, inputs)

    payload = result["__interrupt__"][0].value
    assert payload["reason"] == "director_review"
    assert payload["row"]["Traffic_Light"]["value"] == "Amber"
    assert payload["row"]["submitted"] is False


async def test_prior_quarter_value_is_not_inherited(settings, inputs):
    """The scripted assessment says Amber while the prior quarter said Green."""
    _, result = await run_to_review(ScriptedClient(), settings, inputs)
    assert result["row"].Traffic_Light.value == "Amber"
    assert inputs["prior_update"]["Traffic_Light"] == "Green"


# --- resume ------------------------------------------------------------------


async def test_resume_applies_corrections_and_stages(settings, inputs):
    graph, _ = await run_to_review(ScriptedClient(), settings, inputs)

    correction = {
        "field": "Key_Success",
        "proposed_value": "One thing landed.",
        "director_value": "One thing landed, at half the expected scope.",
        "claim_ids_shown": ["E1.1"],
        "timestamp": "2026-08-27T10:00:00Z",
    }
    final = await graph.ainvoke(
        Command(resume={"corrections": [correction]}),
        config=config(),
        durability="sync",
    )

    assert final["row"].Key_Success.value == "One thing landed, at half the expected scope."
    assert final["row"].Key_Success.edited_by_director is True
    assert final["row"].Traffic_Light.edited_by_director is False

    staged = json.loads((settings.run_dir("t1") / "staged_row.json").read_text())
    assert staged["submitted"] is False
    assert staged["Source"] == "Substrate-Drafted"
    assert "Trend_vs_Prior_Quarter" not in staged


async def test_corrections_are_logged_once_not_twice(settings, inputs):
    """The review node restarts from the top on resume.

    If the correction log were written there rather than in a later node, every
    resume would duplicate it. This is the trap that test exists to catch.
    """
    graph, _ = await run_to_review(ScriptedClient(), settings, inputs)

    correction = {
        "field": "Key_Challenge",
        "proposed_value": "Another did not.",
        "director_value": "Reworded.",
        "claim_ids_shown": [],
        "timestamp": "2026-08-27T10:00:00Z",
    }
    await graph.ainvoke(
        Command(resume={"corrections": [correction]}), config=config(), durability="sync"
    )

    lines = (settings.run_dir("t1") / "corrections.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["director_value"] == "Reworded."


async def test_resume_with_no_corrections_still_stages(settings, inputs):
    graph, _ = await run_to_review(ScriptedClient(), settings, inputs)
    final = await graph.ainvoke(
        Command(resume={"corrections": []}), config=config(), durability="sync"
    )
    assert final["staged_path"]
    assert not (settings.run_dir("t1") / "corrections.jsonl").exists()


async def test_a_correction_clears_the_needs_input_flag(settings, inputs):
    graph, result = await run_to_review(ScriptedClient(), settings, inputs)
    assert result["row"].Support_Needed.needs_director_input is True

    final = await graph.ainvoke(
        Command(
            resume={
                "corrections": [
                    {
                        "field": "Support_Needed",
                        "proposed_value": None,
                        "director_value": "A drafter for the annex.",
                        "claim_ids_shown": [],
                        "timestamp": "2026-08-27T10:00:00Z",
                    }
                ]
            }
        ),
        config=config(),
        durability="sync",
    )
    assert final["row"].Support_Needed.needs_director_input is False
    assert final["row"].Support_Needed.value == "A drafter for the annex."


async def test_each_narrative_field_is_asked_for_on_its_own(settings, inputs):
    """One request per field, not one request for the set.

    Asked for all four at once the model returned some of them and dropped the
    rest — an empty Key_Challenge, no support reasons — and a dropped field
    reaches the director as a blank box that validation can only flag, not
    fill. Three requests over the same brief, run concurrently.
    """
    client = ScriptedClient()

    await run_to_review(client, settings, inputs)

    assert "NarrativeSet" not in client.calls, "the whole set was asked for at once"
    for slice_name in ("SuccessNarrative", "ChallengeNarrative", "SupportAsk"):
        assert client.calls.count(slice_name) == 1, slice_name
    assert client.compose_count == 1, "three calls are still one compose pass"


# --- validation and repair ---------------------------------------------------


async def test_an_overlong_narrative_is_noted_not_retried(settings, inputs):
    """The column width is advice. It does not cost a retry."""
    too_long = default_narratives(key_success=NarrativeField(text="x" * 260, claim_ids=["E1.1"]))
    client = ScriptedClient(narratives=[too_long])

    _, result = await run_to_review(client, settings, inputs)

    assert client.compose_count == 1, "no retry for a column-width note"
    assert result["repair_attempts"] == 0
    assert [i.severity for i in result["issues"]] == ["advice"]


async def test_repair_gives_up_rather_than_looping(settings, inputs):
    always_bad = default_narratives(
        key_success=NarrativeField(text="A value.", claim_ids=["E9.9"])
    )
    client = ScriptedClient(narratives=[always_bad])

    _, result = await run_to_review(client, settings, inputs)

    assert client.compose_count == settings.max_repair_attempts + 1
    assert result["issues"], "the unresolved problem goes to the director"
    assert "__interrupt__" in result


async def test_the_director_sees_unresolved_issues(settings, inputs):
    always_bad = default_narratives(
        key_success=NarrativeField(text="A value.", claim_ids=["E9.9"])
    )
    _, result = await run_to_review(ScriptedClient(narratives=[always_bad]), settings, inputs)

    payload = result["__interrupt__"][0].value
    assert any(i["field"] == "Key_Success" for i in payload["issues"])


async def test_hallucinated_claim_ids_are_caught(settings, inputs):
    client = ScriptedClient(
        assessment=default_assessment(traffic_light_claim_ids=["E9.9"]),
    )
    _, result = await run_to_review(client, settings, inputs)
    assert any("E9.9" in i.message for i in result["issues"])


# --- repair goes back to the pass that can fix it ----------------------------
# A bad traffic-light rationale sent back to compose can never be fixed:
# compose does not produce it. The retries get spent rewriting the wrong text
# and the run ends invalid.


async def test_a_bad_status_rationale_is_repaired_at_assess(settings, inputs):
    bad = default_assessment(traffic_light_rationale="x" * 400)
    good = default_assessment()
    client = ScriptedClient(assessment=[bad, good])

    _, result = await run_to_review(client, settings, inputs)

    assert client.assess_count == 2, "assess must run again, not just compose"
    assert result["issues"] == []


async def test_a_bad_narrative_is_repaired_at_compose_without_reassessing(settings, inputs):
    unevidenced = default_narratives(
        key_success=NarrativeField(text="A value with no support.", claim_ids=[])
    )
    client = ScriptedClient(narratives=[unevidenced, default_narratives()])

    _, result = await run_to_review(client, settings, inputs)

    assert client.compose_count == 2
    assert client.assess_count == 1, "no need to reassess over a narrative"
    assert result["issues"] == []


async def test_a_long_reasoning_is_left_alone(settings, inputs):
    client = ScriptedClient(assessment=[default_assessment(traffic_light_reasoning="x" * 9000)])

    _, result = await run_to_review(client, settings, inputs)

    assert result["issues"] == [], "reasoning has no limit to breach"
    assert client.assess_count == 1, "and costs no retry"


async def test_a_pseudo_citation_is_repaired(settings, inputs):
    bad = default_assessment(
        traffic_light_rationale="Down from last quarter [context, not evidence]."
    )
    client = ScriptedClient(assessment=[bad, default_assessment()])

    _, result = await run_to_review(client, settings, inputs)
    assert result["issues"] == []


async def test_repair_still_gives_up_rather_than_looping(settings, inputs):
    always_bad = default_assessment(traffic_light_rationale="x" * 400)
    client = ScriptedClient(assessment=[always_bad])

    _, result = await run_to_review(client, settings, inputs)

    assert client.assess_count == settings.max_repair_attempts + 1
    assert result["issues"], "an unfixable problem goes to the director"


async def test_support_from_carries_the_evidence_behind_support_needed(settings, inputs):
    from app.schema import NarrativeField

    narratives = default_narratives(
        support_needed=NarrativeField(text="Finance to restore the budget.", claim_ids=["E2.1"]),
        support_from=["Finance"],
    )
    _, result = await run_to_review(ScriptedClient(narratives=narratives), settings, inputs)

    row = result["row"]
    assert row.Support_From.claim_ids == ["E2.1"]
    assert row.Support_From.confidence != "low", "it is not unevidenced"


async def test_empty_support_from_defaults_to_other(settings, inputs):
    """Agent compose must leave at least one Support_From tick on the draft."""
    narratives = default_narratives(support_from=[])
    _, result = await run_to_review(ScriptedClient(narratives=narratives), settings, inputs)

    assert result["row"].Support_From.value == ["Other"]


async def test_empty_support_from_with_support_needed_inherits_claims(settings, inputs):
    from app.schema import NarrativeField

    narratives = default_narratives(
        support_needed=NarrativeField(text="Help from an unnamed team.", claim_ids=["E2.1"]),
        support_from=[],
    )
    _, result = await run_to_review(ScriptedClient(narratives=narratives), settings, inputs)

    row = result["row"]
    assert row.Support_From.value == ["Other"]
    assert row.Support_From.claim_ids == ["E2.1"]
    assert row.Support_From.confidence != "low"


async def test_a_conflict_that_supersedes_nothing_becomes_a_gap(settings, inputs):
    """The distinction is guaranteed here, not left to the model to observe."""
    from app.schema import Conflict, Reconciliation

    reconciliation = Reconciliation(
        conflicts=[
            Conflict(
                topic="Something nobody ever settled",
                winning_claim_id="E1.1",
                superseded_claim_ids=[],
                rule_applied="other",
                note="Nothing later addresses this.",
            )
        ],
        reconciled_position="As stated [E1.1].",
    )
    _, result = await run_to_review(
        ScriptedClient(conflicts=reconciliation), settings, inputs
    )

    assert result["conflicts"] == [], "not a conflict: nothing was overturned"
    assert [g.topic for g in result["gaps"]] == ["Something nobody ever settled"]
    assert result["gaps"][0].raised_by_claim_ids == ["E1.1"]


# --- run-to-run consistency --------------------------------------------------


def test_claims_are_ordered_by_document_regardless_of_read_order():
    """Parallel readers finish in any order; the prompts must not.

    Claim ids are stable, but the accumulated list order is not — and every
    downstream prompt is built by walking that list. Unsorted, two runs over
    identical evidence send differently-ordered prompts and get different
    drafts for no reason a director could see.
    """
    from app.graph.nodes import ordered_claims
    from app.schema import Claim

    def claim(cid):
        return Claim(claim_id=cid, doc_id=cid.split(".")[0], text="t", citations=[])

    shuffled = [claim(c) for c in ("E10.1", "E2.2", "E1.1", "E9.1", "E2.1")]
    assert [c.claim_id for c in ordered_claims({"claims": shuffled})] == [
        "E1.1",
        "E2.1",
        "E2.2",
        "E9.1",
        "E10.1",
    ], "E10 must sort after E9, not between E1 and E2"


def test_formatted_claims_do_not_depend_on_completion_order():
    from app.graph.nodes import format_claims
    from app.schema import Claim

    def claim(cid, text):
        return Claim(claim_id=cid, doc_id=cid.split(".")[0], text=text, citations=[])

    one = [claim("E1.1", "first"), claim("E2.1", "second")]
    other = [claim("E2.1", "second"), claim("E1.1", "first")]
    assert format_claims({"claims": one, "docs": []}) == format_claims(
        {"claims": other, "docs": []}
    )


def test_each_model_gets_the_one_steering_control_it_accepts():
    """effort and temperature are mutually exclusive, decided by the model id.

    Claude Opus 4.7 removed temperature/top_p/top_k — sending one there is a
    400. Older models reject `output_config.effort` the same way. Sending both
    to hedge fails the request outright rather than degrading, so the choice
    cannot be a setting; it follows from the model. The boundary is at 4.7:
    Sonnet 4.6 still takes a temperature.
    """
    from app.config import Settings
    from app.llm import _controls

    settings = Settings(anthropic_api_key="x", effort="high", temperature=0.0)

    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-7"):
        assert _controls(model, settings) == {"output_config": {"effort": "high"}}, model

    for model in ("claude-sonnet-4-5", "claude-sonnet-4-6", "claude-haiku-4-5"):
        assert _controls(model, settings) == {"temperature": 0.0}, model

    unset = Settings(anthropic_api_key="x", temperature=None)
    assert _controls("claude-sonnet-4-5", unset) == {}, "no temperature, none sent"


def test_no_hidden_top_p_rides_along_with_the_temperature():
    """Claude 4.6 rejects temperature and top_p together, with a 400.

    We never set top_p — but some client libraries inject a default one behind
    the scenes, which turns a working request into a failing one on a version
    bump with nothing in this repo changing. Assert on the payload actually
    built, not on our own call site.
    """
    from app.config import Settings
    from app.llm import AnthropicClient

    client = AnthropicClient(Settings(anthropic_api_key="sk-ant-test", temperature=0.0))
    payload = client._reasoner._get_request_payload([{"role": "user", "content": "hi"}])

    assert payload["temperature"] == 0.0
    assert "top_p" not in payload and "top_k" not in payload, payload.keys()


def test_the_model_thinks_hard_enough_to_fill_the_schema():
    """effort decides how much the model explores before answering.

    This was medium for one release, to make runs faster. Compose started
    returning objects with fields missing — an empty Key_Challenge and no
    support reasons — because the schema had grown at the same moment the
    deliberation shrank. Latency is bought back by splitting compose into
    concurrent calls, which does not cost accuracy; this does.
    """
    from app.config import Settings

    assert Settings(anthropic_api_key="x").effort == "high"


def test_structured_output_is_never_capped_by_us():
    """A structured object must not be cut off mid-field.

    max_tokens is required by the API, so it cannot literally be absent — left
    unset, the client requests the model's full output ceiling instead of a
    number we invented. Thinking counts against that budget on Opus 5 and
    nothing here handles a truncated response, so a low cap only ever bought a
    parse failure.
    """
    from app.config import Settings
    from app.llm import AnthropicClient

    # Whatever the configured model's own ceiling is — 128k on the Opus 5 and
    # Sonnet 5 line, 64k on Sonnet 4.5. The point is that the number comes
    # from the model rather than from us.
    client = AnthropicClient(Settings(anthropic_api_key="sk-ant-test"))
    assert Settings(anthropic_api_key="x").max_tokens is None
    assert client._reasoner.max_tokens >= 64_000
    assert client._reader.max_tokens == client._reasoner.max_tokens

    newer = AnthropicClient(
        Settings(anthropic_api_key="sk-ant-test", model="claude-opus-5")
    )
    assert newer._reasoner.max_tokens == 128_000

    # An explicit cap is still honoured for anyone who wants to bound spend.
    capped = AnthropicClient(Settings(anthropic_api_key="sk-ant-test", max_tokens=4096))
    assert capped._reasoner.max_tokens == 4096


def test_the_timeout_matches_the_output_budget():
    """A generation allowed to be long is allowed to be slow.

    Uncapping the output without moving the timeout would swap a truncated
    object for a request that times out and retries three times.
    """
    from app.config import Settings
    from app.llm import AnthropicClient

    client = AnthropicClient(Settings(anthropic_api_key="sk-ant-test"))
    assert client._reasoner.default_request_timeout >= 600


def test_effort_reaches_both_models():
    """On a model that takes effort. The default one does not — see
    test_each_model_gets_the_one_steering_control_it_accepts."""
    from app.config import Settings
    from app.llm import AnthropicClient

    client = AnthropicClient(
        Settings(
            anthropic_api_key="sk-ant-test",
            effort="low",
            model="claude-opus-5",
            reader_model="claude-sonnet-5",
        )
    )
    assert client._reasoner.output_config == {"effort": "low"}
    assert client._reader.output_config == {"effort": "low"}


def test_a_prompt_missing_a_variable_names_the_likely_cause():
    """Prompts are read from disk; the code that fills them is not.

    A prompt that gains a variable in the same change as the node supplying it
    fails mid-run on a server that has not been restarted, with a KeyError that
    points at string.Template and explains nothing.
    """
    from app.graph.nodes import prompt

    with pytest.raises(KeyError) as caught:
        prompt("compose")

    assert "restart" in str(caught.value)
