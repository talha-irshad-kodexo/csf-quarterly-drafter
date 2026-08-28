"""The web app the director actually uses.

Server-rendered HTML with progressive enhancement for inline edits. No build
step, one process, one command to start.

Every route either reads state or records a director's edit. None of them
submits to the system of record.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langgraph.types import Command

from . import audit, config, dashboard, db, progress, render as md, store, vocab
from .config import DEMO_PACK_DIR, PACKAGE_DIR, settings
from .graph.build import open_graph
from .evidence import parse_date, parse_field_table
from .inputs import load_inputs
from .llm import AnthropicClient, ReadOnlyClient
from .validate import advice, errors
from .schema import Correction, DraftRow

logger = logging.getLogger("csf-drafter")

app = FastAPI(title="CSF quarterly update drafter")
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def _fmt_date(value: Any) -> str:
    """`2026-09-30` or a date object as `30 Sep 2026`.

    Dates a director reads should look like dates, not like database keys. ISO
    stays in the audit trail and the exports, where it is the right answer.
    """
    if value in (None, ""):
        return "—"
    date = value if isinstance(value, dt.date) else parse_date(str(value))
    if date is None:
        return str(value)
    return f"{date.day} {date:%b} {date.year}"


def _fmt_datetime(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        stamp = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{stamp.day} {stamp:%b} {stamp.year}, {stamp:%H:%M}"


def _claim_refs(text: Any) -> list[str]:
    """Claim ids cited inside square brackets, in the order they appear.

    Rationales cite as prose — "the measure has receded [E5.5, E5.6]" — which
    reads well and leads nowhere. Pulling the ids out lets the same sentence
    carry a row of real links underneath it, so a reference in the argument is
    followable rather than decorative.
    """
    if not text:
        return []
    found: list[str] = []
    for bracket in vocab.BRACKET_PATTERN.findall(str(text)):
        for part in bracket.split(","):
            ref = part.strip()
            if vocab.CLAIM_ID_PATTERN.match(ref) and ref not in found:
                found.append(ref)
    return found


def _cite_tokens(text: Any) -> list[list[dict[str, Any]]]:
    """Paragraphs of text/cite tokens, so a mark in the sentence is a link.

    A bracket that is not a claim id is left as text — "[prior submission
    context]" is a hedge, not a citation.
    """
    if not text:
        return []
    paragraphs: list[list[dict[str, Any]]] = []
    for block in str(text).split("\n\n"):
        block = block.strip()
        if not block:
            continue
        tokens: list[dict[str, Any]] = []
        cursor = 0
        for match in vocab.BRACKET_PATTERN.finditer(block):
            refs = [part.strip() for part in match.group(1).split(",") if part.strip()]
            if not refs or not all(vocab.CLAIM_ID_PATTERN.match(ref) for ref in refs):
                continue
            if match.start() > cursor:
                tokens.append({"kind": "text", "text": block[cursor : match.start()]})
            tokens.append({"kind": "refs", "refs": refs})
            cursor = match.end()
        if cursor < len(block):
            tokens.append({"kind": "text", "text": block[cursor:]})
        if tokens:
            paragraphs.append(tokens)
    return paragraphs


templates.env.filters["fmt_date"] = _fmt_date
templates.env.filters["fmt_datetime"] = _fmt_datetime
templates.env.filters["claim_refs"] = _claim_refs
templates.env.filters["cite_tokens"] = _cite_tokens

# Inline CSS/JS in HTML so Render free-tier cold starts still look right.
# Edge 404s (`x-render-routing: no-server`) on /static/* while HTML succeeds
# otherwise leave the page unstyled. Read per response so --reload and
# template |safe stay in sync with files on disk.
_STATIC = PACKAGE_DIR / "static"


def _inline_css() -> str:
    return "\n".join(
        (_STATIC / name).read_text(encoding="utf-8")
        for name in ("tokens.css", "app.css")
    )


def _inline_js() -> str:
    return (_STATIC / "app.js").read_text(encoding="utf-8")



def client() -> AnthropicClient:
    """The model client. Requires a key; there is no offline mode."""
    if not has_api_key():
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add a key.",
        )
    return AnthropicClient(settings)


def run_client(run_dir: Path):
    """The model client for one run, reporting each round trip into its trail.

    Separate from `client()` because that is the seam tests replace, and a
    stub has nothing to meter. A client that cannot be observed is used as-is:
    the audit trail is worth having, never worth failing a run over.
    """
    llm = client()
    attach = getattr(llm, "recording_to", None)
    return attach(audit.recorder_for(run_dir)) if attach else llm


def graph_client():
    """Client for opening the checkpointer. Read-only when no key is set."""
    if has_api_key():
        return AnthropicClient(settings)
    return ReadOnlyClient()


def has_api_key() -> bool:
    return bool(settings.resolved_api_key())


_reconciled: set[Path] = set()


def index() -> None:
    """Point the workspace index at the runs folder currently in force.

    Called from every route that reads or writes it rather than once at import,
    because `settings` is replaceable — tests swap in a temporary runs folder,
    and an index bound at import time would keep writing to the old one.

    Reconciles once per folder per process, so runs that were drafted before
    the index existed still appear in it. The trail on disk is the record; this
    is a cache over it, and a cache that silently omits half the history is
    worse than no cache at all.
    """
    path = settings.workspace_db
    db.configure(path)
    if path in _reconciled:
        return
    _reconciled.add(path)
    _backfill()


def _backfill() -> None:
    """Import trails the index has never seen.

    Emptiness per thread is the guard against importing anything twice, which
    is why this reads the index before the file rather than the other way
    round. Runs still being written are skipped: they already have rows.
    """
    runs_dir = settings.runs_dir
    if not runs_dir.exists():
        return

    for path in sorted(runs_dir.iterdir()):
        if not path.is_dir() or path.name in {"understanding"}:
            continue
        thread_id = path.name
        if db.has_audit(thread_id):
            continue
        rows = audit.read(path)
        if not rows:
            continue
        db.import_audit(thread_id, rows)

        staged = path / "staged_row.json"
        row: dict = {}
        if staged.exists():
            try:
                row = flatten_staged_row(json.loads(staged.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                row = {}
        # Which model did the reasoning, read back from the calls it made.
        # The trail never recorded it as a run property, but every reasoning
        # call names it, and a run whose model is unknown is a run whose output
        # cannot be compared with the next one's.
        reasoning = next(
            (
                call.get("model")
                for call in reversed(audit.of_kind(rows, "llm_call"))
                if call.get("stage") in {"reconcile", "assess", "compose"}
            ),
            None,
        )

        # The quarter, from the parameters the run recorded about itself. The
        # objective id is not in the trail at all, so a pre-index run that was
        # never approved keeps a dash there rather than a guess.
        created = next(iter(audit.of_kind(rows, "event")), {})
        from_detail = re.search(r"quarter (\S+)", created.get("detail", "") or "")

        db.upsert_run(
            thread_id,
            created_at=rows[0].get("at"),
            model=reasoning,
            status="staged" if staged.exists() else "review",
            quarter=row.get("Quarter") or (from_detail.group(1) if from_detail else None),
            objective_id=row.get("Objective_ID"),
            traffic_light=row.get("Traffic_Light"),
            progress_percent=row.get("Progress_Percent"),
            as_of=audit.as_of(rows) or None,
            staged_at=_staged_at(thread_id) or None,
        )
        if row:
            # The staged row is the only version of this draft still on disk.
            # Recorded as one snapshot rather than invented history.
            db.save_draft(thread_id, row, reason="approved for export")


def proposal_value(raw: Any) -> Any:
    """Scalar from a staged field — flat new files or nested legacy dumps."""
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value")
    return raw


def flatten_staged_row(row: dict) -> dict:
    """Display/download shape: proposal nests become scalars."""
    flat = dict(row)
    for key in (
        "Traffic_Light",
        "Progress_Percent",
        "Key_Success",
        "Key_Challenge",
        "Support_Needed",
        "Support_From",
    ):
        if key in flat:
            flat[key] = proposal_value(flat[key])
    return flat


def key_source() -> str:
    """Where the loaded key came from, so it is obvious what is in play."""
    if config.runtime_api_key():
        return "entered here"
    if settings.anthropic_api_key:
        return ".env"
    return "environment" if has_api_key() else ""


def shell_context(
    *,
    nav: str = "",
    thread_id: str | None = None,
    staged_path: str | None = None,
    first_doc_id: str | None = None,
    run_meta: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """Chrome shared by every page: key status, sidebar, run count."""
    model = settings.resolved_model()
    reader_model = settings.resolved_reader_model()
    ctx = {
        "run_meta": run_meta or {},
        "nav": nav,
        "has_key": has_api_key(),
        "key_source": key_source(),
        "masked_key": config.mask(settings.resolved_api_key()),
        "run_count": len(discover_runs()),
        "thread_id": thread_id,
        "staged_path": staged_path,
        "first_doc_id": first_doc_id,
        "inline_css": _inline_css(),
        "inline_js": _inline_js(),
        "model": model,
        "reader_model": reader_model,
        # Offered rather than typed. See config.MODEL_CHOICES.
        "model_options": config.model_options(model),
        "reader_model_options": config.model_options(reader_model),
        # The sidebar shows what this run cost: model calls are the part of the
        # trail a reviewer most often wants without opening the audit page.
        "audit_counts": (
            audit.counts(audit.read(settings.run_dir(thread_id))) if thread_id else None
        ),
    }
    if extra:
        ctx.update(extra)
    return ctx


def _field(source: Any, name: str) -> str:
    """Read one field from a DraftRow or from a flattened staged row.

    `row` means both in this app: the review page holds the pydantic model, the
    export page holds the scalars-only dict that gets downloaded. The sidebar
    does not care which, so it asks rather than assumes.
    """
    if source is None:
        return ""
    if isinstance(source, dict):
        return source.get(name) or ""
    return getattr(source, name, "") or ""


def _run_meta(context: dict) -> dict:
    """Sidebar identity for a run: the quarter and objective, not just an id."""
    row = context.get("row")
    objective = context.get("objective") or {}
    quarter = _field(row, "Quarter") or objective.get("Quarter", "")
    objective_id = _field(row, "Objective_ID") or objective.get("Objective_ID", "")

    # The audit page holds neither a draft row nor the objective record, so its
    # sidebar used to say " · " and name nothing. The index knows.
    thread_id = context.get("thread_id")
    if thread_id and not (quarter and objective_id):
        index()
        known = db.run(thread_id) or {}
        quarter = quarter or (known.get("quarter") or "")
        objective_id = objective_id or (known.get("objective_id") or "")

    return {
        "quarter": quarter,
        "objective_id": objective_id,
        "evidence_count": len(context.get("docs") or {}),
    }


def render(
    request: Request,
    name: str,
    context: dict | None = None,
    *,
    nav: str = "",
    thread_id: str | None = None,
    staged_path: str | None = None,
    first_doc_id: str | None = None,
    status_code: int = 200,
):
    context = context or {}
    base = shell_context(
        nav=nav,
        thread_id=thread_id or context.get("thread_id"),
        staged_path=staged_path
        if staged_path is not None
        else context.get("staged_path"),
        first_doc_id=first_doc_id
        if first_doc_id is not None
        else context.get("first_doc_id"),
        run_meta=_run_meta(context),
    )
    return templates.TemplateResponse(
        request, name, base | context, status_code=status_code
    )


def discover_runs() -> list[dict]:
    """Known runs from the live registry and run directories on disk."""
    index()
    found: dict[str, dict] = {}

    # What the index knows about each run before it was staged. A run under
    # review has no staged file to read a quarter out of, so without this the
    # list showed a row of dashes until the moment it was approved — the exact
    # window in which a director is most likely to be looking for it.
    indexed = {row["thread_id"]: row for row in db.runs()}

    runs_dir = settings.runs_dir
    if runs_dir.exists():
        for path in runs_dir.iterdir():
            if not path.is_dir():
                continue
            if path.name in {"understanding"}:
                continue
            thread_id = path.name
            staged = path / "staged_row.json"
            ledger = audit.read(path)
            entry: dict[str, Any] = {
                "thread_id": thread_id,
                "status": "staged" if staged.exists() else "review",
                "quarter": "",
                "objective_id": "",
                "traffic_light": "",
                "progress_percent": None,
                "staged": staged.exists(),
                "updated": staged.stat().st_mtime if staged.exists() else path.stat().st_mtime,
                "created": ledger[0]["at"] if ledger else "",
                "llm_calls": audit.counts(ledger)["llm_calls"],
                "edits": audit.counts(ledger)["edits"],
                # What the tool first proposed, so the list can show
                # "proposed → current" where a director overrode it.
                "proposed_traffic_light": next(
                    (
                        e.get("value_before")
                        for e in audit.of_kind(ledger, "edit")
                        if e.get("field") == "Traffic_Light"
                    ),
                    "",
                ),
            }
            known = indexed.get(thread_id) or {}
            for column, key in (
                ("quarter", "quarter"),
                ("objective_id", "objective_id"),
                ("traffic_light", "traffic_light"),
            ):
                if known.get(column):
                    entry[key] = known[column]
            if known.get("progress_percent") is not None:
                entry["progress_percent"] = known["progress_percent"]
            if known.get("created_at") and not entry["created"]:
                entry["created"] = known["created_at"]

            if staged.exists():
                try:
                    row = flatten_staged_row(
                        json.loads(staged.read_text(encoding="utf-8"))
                    )
                    entry["quarter"] = row.get("Quarter") or ""
                    entry["objective_id"] = row.get("Objective_ID") or ""
                    entry["traffic_light"] = row.get("Traffic_Light") or ""
                    entry["progress_percent"] = row.get("Progress_Percent")
                except (OSError, json.JSONDecodeError):
                    pass
            found[thread_id] = entry

    for thread_id, run in list(progress.registry.items()):
        entry = found.get(thread_id) or {
            "thread_id": thread_id,
            "status": "running",
            "quarter": "",
            "objective_id": "",
            "traffic_light": "",
            "progress_percent": None,
            "staged": False,
            "updated": 0,
            "created": "",
            "llm_calls": 0,
            "edits": 0,
        }
        if not run.finished:
            entry["status"] = "running"
        elif any(e.get("stage") == "failed" for e in run.events):
            entry["status"] = "failed"
        elif entry.get("staged"):
            entry["status"] = "staged"
        else:
            entry["status"] = "review"
        found[thread_id] = entry

    return sorted(found.values(), key=lambda r: r.get("updated") or 0, reverse=True)


# --- the key -----------------------------------------------------------------


@app.post("/settings/key")
async def set_key(
    request: Request,
    api_key: str = Form(default=""),
    model: str = Form(default=""),
    reader_model: str = Form(default=""),
):
    """The Settings dialog: the key and both models, saved together."""
    known = {value for value, _ in config.model_options(settings.resolved_model())}
    changed: list[str] = []

    if model.strip() and model.strip() in known and model.strip() != settings.resolved_model():
        config.set_runtime_model(model)
        changed.append(f"reasoning on {model.strip()}")
    if (
        reader_model.strip()
        and reader_model.strip() in known
        and reader_model.strip() != settings.resolved_reader_model()
    ):
        config.set_runtime_reader_model(reader_model)
        changed.append(f"reading on {reader_model.strip()}")

    api_key = api_key.strip()
    if not api_key:
        if changed:
            return _workspace(request, message=f"Saved — {' · '.join(changed)}.")
        return _workspace(request, error="Paste a key first.")

    config.set_runtime_api_key(api_key)

    try:
        await AnthropicClient(settings).check()
    except Exception as error:
        config.clear_runtime_api_key()
        return _workspace(request, error=_readable(error))

    suffix = f" Also saved: {' · '.join(changed)}." if changed else ""
    return _workspace(request, message=f"Key accepted. The API answered.{suffix}")


@app.post("/settings/key/clear")
async def clear_key(request: Request):
    config.clear_runtime_api_key()
    return _workspace(request, message="Key cleared from this server.")


def _readable(error: Exception) -> str:
    """Turn an SDK exception into something a person can act on."""
    text = str(error)
    if "authentication_error" in text or "invalid x-api-key" in text or "401" in text:
        return "That key was rejected. Check it was copied whole."
    if "permission" in text.lower() or "403" in text:
        return "That key is valid but not permitted to use this model."
    if "credit" in text.lower() or "billing" in text.lower():
        return "That key has no available credit."
    return f"Could not reach the API: {text[:200]}"


def thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_run_dir(thread_id: str) -> Path:
    """Resolve a run folder inside runs_dir, or refuse path tricks."""
    if (
        not thread_id
        or "/" in thread_id
        or "\\" in thread_id
        or ".." in thread_id
        or thread_id in {"understanding"}
        or not _THREAD_ID_RE.match(thread_id)
    ):
        raise HTTPException(400, "invalid run id")

    run_dir = settings.run_dir(thread_id).resolve()
    if run_dir.parent != settings.runs_dir.resolve():
        raise HTTPException(400, "invalid run id")
    return run_dir


# --- landing / workspace -----------------------------------------------------


def _file_row(
    path: Path, role: str, role_label: str, date_label: str = "", doc_id: str = ""
) -> dict:
    """One line of the run's input list: what it is, when, how big.

    The doc id is shown because it is the name citations use — a chip reading
    E2.1 is only followable if something on screen says which file E2 is.
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "name": path.name,
        "doc_id": doc_id,
        "role": role,
        "role_label": role_label,
        "date_label": date_label,
        "size_k": round(size / 1000, 1),
        "editable": True,
    }


def workspace_context() -> dict:
    inputs = load_inputs(settings)
    docs = inputs["docs"]

    # Everything the run will read, in the order the pipeline reads it: the
    # reference records first, then the evidence oldest-first.
    files = [_file_row(settings.objective_file, "objective", "Objective record")]
    if settings.prior_update_file.exists():
        files.append(
            _file_row(settings.prior_update_file, "prior", "Previous quarter row")
        )
    files += [
        _file_row(
            Path(doc.source_path), "evidence", "Evidence", doc.date_label, doc.doc_id
        )
        for doc in docs
    ]

    return {
        "objective": inputs["objective"],
        "prior_update": inputs["prior_update"],
        "docs": docs,
        "files": files,
        "quarter": settings.quarter,
        "as_of": dt.date.today().isoformat(),
        "data_dir": settings.data_dir,
        "ready": bool(docs) and has_api_key(),
        "message": "",
        "error": "",
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def product_dashboard(request: Request):
    """Workspace-level view of every run. Distinct from a single draft snapshot."""
    index()
    runs = discover_runs()
    return render(
        request,
        "product.html",
        {
            "product": dashboard.from_workspace(runs, db.totals()),
            "runs": runs,
        },
        nav="product",
    )


@app.get("/", response_class=HTMLResponse)
async def runs_home(request: Request):
    runs = discover_runs()
    return render(
        request,
        "runs.html",
        {"runs": runs, "run_count": len(runs)},
        nav="runs",
    )


@app.get("/runs/new", response_class=HTMLResponse)
async def new_run(request: Request):
    try:
        context = workspace_context()
    except FileNotFoundError as error:
        return render(
            request,
            "error.html",
            {"message": str(error)},
            nav="new",
            status_code=500,
        )
    return render(request, "new.html", context, nav="new")


# Keep old / landing of workspace reachable via redirect if anything bookmarks it.
# Evidence CRUD still posts back to the workspace.


@app.post("/evidence/add")
async def add_evidence(
    request: Request,
    title: str = Form(default=""),
    date: str = Form(default=""),
    body: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
):
    added: list[str] = []
    try:
        for upload in files:
            if upload.filename:
                path = store.add_uploaded(
                    settings.evidence_dir, upload.filename, await upload.read()
                )
                added.append(path.name)

        if body.strip() or title.strip():
            path = store.add_pasted(settings.evidence_dir, title, body, date)
            added.append(path.name)

        if not added:
            raise store.StoreError("Nothing to add — paste some text or choose a file.")
    except store.StoreError as error:
        return _workspace(request, error=str(error))

    return _workspace(request, message=f"Added {', '.join(added)}.")


@app.post("/evidence/demo")
async def load_demo_pack(request: Request):
    """Copy the bundled evidence pack into the data folder.

    The dropzone used to print the folder the files land in, which told a
    director where the tool keeps its state and nothing about what to do next.
    A workspace someone has emptied needs a way back that is not a terminal,
    and that is what this is.

    Nothing is overwritten. Evidence files that clash are copied alongside
    under a new name, and the objective record and prior quarter are only
    written when they are missing — a director who has edited the objective
    must not lose it to a button labelled "load the demo pack".
    """
    added: list[str] = []
    try:
        for name, target in (
            ("objective.md", settings.objective_file),
            ("prior_update.md", settings.prior_update_file),
        ):
            source = DEMO_PACK_DIR / name
            if source.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                added.append(target.name)

        existing = {
            path.read_text(encoding="utf-8")
            for path in sorted(settings.evidence_dir.glob("*.md"))
        } if settings.evidence_dir.exists() else set()

        for source in sorted((DEMO_PACK_DIR / "evidence").glob("*.md")):
            text = source.read_text(encoding="utf-8")
            # Compared by content, not by name: loading the pack twice should
            # be a no-op, and a renamed copy of the same document is still the
            # same document as far as the reading pass is concerned.
            if text in existing:
                continue
            settings.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = store.unique_path(settings.evidence_dir, store.slugify(source.stem))
            path.write_text(text, encoding="utf-8")
            added.append(path.name)
    except OSError as error:
        return _workspace(request, error=f"Could not load the demo pack: {error}")

    if not added:
        return _workspace(request, message="The demo pack is already loaded.")
    return _workspace(request, message=f"Loaded {len(added)} demo files.")


@app.post("/evidence/{filename}/delete")
async def delete_evidence(request: Request, filename: str):
    try:
        store.remove(settings.evidence_dir, filename)
    except store.StoreError as error:
        return _workspace(request, error=str(error))
    return _workspace(request, message=f"Removed {filename}.")


@app.post("/evidence/{filename}/role")
async def set_evidence_role(request: Request, filename: str, role: str = Form(default="")):
    """Move a file between evidence, the objective record and the prior row.

    index.html holds files in the browser, so changing a role is a dropdown and
    nothing more. Here the role *is* the file's location, so the dropdown has
    to move it. The file it displaces is kept, renamed into the evidence folder
    rather than overwritten — a mis-click must not destroy the objective.
    """
    if role not in ("evidence", "objective", "prior"):
        return _workspace(request, error=f"{role!r} is not a role.")

    try:
        moved = store.set_role(settings, filename, role)
    except store.StoreError as error:
        return _workspace(request, error=str(error))

    return _workspace(request, message=moved)


@app.post("/runs/{thread_id}/citation")
async def dismiss_citation(
    thread_id: str, field: str = Form(default=""), ref: str = Form(default="")
):
    """Mark a citation irrelevant, or restore it.

    Recorded rather than deleted: a director judging a citation unhelpful is a
    fact about the retrieval, and it is the kind of thing worth reading in
    aggregate later.
    """
    run_dir = safe_run_dir(thread_id)
    dismissed = audit.dismissed_citations(audit.read(run_dir))
    now_dismissed = ref not in dismissed.get(field, set())
    audit.record_citation(run_dir, field=field, ref=ref, dismissed=now_dismissed)
    return JSONResponse({"field": field, "ref": ref, "dismissed": now_dismissed})


_ROLE_LABEL = {
    "objective": "Objective record",
    "prior": "Previous quarter row",
    "evidence": "Evidence",
}


@app.post("/evidence/preview")
async def preview_markdown(text: str = Form(default="")):
    """Sanitised HTML for the live markdown viewer on the edit screen."""
    blocks = md.blocks(text)
    html = "".join(f'<div class="md-block">{block["html"]}</div>' for block in blocks)
    if not html:
        html = '<p class="muted">Nothing to preview yet.</p>'
    return JSONResponse({"html": html})


@app.get("/evidence/{filename}/edit", response_class=HTMLResponse)
async def edit_evidence(request: Request, filename: str):
    try:
        path, role = store.resolve_workspace_document(settings, filename)
        text = path.read_text(encoding="utf-8")
    except (store.StoreError, OSError) as error:
        return render(
            request,
            "error.html",
            {
                "message": "Could not open that file",
                "detail": str(error),
            },
            nav="new",
            status_code=404,
        )
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return render(
        request,
        "edit.html",
        {
            "filename": filename,
            "text": text,
            "error": "",
            "role": role,
            "role_label": _ROLE_LABEL.get(role, role),
            "size_k": round(size / 1000, 1),
            "preview_blocks": md.blocks(text),
        },
        nav="new",
    )


@app.post("/evidence/{filename}/edit")
async def save_evidence(request: Request, filename: str, text: str = Form(default="")):
    role = "evidence"
    try:
        path, role = store.resolve_workspace_document(settings, filename)
        store.write_workspace_document(path, text)
    except store.StoreError as error:
        return render(
            request,
            "edit.html",
            {
                "filename": filename,
                "text": text,
                "error": str(error),
                "role": role,
                "role_label": _ROLE_LABEL.get(role, role),
                "size_k": round(len(text.encode("utf-8")) / 1000, 1),
                "preview_blocks": md.blocks(text),
            },
            nav="new",
            status_code=400,
        )
    except OSError as error:
        return render(
            request,
            "error.html",
            {"message": "Could not save that file", "detail": str(error)},
            nav="new",
            status_code=404,
        )
    return _workspace(request, message=f"Saved {filename}.")


@app.post("/objective")
async def save_objective(
    request: Request,
    Objective_ID: str = Form(default=""),
    Title: str = Form(default=""),
    Success_Measure: str = Form(default=""),
    Target_Completion: str = Form(default=""),
):
    existing = parse_field_table(settings.objective_file.read_text(encoding="utf-8"))
    store.update_objective(
        settings.objective_file,
        existing,
        {
            "Objective_ID": Objective_ID.strip() or existing.get("Objective_ID", ""),
            "Title": Title.strip(),
            "Success_Measure": Success_Measure.strip(),
            "Target_Completion": Target_Completion.strip(),
        },
    )
    return _workspace(request, message="Objective updated.")


def _workspace(request: Request, message: str = "", error: str = ""):
    """Re-render the new-run workspace.

    The workspace cannot be shown at all without an objective record, and
    there are ways to end up without one — an empty data folder, or moving the
    objective into evidence with the role dropdown. Every route that edits
    evidence posts back through here, so that state has to render as a page
    that says what is missing rather than as a 500.
    """
    try:
        context = workspace_context()
    except FileNotFoundError as missing:
        return render(
            request,
            "error.html",
            {"message": str(missing), "detail": error or ""},
            nav="new",
            status_code=500,
        )
    return render(
        request,
        "new.html",
        context | {"message": message, "error": error},
        nav="new",
        status_code=400 if error else 200,
    )


# --- generating a draft ------------------------------------------------------


@app.post("/runs")
async def create_run(
    request: Request, quarter: str = Form(default=""), as_of: str = Form(default="")
):
    """Run the graph to the review point and hand back a thread to review."""
    if not has_api_key():
        return _workspace(request, error="No API key loaded. Add one before drafting.")
    if not load_inputs(settings)["docs"]:
        return _workspace(request, error="There is no evidence to read. Add some first.")

    index()
    run_settings = settings.model_copy(update={"quarter": quarter} if quarter else {})
    thread_id = f"{dt.date.today():%Y%m%d}-{uuid.uuid4().hex[:6]}"

    # The as-of date is what days-remaining is counted from. Recorded on the
    # run rather than read from the clock at display time: a draft reviewed a
    # week later must not silently change the arithmetic it was written against.
    as_of_date = parse_date(as_of) or dt.date.today()
    audit.record_event(
        run_settings.run_dir(thread_id),
        {
            "stage": "created",
            "label": "Run created",
            "detail": f"quarter {run_settings.quarter} · "
            f"{len(load_inputs(run_settings)['docs'])} documents · "
            f"as of {as_of_date.isoformat()}",
            "as_of": as_of_date.isoformat(),
        },
    )

    # Recorded before the run starts, so a run that fails in its first pass is
    # still a run someone can find by quarter rather than by hex id.
    inputs = load_inputs(run_settings)
    objective = inputs["objective"]
    db.upsert_run(
        thread_id,
        created_at=audit.now(),
        status="running",
        quarter=run_settings.quarter,
        objective_id=objective.get("Objective_ID", ""),
        objective_title=objective.get("Title", ""),
        as_of=as_of_date.isoformat(),
        model=run_settings.resolved_model(),
        evidence_count=len(inputs["docs"]),
    )
    db.save_documents(
        thread_id,
        [
            {
                "doc_id": doc.doc_id,
                "filename": doc.filename,
                "title": doc.title,
                "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
                "blocks": len(doc.blocks),
            }
            for doc in inputs["docs"]
        ],
    )

    run = progress.registry.start(thread_id)
    run.task = asyncio.create_task(_execute(thread_id, run_settings, run))

    return RedirectResponse(f"/runs/{thread_id}", status_code=303)


async def _forget_run(thread_id: str, graph) -> None:
    """Stop a run if it is going, then remove every trace of it.

    Split out of the route so deleting one run and deleting all of them are
    the same operation — the all case opens the graph once and calls this per
    thread rather than reopening the checkpointer twenty times.
    """
    run = progress.registry.get(thread_id)
    if run is not None and run.task is not None and not run.task.done():
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
    progress.registry.forget(thread_id)

    await graph.checkpointer.adelete_thread(thread_id)
    db.forget_run(thread_id)

    run_dir = settings.run_dir(thread_id)
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)


@app.post("/runs/{thread_id}/delete")
async def delete_run(thread_id: str):
    """Remove a draft run. Evidence files under data/ are left alone."""
    safe_run_dir(thread_id)
    index()

    async with open_graph(graph_client(), settings) as graph:
        await _forget_run(thread_id, graph)

    return RedirectResponse("/", status_code=303)


@app.post("/runs/delete-all")
async def delete_all_runs():
    """Clear the workspace: every draft, every trail, every index row.

    Deliberately not a loop over the delete route. Each run has to be stopped
    before its checkpoint can be dropped, and doing that one HTTP request at a
    time meant a director clearing a dozen drafts clicked delete a dozen times
    and confirmed a dozen dialogs. The evidence folder is untouched, as it is
    for a single delete.
    """
    index()
    threads = [run["thread_id"] for run in discover_runs()]

    async with open_graph(graph_client(), settings) as graph:
        for thread_id in threads:
            try:
                safe_run_dir(thread_id)
            except HTTPException:
                continue
            await _forget_run(thread_id, graph)

    db.forget_all()
    return RedirectResponse("/", status_code=303)


async def _execute(thread_id: str, run_settings, run: progress.Run) -> None:
    """Run the graph, publishing each stage as it completes.

    Every published event is also written to the run's audit trail. The
    progress registry is in-memory and a restart empties it; the trail is the
    copy that is still there tomorrow.
    """
    seen: dict[str, int] = {}
    run_dir = run_settings.run_dir(thread_id)

    def publish(event: dict) -> None:
        run.publish(event)
        audit.record_event(run_dir, event)

    try:
        async with open_graph(run_client(run_dir), run_settings) as graph:
            async for chunk in graph.astream(
                load_inputs(run_settings),
                config=thread_config(thread_id),
                durability="sync",
                stream_mode="updates",
            ):
                for node, payload in chunk.items():
                    if node == "__interrupt__":
                        publish(progress.describe("review", payload, seen))
                    elif not node.startswith("__"):
                        publish(progress.describe(node, payload, seen))
        # Recorded before the run is announced finished, not after. `done` is
        # what flips the run to finished and what the loading screen reloads
        # on, so anything written afterwards is a race the review page can
        # lose — it would arrive to find no first version of its own draft.
        db.upsert_run(thread_id, status="review")
        await _snapshot(thread_id, "drafted")
        publish({"stage": "done", "label": "Draft ready", "detail": ""})
    except Exception as error:
        logger.exception("run %s failed", thread_id)
        publish(
            {
                "stage": "failed",
                "label": "The draft could not be completed",
                "detail": _readable(error),
            }
        )
        db.upsert_run(thread_id, status="failed")


async def _snapshot(thread_id: str, reason: str, row: DraftRow | None = None) -> None:
    """Record what the draft says right now.

    The checkpoint holds the current row and nothing else, so "what did this
    field say before I rewrote it" was a question the workspace could not
    answer. One row per movement here answers it, and answers it across runs:
    last quarter's approved draft is still readable after this quarter's has
    replaced it in the folder.

    Takes the row when the caller already has it — an edit does — and reads it
    back otherwise, so this can also be called from a place that only knows
    the thread.
    """
    index()
    if row is None:
        try:
            row = (await load_state(thread_id)).get("row")
        except HTTPException:
            return
    if row is None:
        return
    values = row.export_values()
    db.save_draft(thread_id, values, reason=reason)
    db.upsert_run(
        thread_id,
        quarter=values.get("Quarter"),
        objective_id=values.get("Objective_ID"),
        traffic_light=values.get("Traffic_Light"),
        progress_percent=values.get("Progress_Percent"),
    )


@app.get("/runs/{thread_id}/progress.json")
async def run_progress(thread_id: str):
    run = progress.registry.get(thread_id)
    if run is None:
        raise HTTPException(404, f"no run called {thread_id}")
    return JSONResponse({"finished": run.finished, "events": run.events})


@app.get("/runs/{thread_id}/events")
async def run_events(thread_id: str):
    run = progress.registry.get(thread_id)
    if run is None:
        raise HTTPException(404, f"no run called {thread_id}")
    return StreamingResponse(
        progress.stream(run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- review ------------------------------------------------------------------


async def load_state(thread_id: str) -> dict[str, Any]:
    async with open_graph(graph_client(), settings) as graph:
        snapshot = await graph.aget_state(thread_config(thread_id))
    if not snapshot.values:
        raise HTTPException(404, f"no run called {thread_id}")
    return dict(snapshot.values) | {"_awaiting_review": bool(snapshot.next)}


def _first_doc_id(state: dict) -> str | None:
    docs = state.get("docs") or []
    if not docs:
        return None
    return docs[0].doc_id


# The passes, in the order they run, for the loading screen. index.html has
# eight fixed stages; this pipeline has these. The row markup is the same.
PIPELINE_STAGES = [
    ("load", "Working out what each document is"),
    ("read_document", "Reading the evidence"),
    ("reconcile", "Reconciling what the documents disagree about"),
    ("assess", "Assessing against the success measure"),
    ("compose", "Writing the narrative"),
    ("validate", "Checking against the CSF schema"),
    ("review", "Ready for review"),
]


def _stage_rows(events: list[dict]) -> list[dict]:
    """Each pass with its state, so the loading screen can show the sequence.

    The sequence is the argument for the design — five documents read
    separately, then reconciled, then assessed — and this is the one place a
    director sees it.

    An event means that node just finished. Reading fires once per document,
    so that row stays active until reconciliation starts rather than jumping
    ahead after the first file.
    """
    seen = {e.get("stage"): e for e in events}
    failed = "failed" in seen
    keys = [key for key, _ in PIPELINE_STAGES]
    repeating = {"read_document"}
    finished = [e.get("stage") for e in events if e.get("stage") in set(keys)]
    latest = finished[-1] if finished else None

    def state_for(key: str) -> str:
        if failed:
            return "done" if key in seen else "pend"
        if latest is None:
            return "active" if key == keys[0] else "pend"
        here, last = keys.index(key), keys.index(latest)
        if latest in repeating:
            if here < last:
                return "done"
            return "active" if here == last else "pend"
        if here <= last:
            return "done"
        if here == last + 1:
            return "active"
        return "pend"

    rows = []
    for key, label in PIPELINE_STAGES:
        rows.append(
            {
                "key": key,
                "label": label,
                "detail": (seen.get(key) or {}).get("detail", ""),
                "state": state_for(key),
            }
        )

    if failed:
        rows.append(
            {
                "key": "failed",
                "label": seen["failed"].get("label", "Stopped"),
                "detail": seen["failed"].get("detail", ""),
                "state": "fail",
            }
        )
    return rows


@app.get("/runs/{thread_id}", response_class=HTMLResponse)
async def review(request: Request, thread_id: str):
    run = progress.registry.get(thread_id)

    ledger = audit.read(settings.run_dir(thread_id))

    if run is not None and not run.finished:
        return render(
            request,
            "running.html",
            {
                "thread_id": thread_id,
                "stages": _stage_rows(run.events),
                "as_of": parse_date(audit.as_of(ledger)) or dt.date.today(),
            },
            nav="review",
            thread_id=thread_id,
        )

    if run is not None and any(e["stage"] == "failed" for e in run.events):
        failure = next(e for e in run.events if e["stage"] == "failed")
        stages = _stage_rows(run.events)
        last = next((s["label"] for s in reversed(stages) if s["state"] == "done"), "")
        return render(
            request,
            "error.html",
            {
                "message": failure["label"],
                "detail": failure["detail"],
                "thread_id": thread_id,
                "stages": stages,
                "failed_stage_label": last or "the pipeline",
            },
            nav="review",
            thread_id=thread_id,
            status_code=500,
        )

    try:
        state = await load_state(thread_id)
    except HTTPException as error:
        return render(
            request,
            "error.html",
            {
                "message": "Could not open this run",
                "detail": error.detail if isinstance(error.detail, str) else str(error.detail),
                "thread_id": thread_id,
            },
            nav="review",
            thread_id=thread_id,
            status_code=error.status_code,
        )

    context = review_context(thread_id, state)
    return render(
        request,
        "review.html",
        context,
        nav="review",
        thread_id=thread_id,
        staged_path=context.get("staged_path"),
        first_doc_id=context.get("first_doc_id"),
    )


@app.get("/runs/{thread_id}/dashboard", response_class=HTMLResponse)
async def draft_dashboard(request: Request, thread_id: str):
    """At-a-glance snapshot of this draft. Not the institute reporting model.

    Always HTML. A run still in flight has no row yet; the page says so
    rather than 404, which reads as the snapshot itself being missing.
    """
    index()
    run = progress.registry.get(thread_id)
    known = db.run(thread_id) or {}
    pending = (run is not None and not run.finished) or known.get("status") == "running"
    if not known:
        known = {"thread_id": thread_id, "status": "running" if pending else ""}
    state = None
    try:
        state = await load_state(thread_id)
    except HTTPException:
        state = None

    if state and (state.get("row") or state.get("objective")):
        context = review_context(thread_id, state)
        if context.get("dash") is None:
            context["dash"] = dashboard.from_index(known, pending=pending)
        elif pending:
            context["dash"]["pending"] = True
    else:
        context = {
            "dash": dashboard.from_index(known, pending=pending),
            "objective": {
                "Title": known.get("objective_title") or "",
                "Objective_ID": known.get("objective_id") or "",
            },
            "thread_id": thread_id,
        }

    return render(
        request,
        "dashboard.html",
        context,
        nav="dashboard",
        thread_id=thread_id,
        staged_path=context.get("staged_path"),
        first_doc_id=context.get("first_doc_id"),
    )


# How each reconciliation rule reads as a sentence. The outcome without the
# reasoning is not reviewable: a director who disagrees needs the rule named.
RULE_SENTENCE = {
    "later_supersedes_earlier": "recency — the later account wins where both are"
    " the same class of source",
    "participant_supersedes_outbound": "a first-hand account from someone in the"
    " room outranks a document written for an outside audience",
    "other": "no standard rule decides this pair — the reconciler explains it below",
}


def finding_id(kind: str, *parts: str) -> str:
    """A stable id for something the run found.

    Conflicts and gaps carry no id of their own — they are model output, not
    records. Hashing their content gives an id that survives a page reload and
    a server restart without inventing a field the schema does not have, and
    without depending on list position.
    """
    # Hyphenated, not colon-separated: the id becomes an element id and a
    # URL fragment, and a colon there is legal HTML but not a CSS selector.
    digest = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{kind}-{digest}"



def _findings(state: dict, acked: set[str]) -> dict:
    """Conflicts and gaps, normalised into what the attention panel renders.

    Severity comes from the reconcile pass, not from here. It is the only step
    that has read every account of the same event, so it is the only one that
    can tell a scope change from a routine correction — a rule in this function
    would only ever be guessing from the shape of the output.
    """
    conflicts = []
    for conflict in state.get("conflicts", []):
        ident = finding_id(
            "conflict",
            conflict.topic,
            conflict.winning_claim_id,
            *conflict.superseded_claim_ids,
        )
        conflicts.append(
            {
                "id": ident,
                "severity": conflict.severity,
                "severity_reason": conflict.severity_reason,
                "topic": conflict.topic,
                "note": conflict.note,
                "winning_claim_id": conflict.winning_claim_id,
                "superseded_claim_ids": conflict.superseded_claim_ids,
                "resolved_by": RULE_SENTENCE.get(
                    conflict.rule_applied, conflict.rule_applied.replace("_", " ")
                ),
                "acknowledged": ident in acked,
            }
        )

    gaps = []
    for gap in state.get("gaps", []):
        ident = finding_id("gap", gap.topic, *gap.raised_by_claim_ids)
        gaps.append(
            {
                "id": ident,
                "severity": gap.severity,
                "severity_reason": gap.severity_reason,
                "topic": gap.topic,
                "note": gap.note,
                "bears_on": gap.bears_on,
                "raised_by_claim_ids": gap.raised_by_claim_ids,
                "acknowledged": ident in acked,
            }
        )

    return {"conflicts": conflicts, "gaps": gaps}


def _days_remaining(objective: dict, as_of: dt.date) -> int | None:
    """Days from the run's as-of date to the target completion, if it has one."""
    target = parse_date(objective.get("Target_Completion", "") or "")
    if target is None:
        return None
    return (target - as_of).days


def _staged_at(thread_id: str) -> str:
    path = settings.run_dir(thread_id) / "staged_row.json"
    if not path.exists():
        return ""
    return dt.datetime.fromtimestamp(
        path.stat().st_mtime, tz=dt.timezone.utc
    ).isoformat(timespec="seconds")


def review_context(thread_id: str, state: dict) -> dict:
    row: DraftRow | None = state.get("row")
    docs = {doc.doc_id: doc for doc in state.get("docs", [])}
    claims = {claim.claim_id: claim for claim in state.get("claims", [])}
    first_doc = next(iter(docs), None)
    proposals = list(row.proposals().items()) if row else []
    acknowledged_count = sum(1 for _, p in proposals if p.edited_by_director)
    issues = state.get("issues", [])
    objective = state.get("objective", {})
    corrections = state.get("corrections", [])
    ledger = audit.read(settings.run_dir(thread_id))
    findings = _findings(state, audit.acknowledged(ledger))
    as_of_date = parse_date(audit.as_of(ledger)) or dt.date.today()

    ctx = {
        "thread_id": thread_id,
        # What the tool first proposed, per field, so an overridden value can
        # show what it replaced instead of quietly becoming the only answer.
        "proposed": {c.field: c.proposed_value for c in corrections},
        "override_reason": {c.field: getattr(c, "reason", "") for c in corrections},
        "dismissed_cites": audit.dismissed_citations(ledger),
        # index.html renders a success-measure component table here. This
        # pipeline does not decompose the measure, so the table renders nothing
        # rather than being replaced with a substitute for it.
        "assessments": [],
        "conflicts": findings["conflicts"],
        "gaps": findings["gaps"],
        "staged_at": _staged_at(thread_id),
        "findings": findings,
        "unacknowledged_findings": sum(
            1
            for group in findings.values()
            for item in group
            if not item["acknowledged"]
        ),
        "days_remaining": _days_remaining(objective, as_of_date),
        "as_of": as_of_date,
        "rule_sentence": RULE_SENTENCE,
        "row": row,
        "fields": proposals,
        "acknowledged_count": acknowledged_count,
        "all_acknowledged": bool(proposals)
        and acknowledged_count == len(proposals),
        "blockers": _blockers(proposals),
        "objective": state.get("objective", {}),
        "prior_update": state.get("prior_update", {}),
        "reconciled_position": state.get("reconciled_position", ""),
        "issues": issues,
        "errors": errors(issues),
        "advice": advice(issues),
        "claims": claims,
        "docs": docs,
        "first_doc_id": first_doc,
        "staged_path": state.get("staged_path"),
        "awaiting_review": state.get("_awaiting_review", False),
        "traffic_lights": vocab.TRAFFIC_LIGHTS,
        "support_from_options": vocab.SUPPORT_FROM,
        "significant_fields": vocab.SIGNIFICANT_FIELDS,
        "max_chars": vocab.NARRATIVE_MAX_CHARS,
        "corrections": state.get("corrections", []),
    }
    ctx["dash"] = dashboard.from_review(ctx) if row else None
    return ctx


def _blockers(proposals: list) -> list[str]:
    """What actually stands between this draft and approval, named.

    Only the acknowledgement gate belongs here, because that is the only thing
    `_stage_run` refuses on. A validation error is shown in the attention panel
    and an over-long narrative is advice — the director may want to submit it
    and trim it themselves. Listing either here would describe a gate that does
    not exist, and a footer that cries wolf is worse than one that says nothing.
    """
    pending = [name for name, proposal in proposals if not proposal.edited_by_director]
    if not pending:
        return []
    return [
        f"{len(pending)} field{'' if len(pending) == 1 else 's'} still to acknowledge: "
        + ", ".join(name.replace("_", " ") for name in pending)
    ]


@app.post("/runs/{thread_id}/field/{field}", response_class=HTMLResponse)
async def edit_field(request: Request, thread_id: str, field: str):
    """Acknowledge a field, update it, or toggle back to the draft proposal."""
    state = await load_state(thread_id)
    row: DraftRow | None = state.get("row")
    if row is None or field not in row.proposals():
        raise HTTPException(404, f"no field called {field}")

    form = await request.form()
    value = _coerce(field, form)
    proposal = getattr(row, field)
    corrections = list(state.get("corrections", []))
    existing = next((c for c in corrections if c.field == field), None)
    run_dir = settings.run_dir(thread_id)
    before = proposal.value

    # Second press with the same value clears the acknowledgement and restores
    # the draft proposal so export locks again until every field is re-acked.
    if proposal.edited_by_director and _same_field_value(field, proposal.value, value):
        corrections = [c for c in corrections if c.field != field]
        if existing is not None:
            proposal.value = existing.proposed_value
        proposal.edited_by_director = False
        if field in vocab.SIGNIFICANT_FIELDS and proposal.value is None:
            proposal.needs_director_input = True
        audit.record_edit(
            run_dir,
            field=field,
            edit_kind="acknowledgement_cleared",
            before=before,
            after=proposal.value,
            claim_ids_shown=proposal.claim_ids,
        )
    else:
        reason = (form.get("reason") or "").strip()
        correction = Correction(
            field=field,
            proposed_value=(
                existing.proposed_value if existing is not None else proposal.value
            ),
            director_value=value,
            claim_ids_shown=proposal.claim_ids,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            # Carried forward on a re-edit: the reasoning travels with the row,
            # and a second tweak to the same field does not erase why it moved.
            reason=reason or (getattr(existing, "reason", "") if existing else ""),
        )
        corrections = [c for c in corrections if c.field != field]
        corrections.append(correction)
        proposal.value = value
        proposal.edited_by_director = True
        proposal.needs_director_input = False
        # Append-only: the corrections list keeps one row per field, so ten
        # edits to Key_Challenge would otherwise leave one. The trail keeps ten.
        audit.record_edit(
            run_dir,
            field=field,
            edit_kind=audit.describe_edit(field, before, value),
            before=before,
            after=value,
            claim_ids_shown=proposal.claim_ids,
            reason=correction.reason,
        )

    async with open_graph(graph_client(), settings) as graph:
        await graph.aupdate_state(
            thread_config(thread_id), {"row": row, "corrections": corrections}
        )
    await _snapshot(thread_id, f"edited {field}", row)

    # A plain redirect, in both cases. index.html re-renders the whole review
    # after any edit — a field's state is not local to itself, because the
    # approve gate and the Source projection both move with it — so swapping
    # one card in place would leave the rest of the page describing the draft
    # as it was a moment ago.
    return RedirectResponse(f"/runs/{thread_id}#field-{field}", status_code=303)


def _same_field_value(field: str, left: Any, right: Any) -> bool:
    if field == "Support_From":
        return list(left or []) == list(right or [])
    return left == right


def _coerce(field: str, form) -> Any:
    if field == "Progress_Percent":
        raw = (form.get(field) or "").strip()
        try:
            return max(0, min(100, int(raw)))
        except ValueError:
            return None
    if field == "Support_From":
        return [v for v in form.getlist(field) if v in vocab.SUPPORT_FROM]
    value = (form.get(field) or "").strip()
    return value or None


@app.post("/runs/{thread_id}/finding/{ident}")
async def acknowledge_finding(
    request: Request, thread_id: str, ident: str, title: str = Form(default="")
):
    """Mark a conflict or gap as seen, or unmark it.

    Deliberately not part of the approval gate. The gate is field-level
    acknowledgement, and adding a second one here would let a director unlock
    export by clicking through findings without reading a field.
    """
    run_dir = safe_run_dir(thread_id)
    already = ident in audit.acknowledged(audit.read(run_dir))
    audit.record_ack(run_dir, finding_id=ident, title=title, acked=not already)
    return RedirectResponse(f"/runs/{thread_id}#finding-{ident}", status_code=303)


@app.post("/runs/{thread_id}/acknowledge-all")
async def acknowledge_all_fields_route(thread_id: str):
    """Toggle: acknowledge every field, or clear all acknowledgements."""
    state = await load_state(thread_id)
    row: DraftRow | None = state.get("row")
    if row is None:
        raise HTTPException(404, "no draft to acknowledge")

    proposals = row.proposals()
    all_acked = bool(proposals) and all(p.edited_by_director for p in proposals.values())
    corrections = list(state.get("corrections", []))
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    run_dir = settings.run_dir(thread_id)
    audit.record_edit(
        run_dir,
        field="(every field)",
        edit_kind="acknowledgement_cleared" if all_acked else "acknowledged",
        before=f"{sum(1 for p in proposals.values() if p.edited_by_director)} acknowledged",
        after="0 acknowledged" if all_acked else f"{len(proposals)} acknowledged",
    )

    if all_acked:
        # Second press — restore draft proposals and lock export again.
        kept: list[Correction] = []
        for field, proposal in proposals.items():
            existing = next((c for c in corrections if c.field == field), None)
            if existing is not None:
                proposal.value = existing.proposed_value
            proposal.edited_by_director = False
            if field in vocab.SIGNIFICANT_FIELDS and proposal.value is None:
                proposal.needs_director_input = True
        corrections = kept
    else:
        for field, proposal in proposals.items():
            if proposal.edited_by_director:
                continue
            existing = next((c for c in corrections if c.field == field), None)
            corrections = [c for c in corrections if c.field != field]
            corrections.append(
                Correction(
                    field=field,
                    proposed_value=(
                        existing.proposed_value
                        if existing is not None
                        else proposal.value
                    ),
                    director_value=proposal.value,
                    claim_ids_shown=proposal.claim_ids,
                    timestamp=stamp,
                )
            )
            proposal.edited_by_director = True
            proposal.needs_director_input = False

    async with open_graph(graph_client(), settings) as graph:
        await graph.aupdate_state(
            thread_config(thread_id), {"row": row, "corrections": corrections}
        )
    await _snapshot(
        thread_id, "cleared every acknowledgement" if all_acked else "acknowledged every field", row
    )

    return RedirectResponse(f"/runs/{thread_id}", status_code=303)


# --- staging / approve -------------------------------------------------------


async def _stage_run(thread_id: str) -> None:
    if not has_api_key():
        raise HTTPException(400, "No API key loaded. Add one before approving.")
    state = await load_state(thread_id)
    row: DraftRow | None = state.get("row")
    if row is None:
        raise HTTPException(400, "No draft to approve.")
    pending = [
        name for name, proposal in row.proposals().items() if not proposal.edited_by_director
    ]
    if pending:
        raise HTTPException(
            400,
            "Acknowledge every field before approving for export. "
            f"Still needed: {', '.join(pending)}.",
        )
    corrections = [c.model_dump() for c in state.get("corrections", [])]
    run_dir = settings.run_dir(thread_id)

    async with open_graph(run_client(run_dir), settings) as graph:
        await graph.ainvoke(
            Command(resume={"corrections": corrections}),
            config=thread_config(thread_id),
            durability="sync",
        )

    audit.record_event(
        run_dir,
        {
            "stage": "approved",
            "label": "Approved for export",
            "detail": f"{len(corrections)} field "
            f"{'correction' if len(corrections) == 1 else 'corrections'} carried into the "
            "staged row · nothing was submitted",
        },
    )
    await _snapshot(thread_id, "approved for export", row)
    db.upsert_run(thread_id, status="staged", staged_at=_staged_at(thread_id))


@app.post("/runs/{thread_id}/stage")
async def stage(thread_id: str):
    """Resume past review and write the staged row (legacy path name)."""
    await _stage_run(thread_id)
    return RedirectResponse(f"/runs/{thread_id}/export", status_code=303)


@app.post("/runs/{thread_id}/approve")
async def approve(thread_id: str):
    """SPA wording for stage — approve for export, never submit."""
    await _stage_run(thread_id)
    return RedirectResponse(f"/runs/{thread_id}/export", status_code=303)


async def _require_acknowledged(thread_id: str) -> dict[str, Any]:
    """Load run state and refuse export until every field is acknowledged."""
    state = await load_state(thread_id)
    row: DraftRow | None = state.get("row")
    pending = [
        name
        for name, proposal in (row.proposals().items() if row else [])
        if not proposal.edited_by_director
    ]
    if pending:
        raise HTTPException(
            400,
            "Acknowledge every field before export. "
            f"Still needed: {', '.join(pending)}.",
        )
    return state


@app.get("/runs/{thread_id}/staged.json")
async def staged_json(thread_id: str):
    path = settings.run_dir(thread_id) / "staged_row.json"
    if not path.exists():
        raise HTTPException(404, "nothing staged for this run yet")
    await _require_acknowledged(thread_id)
    return JSONResponse(
        flatten_staged_row(json.loads(path.read_text(encoding="utf-8")))
    )


@app.get("/runs/{thread_id}/export", response_class=HTMLResponse)
async def export_page(request: Request, thread_id: str):
    path = settings.run_dir(thread_id) / "staged_row.json"
    if not path.exists():
        return render(
            request,
            "export.html",
            {
                "thread_id": thread_id,
                "approved": False,
                "row": None,
                "staged_at": None,
            },
            nav="export",
            thread_id=thread_id,
        )

    try:
        state = await _require_acknowledged(thread_id)
    except HTTPException:
        return RedirectResponse(f"/runs/{thread_id}", status_code=303)

    row = flatten_staged_row(json.loads(path.read_text(encoding="utf-8")))
    first_doc = _first_doc_id(state)

    return render(
        request,
        "export.html",
        {
            "thread_id": thread_id,
            "approved": True,
            "row": row,
            "staged_path": str(path),
            "staged_at": dt.datetime.fromtimestamp(
                path.stat().st_mtime, tz=dt.timezone.utc
            ).isoformat(timespec="seconds"),
            "first_doc_id": first_doc,
            "all_acknowledged": True,
        },
        nav="export",
        thread_id=thread_id,
        staged_path=str(path),
        first_doc_id=first_doc,
    )


@app.get("/runs/{thread_id}/export.csv")
async def export_csv(thread_id: str):
    path = settings.run_dir(thread_id) / "staged_row.json"
    if not path.exists():
        raise HTTPException(404, "nothing staged for this run yet")
    await _require_acknowledged(thread_id)
    row = flatten_staged_row(json.loads(path.read_text(encoding="utf-8")))
    # Prefer SharePoint column order; drop internal bookkeeping from CSV.
    preferred = [
        "Objective_ID",
        "Quarter",
        "Traffic_Light",
        "Progress_Percent",
        "Key_Success",
        "Key_Challenge",
        "Support_Needed",
        "Support_From",
        "Source",
    ]
    keys = [k for k in preferred if k in row] + [
        k for k in row if k not in preferred and k not in {"submitted", "staged_at", "thread_id"}
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(keys)
    writer.writerow(
        [
            "; ".join(str(v) for v in row[k])
            if isinstance(row[k], list)
            else ("" if row[k] is None else str(row[k]))
            for k in keys
        ]
    )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{thread_id}-row.csv"'
        },
    )


def _audit_context(thread_id: str) -> dict:
    """The three ledgers behind the audit page.

    Events come from the trail on disk, not the in-memory registry, so the
    page reads the same after a restart as it did during the run. A live run
    is merged in on top because its last few events may not have landed yet.
    """
    run_dir = settings.run_dir(thread_id)
    rows = audit.read(run_dir)

    events = audit.of_kind(rows, "event")
    run = progress.registry.get(thread_id)
    if run is not None and len(run.events) > len(events):
        seen = {(e.get("stage"), e.get("label"), e.get("detail")) for e in events}
        events = events + [
            {"at": "", "kind": "event", "status": "progress", **event}
            for event in run.events
            if (event.get("stage"), event.get("label"), event.get("detail")) not in seen
        ]

    index()
    return {
        "events": events,
        "llm_calls": audit.of_kind(rows, "llm_call"),
        "edits": audit.of_kind(rows, "edit"),
        "acks": audit.of_kind(rows, "ack"),
        "cites": audit.of_kind(rows, "citation"),
        "counts": audit.counts(rows),
        # Every version of the row this run has held, newest first. The trail
        # says a field changed; this says what it changed to.
        "snapshots": db.drafts(thread_id),
        "run_row": db.run(thread_id) or {},
        "documents": db.documents(thread_id),
    }


@app.get("/audit", response_class=HTMLResponse)
async def workspace_audit(request: Request, run: str = ""):
    """The audit trail across every run, not just the one being reviewed.

    Reachable from the sidebar from the first page load, before any run exists.
    Putting it behind a run made it something a director found only after they
    had already drafted — and the first question anyone asks of a tool that
    writes on their behalf is what it did, which is exactly this page.
    """
    index()
    return render(
        request,
        "audit_all.html",
        {
            "totals": db.totals(),
            "runs": db.runs(),
            "events": db.audit_rows(run or None, "event", limit=300),
            "llm_calls": db.audit_rows(run or None, "llm_call", limit=300),
            "edits": db.audit_rows(run or None, "edit", limit=300),
            "acks": db.audit_rows(run or None, "ack", limit=300),
            "snapshots": db.drafts(run or None, limit=200),
            "filter_run": run,
            "db_path": settings.workspace_db,
        },
        nav="workspace-audit",
    )


@app.get("/runs/{thread_id}/audit", response_class=HTMLResponse)
async def audit_page(request: Request, thread_id: str):
    ledgers = _audit_context(thread_id)

    corrections: list[Any] = []
    staged_path = None
    first_doc = None
    try:
        state = await load_state(thread_id)
        corrections = state.get("corrections") or []
        staged_path = state.get("staged_path")
        first_doc = _first_doc_id(state)
    except HTTPException:
        staged = settings.run_dir(thread_id) / "staged_row.json"
        if staged.exists():
            staged_path = str(staged)
        corrections_path = settings.run_dir(thread_id) / "corrections.jsonl"
        if corrections_path.exists():
            for line in corrections_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        corrections.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    return render(
        request,
        "audit.html",
        {
            "thread_id": thread_id,
            "corrections": corrections,
            "staged_path": staged_path,
            "first_doc_id": first_doc,
            **ledgers,
        },
        nav="audit",
        thread_id=thread_id,
        staged_path=staged_path,
        first_doc_id=first_doc,
    )


@app.get("/runs/{thread_id}/audit.md")
async def audit_markdown(thread_id: str):
    ledgers = _audit_context(thread_id)
    counts = ledgers["counts"]

    lines = [f"# Evidence trail — {thread_id}", ""]
    lines.append(
        f"{counts['events']} pipeline events · {counts['llm_calls']} model calls "
        f"({counts['llm_input_tokens']} in / {counts['llm_output_tokens']} out) · "
        f"{counts['edits']} director edits"
    )
    lines.append("")

    if ledgers["events"]:
        lines.append("## Pipeline events")
        for event in ledgers["events"]:
            detail = f" — {event['detail']}" if event.get("detail") else ""
            stamp = f"`{event.get('at', '')}` " if event.get("at") else ""
            lines.append(f"- {stamp}**{event.get('label', event.get('stage'))}**{detail}")
        lines.append("")

    if ledgers["llm_calls"]:
        lines.append("## Model calls")
        lines.append("")
        lines.append("| At | Stage | Prompt | Model | In | Out | Latency | Stop |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for call in ledgers["llm_calls"]:
            lines.append(
                f"| {call.get('at', '')} | {call.get('stage', '')} | "
                f"{call.get('prompt_name', '')} | `{call.get('model', '')}` | "
                f"{call.get('input_tokens') if call.get('input_tokens') is not None else '—'} | "
                f"{call.get('output_tokens') if call.get('output_tokens') is not None else '—'} | "
                f"{call.get('latency_ms', '—')} ms | {call.get('stop_reason') or '—'} |"
            )
        lines.append("")
        lines.append(
            "No prompt or response body is stored. The trail records what happened, "
            "not the evidence text a second time."
        )
        lines.append("")

    if ledgers["edits"]:
        lines.append("## Director edits (append-only)")
        for edit in ledgers["edits"]:
            distance = edit.get("char_distance")
            suffix = f" · distance {distance}" if distance is not None else ""
            shown = edit.get("claim_ids_shown") or []
            evidence = f" · evidence on screen: {', '.join(shown)}" if shown else ""
            lines.append(
                f"- `{edit.get('at', '')}` **{edit.get('field')}** · "
                f"{edit.get('edit_kind')}{suffix}{evidence}"
            )
        lines.append("")

    corrections: list[Any] = []
    try:
        state = await load_state(thread_id)
        corrections = state.get("corrections") or []
    except HTTPException:
        path = settings.run_dir(thread_id) / "corrections.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        corrections.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    if corrections:
        lines.append("## Director corrections")
        for c in corrections:
            if hasattr(c, "model_dump"):
                c = c.model_dump()
            lines.append(
                f"- **{c.get('field')}** proposed `{c.get('proposed_value')}` → "
                f"director `{c.get('director_value')}` ({c.get('timestamp', '')})"
            )
        lines.append("")

    staged = settings.run_dir(thread_id) / "staged_row.json"
    if staged.exists():
        lines.append("## Staging")
        lines.append(f"- Staged row written at `{staged}`")
        lines.append("- Source remains Substrate-Drafted — nothing was submitted.")

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{thread_id}-audit.md"'
        },
    )


@app.get("/runs/{thread_id}/state.json")
async def state_json(thread_id: str):
    """The proposed row as JSON, for anyone who would rather read the data."""
    state = await load_state(thread_id)
    row: DraftRow | None = state.get("row")
    return JSONResponse(
        {
            "thread_id": thread_id,
            "awaiting_review": state.get("_awaiting_review", False),
            "row": row.model_dump() if row else None,
            "conflicts": [c.model_dump() for c in state.get("conflicts", [])],
            "gaps": [g.model_dump() for g in state.get("gaps", [])],
            "issues": [i.model_dump() for i in state.get("issues", [])],
            "claims": [c.model_dump() for c in state.get("claims", [])],
        }
    )


# --- evidence ----------------------------------------------------------------


@app.get("/runs/{thread_id}/evidence/{doc_id}", response_class=HTMLResponse)
async def evidence(
    request: Request, thread_id: str, doc_id: str, claim: str = "", back: str = ""
):
    run = progress.registry.get(thread_id)
    index()
    known = db.run(thread_id) or {}
    pending = (run is not None and not run.finished) or known.get("status") == "running"
    try:
        context = await _source_context(thread_id, doc_id, claim, back=back)
    except HTTPException as error:
        if pending:
            return render(
                request,
                "evidence_pending.html",
                {"thread_id": thread_id},
                nav="evidence",
                thread_id=thread_id,
            )
        return render(
            request,
            "error.html",
            {
                "message": "Could not open this evidence",
                "detail": error.detail if isinstance(error.detail, str) else str(error.detail),
                "thread_id": thread_id,
            },
            nav="evidence",
            thread_id=thread_id,
            status_code=error.status_code,
        )
    return render(
        request,
        "evidence.html",
        context,
        nav="evidence",
        thread_id=thread_id,
        staged_path=context.get("staged_path"),
        first_doc_id=context.get("first_doc_id"),
    )


async def _source_context(
    thread_id: str, doc_id: str, claim: str, back: str = ""
) -> dict:
    state = await load_state(thread_id)
    docs = {doc.doc_id: doc for doc in state.get("docs", [])}
    doc = docs.get(doc_id)
    if doc is None:
        raise HTTPException(404, f"no document called {doc_id}")

    claims = {c.claim_id: c for c in state.get("claims", [])}
    requested = [claims[cid] for cid in _split(claim) if cid in claims]

    highlighted = {
        index
        for cited_claim in requested
        for citation in cited_claim.citations
        if citation.doc_id == doc_id
        for index in citation.block_indices
    }
    cited_lines = _cited_lines(doc, highlighted)
    original = _document_text(doc)

    return {
        "thread_id": thread_id,
        "doc": doc,
        "docs": docs,
        "first_doc_id": next(iter(docs), None),
        "staged_path": state.get("staged_path"),
        "cited_claims": requested,
        "highlighted": highlighted,
        # The document as a person would read it: markdown rendered, with the
        # cited blocks still marked. The numbered source below is what a
        # citation is checked against; this is what the document says.
        "rendered": (
            md.blocks(original, cited_lines)
            if original
            else [
                {
                    "html": md.to_html(block.text),
                    "line_start": block.line_start,
                    "line_end": block.line_end,
                    "cited": position in highlighted,
                }
                for position, block in enumerate(doc.blocks)
            ]
        ),
        # The document as it was written, with the cited lines marked. A block
        # list shows what the model was sent; this shows what a person wrote,
        # which is what someone checking a citation came here to see.
        "source_lines": _source_lines(doc, cited_lines, highlighted),
        "cited_lines": cited_lines,
        "first_cited_line": min(cited_lines) if cited_lines else None,
        "back": _safe_back(back) or f"/runs/{thread_id}",
    }


def _cited_lines(doc, highlighted: set[int]) -> set[int]:
    """Line numbers covered by the cited blocks."""
    lines: set[int] = set()
    for index in highlighted:
        if 0 <= index < len(doc.blocks):
            block = doc.blocks[index]
            lines.update(range(block.line_start, block.line_end + 1))
    return lines


def _document_text(doc) -> str:
    """The original file, or nothing if it has been removed since the run."""
    if not doc.source_path:
        return ""
    try:
        return Path(doc.source_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _source_lines(doc, cited_lines: set[int], highlighted: set[int]) -> list[dict]:
    """Numbered lines of the original file, falling back to the blocks.

    Read from disk when the file is still there, because the line numbers the
    blocks carry only mean anything against the original text. If the file has
    gone, the blocks are laid out in their own order rather than pretending to
    line numbers that no longer refer to anything.
    """
    text = _document_text(doc)

    if text:
        return [
            {"n": number, "text": line, "cited": number in cited_lines}
            for number, line in enumerate(text.splitlines(), start=1)
        ]

    return [
        {"n": block.line_start, "text": block.text, "cited": index in highlighted}
        for index, block in enumerate(doc.blocks)
    ]


def _safe_back(back: str) -> str:
    """Only ever return to this app. An open redirect is not worth a back link."""
    if back.startswith("/") and not back.startswith("//"):
        return back
    return ""


# The source fragment endpoint is gone with the evidence rail it fed.
# index.html has no rail: a citation opens the document full-page, with the
# cited lines marked and a link back to the field it was clicked from.


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
