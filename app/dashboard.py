"""Director-facing snapshot of one draft.

This is not the institute Power BI model. It shows this row against last
quarter so a director can see what moved, with the closed traffic-light
colours. Trend_vs_Prior_Quarter is still absent — Power BI derives that
downstream; the labels here are visual only.
"""

from __future__ import annotations

from typing import Any

from . import vocab
from .schema import DraftRow, FieldProposal

_TL_GLYPH = {"Red": "●", "Amber": "◐", "Green": "○", "Blue": "◆"}


def _plain(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "—":
        return None
    return text


def _as_int(value: Any) -> int | None:
    text = _plain(value)
    if text is None:
        return None
    try:
        return int(text.replace("%", "").split()[0])
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v and str(v) != "—"]
    text = _plain(value)
    if text is None:
        return []
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def _proposal_value(proposal: Any) -> Any:
    if isinstance(proposal, FieldProposal):
        return proposal.value
    return proposal


def _movement(prior: Any, current: Any) -> str:
    """Visual only. Not Trend_vs_Prior_Quarter."""
    left, right = _plain(prior), _plain(current)
    if left is None and right is None:
        return "none"
    if left is None:
        return "new"
    if right is None:
        return "changed"
    if str(left) == str(right):
        return "same"
    return "changed"


def _join(value: Any) -> str | None:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value) if value else None
    return _plain(value)


def _tone_for_days(days: int | None) -> str:
    if days is None:
        return ""
    if days < 0:
        return "Red"
    if days <= 30:
        return "Amber"
    return "Green"


def _tone_for_attention(conflicts: list, gaps: list) -> str:
    high = any(item.get("severity") == "high" for item in conflicts + gaps)
    if high:
        return "Red"
    if conflicts or gaps:
        return "Amber"
    return "Green"


def _tone_for_review(acknowledged: int, total: int) -> str:
    if total and acknowledged >= total:
        return "Green"
    if acknowledged:
        return "Amber"
    return "Amber"


def _tip_traffic(value: str | None, *, which: str) -> str:
    if not value or value not in vocab.TRAFFIC_LIGHTS:
        return f"{which}: no traffic light yet. Closed vocabulary: Red, Amber, Green, Blue."
    anchor = vocab.TRAFFIC_LIGHTS[value]
    return (
        f"{which}: {value} — {anchor}. "
        "Closed vocabulary from the CSF extract. A value outside it is invalid."
    )


def _tip_progress(pct: int | None, *, which: str, prior: int | None = None) -> str:
    if pct is None:
        return f"{which}: no progress figure yet. The field is a 0–100 integer."
    extra = ""
    if prior is not None and which.startswith("This"):
        extra = (
            f" Last quarter was {prior}%. The two figures may not share a "
            "denominator. Power BI will still compute Trend_vs_Prior_Quarter "
            "downstream — this screen does not submit that field."
        )
    return (
        f"{which}: {pct}% attainment of the success measure, 0–100 integer.{extra}"
    )


def from_review(ctx: dict) -> dict[str, Any]:
    """Build the snapshot the dashboard module renders.

    Reads the same context the review page already has, so the two surfaces
    cannot drift: one derivation, two presentations.
    """
    row: DraftRow | None = ctx.get("row")
    prior = ctx.get("prior_update") or {}
    conflicts = list(ctx.get("conflicts") or [])
    gaps = list(ctx.get("gaps") or [])
    fields = list(ctx.get("fields") or [])
    acknowledged = int(ctx.get("acknowledged_count") or 0)
    total_fields = len(fields) if fields else (len(row.proposals()) if row else 0)
    days = ctx.get("days_remaining")
    objective = ctx.get("objective") or {}
    docs = ctx.get("docs") or {}

    current_tl = _plain(_proposal_value(row.Traffic_Light) if row else None)
    current_pg = _as_int(_proposal_value(row.Progress_Percent) if row else None)
    prior_tl = _plain(prior.get("Traffic_Light"))
    prior_pg = _as_int(prior.get("Progress_Percent"))

    current_success = _join(_proposal_value(row.Key_Success) if row else None)
    current_challenge = _join(_proposal_value(row.Key_Challenge) if row else None)
    current_support = _join(_proposal_value(row.Support_Needed) if row else None)
    current_from = _as_list(_proposal_value(row.Support_From) if row else None)
    prior_from = _as_list(prior.get("Support_From"))

    high_conflicts = sum(1 for c in conflicts if c.get("severity") == "high")
    unseen_findings = sum(
        1 for item in conflicts + gaps if not item.get("acknowledged")
    )

    compare = [
        {
            "field": "Traffic_Light",
            "label": "Traffic light",
            "kind": "traffic",
            "prior": prior_tl,
            "current": current_tl,
            "movement": _movement(prior_tl, current_tl),
            "prior_tip": _tip_traffic(prior_tl, which="Last quarter"),
            "current_tip": _tip_traffic(current_tl, which="This draft"),
        },
        {
            "field": "Progress_Percent",
            "label": "Progress",
            "kind": "progress",
            "prior": prior_pg,
            "current": current_pg,
            "movement": _movement(prior_pg, current_pg),
            "prior_tip": _tip_progress(prior_pg, which="Last quarter"),
            "current_tip": _tip_progress(
                current_pg, which="This draft", prior=prior_pg
            ),
        },
        {
            "field": "Key_Success",
            "label": "Key success",
            "kind": "text",
            "prior": _plain(prior.get("Key_Success")),
            "current": current_success,
            "movement": _movement(prior.get("Key_Success"), current_success),
            "prior_tip": "What you submitted last quarter as the success.",
            "current_tip": "What this draft proposes. Hover a citation on the review screen to open the source.",
        },
        {
            "field": "Key_Challenge",
            "label": "Key challenge",
            "kind": "text",
            "prior": _plain(prior.get("Key_Challenge")),
            "current": current_challenge,
            "movement": _movement(prior.get("Key_Challenge"), current_challenge),
            "prior_tip": "What you submitted last quarter as the challenge.",
            "current_tip": "What this draft proposes as the thing most likely to stop the objective.",
        },
        {
            "field": "Support_Needed",
            "label": "Support needed",
            "kind": "text",
            "prior": _plain(prior.get("Support_Needed")),
            "current": current_support,
            "movement": _movement(prior.get("Support_Needed"), current_support),
            "prior_tip": "What you submitted last quarter.",
            "current_tip": "Left empty when the evidence does not name a need — that is deliberate, not an omission.",
        },
        {
            "field": "Support_From",
            "label": "Support from",
            "kind": "list",
            "prior": prior_from,
            "current": current_from,
            "movement": _movement(_join(prior_from), _join(current_from)),
            "prior_tip": "Closed vocabulary: ILT, BDU, Finance, HR, IDDT, Communications, Other. Empty is valid.",
            "current_tip": "Closed vocabulary: ILT, BDU, Finance, HR, IDDT, Communications, Other. Empty is valid when nothing is asked.",
        },
    ]

    legend = [
        {
            "value": value,
            "anchor": anchor,
            "glyph": _TL_GLYPH[value],
            "tip": f"{value} — {anchor}. Closed vocabulary. A value outside it is invalid.",
        }
        for value, anchor in vocab.TRAFFIC_LIGHTS.items()
    ]

    source = (row.Source if row else vocab.SUBSTRATE_DRAFTED) or vocab.SUBSTRATE_DRAFTED
    pending = bool(ctx.get("pending"))

    return {
        "quarter": getattr(row, "Quarter", None) or "",
        "objective_id": getattr(row, "Objective_ID", None) or objective.get("Objective_ID", ""),
        "title": objective.get("Title", ""),
        "success_measure": objective.get("Success_Measure", ""),
        "source": source,
        "source_tip": (
            "Source is Substrate-Drafted for a row originating from the Personal "
            "Layer. It is not the director's value to pick."
        ),
        "prior_quarter": prior.get("Quarter") or "previous quarter",
        "prior_submitted": prior.get("Submitted") or "",
        "has_prior": bool(prior),
        "current_tl": current_tl,
        "prior_tl": prior_tl,
        "current_pg": current_pg,
        "prior_pg": prior_pg,
        "tl_glyph": _TL_GLYPH.get(current_tl or "", "•"),
        "prior_tl_glyph": _TL_GLYPH.get(prior_tl or "", "•"),
        "tl_tip": _tip_traffic(current_tl, which="This draft"),
        "prior_tl_tip": _tip_traffic(prior_tl, which="Last quarter"),
        "pg_tip": _tip_progress(current_pg, which="This draft", prior=prior_pg),
        "days": days,
        "days_tone": _tone_for_days(days),
        "days_tip": (
            "No target completion on the objective record."
            if days is None
            else (
                f"{abs(days)} days overdue against the target completion."
                if days < 0
                else f"{days} days remaining to the target completion on the objective record. Arithmetic is against the run's as-of date, not the clock."
            )
        ),
        "conflicts": len(conflicts),
        "gaps": len(gaps),
        "high_conflicts": high_conflicts,
        "unseen_findings": unseen_findings,
        "attention_tone": _tone_for_attention(conflicts, gaps),
        "attention_tip": (
            f"{len(conflicts)} contradicted statement"
            f"{'' if len(conflicts) == 1 else 's'} "
            f"({high_conflicts} high), {len(gaps)} gap"
            f"{'' if len(gaps) == 1 else 's'} the evidence raised and never settled. "
            "Acknowledge on a finding means you have read it, not that the two accounts now agree."
        ),
        "acknowledged": acknowledged,
        "total_fields": total_fields,
        "review_tone": _tone_for_review(acknowledged, total_fields),
        "review_tip": (
            f"{acknowledged} of {total_fields} draft fields checked. "
            "This is the human gate: export stays locked until every field is acknowledged. "
            "It is not a comparison with last quarter."
        ),
        "docs_count": len(docs) if hasattr(docs, "__len__") else 0,
        "docs_tip": "Documents this run read. Citations on the draft point into these files, not at sentences the model invented.",
        "compare": compare,
        "legend": legend,
        "trend_note": (
            "Trend_vs_Prior_Quarter (Improved / Same / Deteriorated / New) is "
            "calculated downstream in Power BI. It is not a field on this draft "
            "and is not submitted from here. The colours and 'changed / same' "
            "labels below are context so you can see last quarter was not copied forward."
        ),
        "movement_tips": {
            "changed": "The two values differ. Visual only — Power BI will classify the official trend.",
            "same": "This draft currently matches last quarter. That is a choice, not an inheritance.",
            "new": "No prior value to compare against.",
            "none": "Neither quarter has a value.",
        },
        "pending": pending,
    }


def from_index(known: dict[str, Any], *, pending: bool = False) -> dict[str, Any]:
    """Snapshot from the workspace index when the graph state is not open yet.

    A run that is still reading has no draft row. The sidebar still offers
    Snapshot, and a JSON 404 there reads as the page missing rather than as
    the draft not being ready.
    """
    snap = from_review(
        {
            "row": None,
            "prior_update": {},
            "conflicts": [],
            "gaps": [],
            "fields": [],
            "acknowledged_count": 0,
            "days_remaining": None,
            "objective": {
                "Objective_ID": known.get("objective_id") or "",
                "Title": known.get("objective_title") or "",
            },
            "docs": {},
            "pending": pending or known.get("status") == "running",
        }
    )
    snap["quarter"] = known.get("quarter") or ""
    snap["objective_id"] = known.get("objective_id") or ""
    snap["title"] = known.get("objective_title") or ""
    snap["current_tl"] = _plain(known.get("traffic_light"))
    snap["current_pg"] = _as_int(known.get("progress_percent"))
    snap["tl_glyph"] = _TL_GLYPH.get(snap["current_tl"] or "", "•")
    snap["tl_tip"] = _tip_traffic(snap["current_tl"], which="This draft")
    snap["pg_tip"] = _tip_progress(snap["current_pg"], which="This draft")
    snap["pending"] = pending or known.get("status") == "running"
    return snap


def from_workspace(runs: list[dict[str, Any]], totals: dict[str, Any]) -> dict[str, Any]:
    """Product-level view across every run. Not a substitute for a run snapshot."""
    lights = {value: 0 for value in vocab.TRAFFIC_LIGHTS}
    by_status = {"running": 0, "review": 0, "staged": 0, "failed": 0, "other": 0}
    progress_values: list[int] = []
    latest = runs[0] if runs else {}

    for run in runs:
        status = run.get("status") or "other"
        if status in by_status:
            by_status[status] += 1
        else:
            by_status["other"] += 1
        light = _plain(run.get("traffic_light"))
        if light in lights:
            lights[light] += 1
        pct = _as_int(run.get("progress_percent"))
        if pct is not None:
            progress_values.append(pct)

    mean_pg = (
        round(sum(progress_values) / len(progress_values)) if progress_values else None
    )
    latest_tl = _plain(latest.get("traffic_light"))
    running = by_status["running"]
    review = by_status["review"]
    approved = by_status["staged"]

    return {
        "run_count": len(runs),
        "running": running,
        "review": review,
        "approved": approved,
        "failed": by_status["failed"],
        "lights": [
            {
                "value": value,
                "count": lights[value],
                "glyph": _TL_GLYPH[value],
                "anchor": vocab.TRAFFIC_LIGHTS[value],
            }
            for value in vocab.TRAFFIC_LIGHTS
        ],
        "light_total": sum(lights.values()),
        "mean_pg": mean_pg,
        "latest": latest,
        "latest_tl": latest_tl,
        "latest_tl_glyph": _TL_GLYPH.get(latest_tl or "", "•"),
        "calls": totals.get("llm_calls") or 0,
        "tokens_in": totals.get("llm_input_tokens") or 0,
        "tokens_out": totals.get("llm_output_tokens") or 0,
        "edits": totals.get("edits") or 0,
        "runs": runs,
        "running_tip": (
            f"{running} run{'s' if running != 1 else ''} still deriving a draft. "
            "Nothing is submitted from here."
        ),
        "review_tip": (
            f"{review} draft{'s' if review != 1 else ''} waiting for a director "
            "to check every field."
        ),
        "approved_tip": (
            f"{approved} row{'s' if approved != 1 else ''} approved for export. "
            "This tool does not submit them."
        ),
        "light_tip": (
            "Closed vocabulary across every draft that has a traffic light. "
            "A run still in flight has none yet."
        ),
        "cost_tip": (
            f"{totals.get('llm_calls') or 0} live model calls. "
            "No response cache in this build."
        ),
    }
