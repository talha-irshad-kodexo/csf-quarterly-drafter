"""The audit trail: what happened, written down as it happened.

Three kinds of record, one file per run, append-only:

  event      a pipeline stage started, finished or failed
  llm_call   one round trip to the model — stage, prompt, tokens, latency
  edit       one director action against the draft

Append-only is the point. The graph checkpoint holds the *current* state of a
run, which means it can only ever answer "what does the draft say now". A
director who edits one field ten times leaves ten rows here, and a field edited
repeatedly is a field the draft is getting wrong — that is the signal the
checkpoint throws away.

The ledger lives next to the staged row rather than in the checkpoint database
so that it survives a server restart, an interrupted run, and a checkpoint that
cannot be opened. Reading it requires nothing but the filesystem.

No prompt or response body is stored. The trail records what happened, not the
evidence text a second time.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("csf-drafter")

LEDGER = "audit.jsonl"

# Which pipeline stage a model call belongs to, keyed by the schema it fills.
# Derived here rather than passed in at every call site: the schema already
# names the pass, and threading a label through four nodes would be one more
# thing to keep in sync.
STAGE_BY_SCHEMA: dict[str, str] = {
    "Understanding": "load",
    "Reconciliation": "reconcile",
    "StatusAssessment": "assess",
    "NarrativeSet": "compose",
    # compose is three concurrent calls over one brief, so three
    # entries land under the same stage. That is what happened.
    "SuccessNarrative": "compose",
    "ChallengeNarrative": "compose",
    "SupportAsk": "compose",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ledger_path(run_dir: Path) -> Path:
    return run_dir / LEDGER


def append(run_dir: Path | None, record: dict) -> None:
    """Add one row. Never raises: an unwritable trail must not fail a run.

    Written twice: to the run's own JSONL file, which is the record, and to the
    workspace index, which is what makes the same row findable from outside the
    folder it happened in. The file is written first and on its own — a
    database that will not open must not cost the trail.
    """
    if run_dir is None:
        return
    row = {"at": now(), **record}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with ledger_path(run_dir).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
    except OSError:
        logger.warning("could not write the audit trail for %s", run_dir, exc_info=True)

    from . import db  # deferred: keeps this module importable on its own

    # The index belongs to the runs folder this trail lives in, so it is
    # derived from the path rather than from a global someone had to remember
    # to set. A run written to a temporary folder indexes into that folder.
    db.configure(run_dir.parent / db.FILENAME)
    db.record_audit(run_dir.name, row)


def read(run_dir: Path | None) -> list[dict]:
    if run_dir is None:
        return []
    path = ledger_path(run_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def of_kind(rows: Iterable[dict], kind: str) -> list[dict]:
    return [row for row in rows if row.get("kind") == kind]


# --- writing -----------------------------------------------------------------


def record_event(run_dir: Path | None, event: dict) -> None:
    """One pipeline stage, as published to the progress stream.

    Fields are listed rather than copied wholesale. A graph payload can carry
    the evidence text, and the trail records what happened, not the documents
    a second time — so anything new has to be named here on purpose.
    """
    record = {
        "kind": "event",
        "stage": event.get("stage", ""),
        "label": event.get("label", ""),
        "detail": event.get("detail", ""),
        "status": (
            "failure"
            if event.get("stage") == "failed"
            else "success" if event.get("stage") == "done" else "progress"
        ),
    }
    # The run parameters, kept on the run that used them.
    if event.get("as_of"):
        record["as_of"] = event["as_of"]
    append(run_dir, record)


def record_llm_call(run_dir: Path | None, call: dict) -> None:
    append(run_dir, {"kind": "llm_call", **call})


def record_edit(
    run_dir: Path | None,
    *,
    field: str,
    edit_kind: str,
    before: Any,
    after: Any,
    claim_ids_shown: list[str] | None = None,
    reason: str = "",
) -> None:
    """One director action. `before`/`after` are the values, not the row."""
    append(
        run_dir,
        {
            "kind": "edit",
            "field": field,
            "edit_kind": edit_kind,
            "value_before": _flat(before),
            "value_after": _flat(after),
            "char_distance": char_distance(_flat(before), _flat(after)),
            "claim_ids_shown": claim_ids_shown or [],
            "reason": reason,
        },
    )


def record_ack(run_dir: Path | None, *, finding_id: str, title: str, acked: bool) -> None:
    """A director marking a finding seen, or unmarking it.

    Kept in the same append-only file as everything else rather than in the
    graph checkpoint. Acknowledgement is a fact about the review, not about the
    draft, and folding it out of the trail means the history — acknowledged,
    then unacknowledged after opening the source — is not lost the way a single
    boolean would lose it.
    """
    append(
        run_dir,
        {"kind": "ack", "finding_id": finding_id, "title": title, "acked": acked},
    )


def record_citation(run_dir: Path | None, *, field: str, ref: str, dismissed: bool) -> None:
    """A director judging one citation relevant or not."""
    append(
        run_dir,
        {"kind": "citation", "field": field, "ref": ref, "dismissed": dismissed},
    )


def dismissed_citations(rows: Iterable[dict]) -> dict[str, set[str]]:
    """Currently-dismissed citations per field, folded from the trail."""
    state: dict[str, dict[str, bool]] = {}
    for row in of_kind(rows, "citation"):
        state.setdefault(row.get("field", ""), {})[row.get("ref", "")] = bool(
            row.get("dismissed")
        )
    return {
        field: {ref for ref, off in refs.items() if off}
        for field, refs in state.items()
    }


def acknowledged(rows: Iterable[dict]) -> set[str]:
    """Which findings currently stand acknowledged, folded from the trail."""
    state: dict[str, bool] = {}
    for row in of_kind(rows, "ack"):
        state[row.get("finding_id", "")] = bool(row.get("acked"))
    return {finding_id for finding_id, acked in state.items() if acked}


def _flat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


# --- reading -----------------------------------------------------------------


def char_distance(before: str | None, after: str | None) -> int | None:
    """Levenshtein distance between two field values.

    Keystroke-weighted distance is what tracks the effort a director actually
    spent; surface-similarity scores do not. A field with a large distance was
    rewritten, not accepted, and that difference is the whole point of logging
    the edit.
    """
    if before is None or after is None:
        return None
    if before == after:
        return 0

    previous = list(range(len(after) + 1))
    for i, left in enumerate(before, start=1):
        current = [i]
        for j, right in enumerate(after, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left != right),
                )
            )
        previous = current
    return previous[-1]


def describe_edit(field: str, before: Any, after: Any) -> str:
    """What kind of edit this was, in the vocabulary the audit page shows.

    An override is a director replacing a value the workflow derived on a
    field where that derivation is the product — the traffic light and the
    progress figure. Changing those is a different act from tightening a
    sentence, and the trail should not flatten the two.
    """
    from . import vocab

    if _flat(before) == _flat(after):
        return "acknowledged"
    if field in ("Traffic_Light", "Progress_Percent"):
        return "override"
    if field in vocab.NARRATIVE_FIELDS or field == "Support_From":
        return "value_change"
    return "value_change"


def as_of(rows: Iterable[dict]) -> str:
    """The as-of date this run was started with, if it recorded one."""
    for row in of_kind(rows, "event"):
        if row.get("as_of"):
            return row["as_of"]
    return ""


def counts(rows: Iterable[dict]) -> dict:
    """Headline numbers for the sidebar and the runs list."""
    rows = list(rows)
    calls = of_kind(rows, "llm_call")
    return {
        "events": len(of_kind(rows, "event")),
        "llm_calls": len(calls),
        "llm_input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
        "llm_output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
        "edits": len(of_kind(rows, "edit")),
    }


# --- the recorder handed to the model client ---------------------------------


def recorder_for(run_dir: Path | None):
    """A callable the LLM client uses to log each round trip.

    Returned as a closure rather than exposing the run directory to llm.py:
    the client should know it is being observed and nothing more about where.
    """

    def record(call: dict) -> None:
        record_llm_call(run_dir, call)

    return record
