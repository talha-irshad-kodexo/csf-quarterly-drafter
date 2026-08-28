"""A scripted LLMClient, so the graph can be tested without an API key.

It answers by schema rather than by call order, which keeps the tests readable
and stops them breaking every time a node is reordered.
"""

from __future__ import annotations

from typing import Any

from app.audit import STAGE_BY_SCHEMA
from app.schema import (
    ChallengeNarrative,
    Conflict,
    NarrativeSet,
    Reconciliation,
    StatusAssessment,
    SuccessNarrative,
    SupportAsk,
)

# compose asks for one slice per call. The scripted set still describes a
# whole round, so a round is served in pieces and only counted once.
COMPOSE_SLICES = (SuccessNarrative, ChallengeNarrative, SupportAsk)


class ScriptedClient:
    """Returns whatever it was handed. Records every call for assertions."""

    def __init__(
        self,
        *,
        reading: Any = None,
        conflicts: Reconciliation | None = None,
        assessment: StatusAssessment | list[StatusAssessment] | None = None,
        narratives: NarrativeSet | list[NarrativeSet] | None = None,
    ) -> None:
        self.reading = reading if reading is not None else default_reading()
        self.conflicts = conflicts or default_conflicts()
        assessment = assessment if assessment is not None else default_assessment()
        self._assessments = assessment if isinstance(assessment, list) else [assessment]
        narratives = narratives if narratives is not None else default_narratives()
        self._narratives = narratives if isinstance(narratives, list) else [narratives]
        self.calls: list[str] = []
        self.compose_count = 0
        self._slices_served = 0
        self.assess_count = 0
        self._recorder = None

    def recording_to(self, recorder):
        """Same seam AnthropicClient offers, so the audit trail is exercised.

        Without it the ledger's model-call rows would only ever be written by
        code no test runs. The token counts are absent because a stub has none;
        everything else about the row is real.
        """
        self._recorder = recorder
        return self

    def _record(self, stage: str) -> None:
        if self._recorder is None:
            return
        self._recorder(
            {
                "stage": stage,
                "prompt_name": stage,
                "model": "scripted",
                "latency_ms": 0,
                "input_tokens": None,
                "output_tokens": None,
                "stop_reason": None,
            }
        )

    async def read_with_citations(
        self, system: str, instruction: str, documents: list[dict[str, Any]]
    ) -> Any:
        self.calls.append("read")
        self._record("read_document")
        return self.reading

    async def structured(
        self, system: str, instruction: str, schema: type, fast: bool = False
    ) -> Any:
        self.calls.append(schema.__name__)
        self._record(STAGE_BY_SCHEMA.get(schema.__name__, schema.__name__.lower()))
        if schema is Reconciliation:
            return self.conflicts
        if schema is StatusAssessment:
            index = min(self.assess_count, len(self._assessments) - 1)
            self.assess_count += 1
            return self._assessments[index]
        if schema is NarrativeSet or schema in COMPOSE_SLICES:
            index = min(self.compose_count, len(self._narratives) - 1)
            whole = self._narratives[index]
            if schema is NarrativeSet:
                self.compose_count += 1
                return whole
            self._slices_served += 1
            if self._slices_served == len(COMPOSE_SLICES):
                self._slices_served = 0
                self.compose_count += 1
            return schema.model_validate(whole.model_dump())
        raise AssertionError(f"no scripted response for {schema.__name__}")


def cited(text: str, start: int = 0, end: int = 1, doc_index: int = 0) -> dict:
    return {
        "type": "text",
        "text": text,
        "citations": [
            {
                "type": "content_block_location",
                "cited_text": "source text",
                "document_index": doc_index,
                "start_block_index": start,
                "end_block_index": end,
            }
        ],
    }


def default_reading() -> list[dict]:
    return [cited("Something in this document happened.")]


def default_conflicts() -> Reconciliation:
    return Reconciliation(
        conflicts=[
            Conflict(
                topic="A disagreement",
                winning_claim_id="E2.1",
                superseded_claim_ids=["E1.1"],
                rule_applied="later_supersedes_earlier",
                note="The later account stands.",
                severity="medium",
                severity_reason="routine correction, no change to coverage",
            )
        ],
        gaps=[],
        reconciled_position="The position is as stated [E2.1].",
    )


def default_assessment(**overrides) -> StatusAssessment:
    defaults = dict(
        traffic_light="Amber",
        traffic_light_rationale="Not Green because the measure has receded [E1.1].",
        traffic_light_reasoning="The full argument would go here [E1.1].",
        traffic_light_claim_ids=["E1.1"],
        progress_percent=30,
        progress_rationale="30% of the success measure is attained [E1.1].",
        progress_reasoning="The full derivation would go here [E1.1].",
        progress_claim_ids=["E1.1"],
    )
    return StatusAssessment(**{**defaults, **overrides})


def default_narratives(**overrides) -> NarrativeSet:
    from app.schema import NarrativeField

    from app.schema import ChoiceReason

    defaults = dict(
        key_success=NarrativeField(text="One thing landed.", claim_ids=["E1.1"]),
        key_challenge=NarrativeField(text="Another did not.", claim_ids=["E1.1"]),
        support_needed=NarrativeField(text=None, needs_director_input=True),
        support_from=["Other"],
        support_from_reasons=[
            ChoiceReason(
                value="Other",
                reason=(
                    "Needs a legal drafter seconded for the data-protection annex "
                    "[E1.1]. Neither HR nor ILT covers procuring specialist external "
                    "legal work."
                ),
            )
        ],
    )
    return NarrativeSet(**{**defaults, **overrides})
