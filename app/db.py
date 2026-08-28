"""Everything that happened, in one file you can query.

The JSONL trail next to each run is still the record of truth: it is written
first, it survives a corrupt database, and reading it needs nothing but the
filesystem. This module is the *index* over those records — one SQLite file
under `runs/`, holding every run, every audit row, and a snapshot of the draft
row each time it moved.

That is the part the trail could not do. A ledger per folder answers "what
happened in this run"; it cannot answer "what did the previous quarter's draft
say before the director changed it", or "show me every override across every
run", without walking every directory and parsing every line. Those are the
questions a workspace gets asked once it has more than one draft in it.

Every write is best-effort, for the same reason the trail's writes are: an
index that cannot be written must cost a page of history, never a run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("csf-drafter")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    thread_id        TEXT PRIMARY KEY,
    created_at       TEXT,
    updated_at       TEXT,
    status           TEXT,
    quarter          TEXT,
    objective_id     TEXT,
    objective_title  TEXT,
    as_of            TEXT,
    model            TEXT,
    evidence_count   INTEGER,
    traffic_light    TEXT,
    progress_percent INTEGER,
    staged_at        TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    stage     TEXT,
    field     TEXT,
    label     TEXT,
    detail    TEXT,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_by_run  ON audit (thread_id, id);
CREATE INDEX IF NOT EXISTS audit_by_kind ON audit (kind, id);

-- One row per time the draft moved. Append-only, like the trail: the point is
-- to be able to read what a field said three edits ago.
CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id        TEXT NOT NULL,
    at               TEXT NOT NULL,
    reason           TEXT,
    quarter          TEXT,
    objective_id     TEXT,
    traffic_light    TEXT,
    progress_percent INTEGER,
    row              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_by_run ON drafts (thread_id, id);

-- What each run actually read, kept because the evidence folder is editable
-- and a draft is only reviewable against the documents it was drafted from.
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL,
    doc_id     TEXT,
    filename   TEXT,
    title      TEXT,
    doc_date   TEXT,
    blocks     INTEGER
);
CREATE INDEX IF NOT EXISTS documents_by_run ON documents (thread_id, id);
"""

FILENAME = "workspace.db"

_path: Path | None = None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def configure(path: Path) -> None:
    """Point the index at a file and make sure the tables exist.

    Idempotent and cheap on the common path, because it is called from
    everywhere rather than once at startup. That is deliberate: the runs folder
    is a setting, tests point it at a temporary directory, and an index that
    remembered the first folder it ever saw would write one workspace's history
    into another's file.
    """
    global _path
    if _path == path:
        return
    _path = path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _open() as conn:
            conn.executescript(SCHEMA)
    except sqlite3.Error:
        logger.warning("could not open the workspace database at %s", path, exc_info=True)


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL so a page rendering the index cannot be blocked by a run writing to
    # it, which is the only concurrency this app actually has.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _write() -> Iterator[sqlite3.Connection | None]:
    """A connection, or None if the index is unavailable. Never raises.

    The caller writes `if conn is None: return`, which reads as a decision
    rather than as an exception being swallowed somewhere out of sight.
    """
    if _path is None:
        yield None
        return
    try:
        conn = _open()
    except sqlite3.Error:
        logger.warning("could not open the workspace database", exc_info=True)
        yield None
        return
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        logger.warning("workspace database write failed", exc_info=True)
    finally:
        conn.close()


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Read rows as plain dicts. An unreadable index reads as empty."""
    if _path is None:
        return []
    try:
        conn = _open()
    except sqlite3.Error:
        return []
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        logger.warning("workspace database read failed: %s", sql, exc_info=True)
        return []
    finally:
        conn.close()


# --- runs --------------------------------------------------------------------


def upsert_run(thread_id: str, **fields: Any) -> None:
    """Create or update one run. Only the fields given are touched.

    Partial by design: `create_run` knows the quarter and the model, staging
    knows the traffic light, and neither should have to restate the other's
    facts to avoid blanking them.
    """
    if not thread_id:
        return
    fields = {k: v for k, v in fields.items() if v is not None}
    fields["updated_at"] = now()

    with _write() as conn:
        if conn is None:
            return
        conn.execute(
            "INSERT OR IGNORE INTO runs (thread_id, created_at) VALUES (?, ?)",
            (thread_id, fields.get("created_at") or now()),
        )
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE runs SET {assignments} WHERE thread_id = ?",
            (*fields.values(), thread_id),
        )


def run(thread_id: str) -> dict | None:
    rows = _query("SELECT * FROM runs WHERE thread_id = ?", (thread_id,))
    return rows[0] if rows else None


def runs() -> list[dict]:
    return _query("SELECT * FROM runs ORDER BY COALESCE(updated_at, created_at) DESC")


def forget_run(thread_id: str) -> None:
    with _write() as conn:
        if conn is None:
            return
        for table in ("audit", "drafts", "documents"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM runs WHERE thread_id = ?", (thread_id,))


def forget_all() -> None:
    with _write() as conn:
        if conn is None:
            return
        for table in ("audit", "drafts", "documents", "runs"):
            conn.execute(f"DELETE FROM {table}")


# --- the audit index ---------------------------------------------------------


def record_audit(thread_id: str, record: dict) -> None:
    """Mirror one trail row into the index.

    The whole record is kept as JSON alongside the few columns worth querying,
    so a new field in the trail needs no migration here to be readable.
    """
    if not thread_id or not record:
        return
    with _write() as conn:
        if conn is None:
            return
        conn.execute(
            "INSERT INTO audit (thread_id, at, kind, stage, field, label, detail, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                record.get("at") or now(),
                record.get("kind", ""),
                record.get("stage"),
                record.get("field"),
                record.get("label"),
                record.get("detail"),
                json.dumps(record, default=str),
            ),
        )


def has_audit(thread_id: str) -> bool:
    rows = _query("SELECT 1 FROM audit WHERE thread_id = ? LIMIT 1", (thread_id,))
    return bool(rows)


def import_audit(thread_id: str, rows: list[dict]) -> None:
    """Load a whole trail at once, for a run that predates the index.

    Only ever called for a thread the index has nothing for, so there is no
    de-duplication here — the caller's emptiness check is the guard, and doing
    it per row would mean a query per line of every trail on disk.
    """
    if not thread_id or not rows:
        return
    with _write() as conn:
        if conn is None:
            return
        conn.executemany(
            "INSERT INTO audit (thread_id, at, kind, stage, field, label, detail, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    thread_id,
                    row.get("at") or now(),
                    row.get("kind", ""),
                    row.get("stage"),
                    row.get("field"),
                    row.get("label"),
                    row.get("detail"),
                    json.dumps(row, default=str),
                )
                for row in rows
            ],
        )


def audit_rows(
    thread_id: str | None = None, kind: str | None = None, limit: int = 500
) -> list[dict]:
    """Trail rows, newest first, with the JSON payload merged back in."""
    where, params = [], []
    if thread_id:
        where.append("thread_id = ?")
        params.append(thread_id)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = _query(
        f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ?", (*params, limit)
    )
    merged = []
    for row in rows:
        try:
            payload = json.loads(row.pop("payload") or "{}")
        except json.JSONDecodeError:
            payload = {}
        merged.append({**payload, **{k: v for k, v in row.items() if v is not None}})
    return merged


# --- draft snapshots ---------------------------------------------------------


def save_draft(thread_id: str, row: dict, reason: str = "") -> None:
    """One snapshot of the draft row, as it stood at this moment."""
    if not thread_id or not row:
        return
    with _write() as conn:
        if conn is None:
            return
        conn.execute(
            "INSERT INTO drafts (thread_id, at, reason, quarter, objective_id,"
            " traffic_light, progress_percent, row) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                now(),
                reason,
                row.get("Quarter"),
                row.get("Objective_ID"),
                row.get("Traffic_Light"),
                row.get("Progress_Percent"),
                json.dumps(row, default=str),
            ),
        )


def drafts(thread_id: str | None = None, limit: int = 200) -> list[dict]:
    """Snapshots, newest first, with the stored row parsed back out."""
    clause = "WHERE thread_id = ?" if thread_id else ""
    params: tuple = (thread_id, limit) if thread_id else (limit,)
    rows = _query(f"SELECT * FROM drafts {clause} ORDER BY id DESC LIMIT ?", params)
    for row in rows:
        try:
            row["row"] = json.loads(row.get("row") or "{}")
        except json.JSONDecodeError:
            row["row"] = {}
    return rows


def latest_draft(thread_id: str) -> dict | None:
    found = drafts(thread_id, limit=1)
    return found[0] if found else None


# --- documents ---------------------------------------------------------------


def save_documents(thread_id: str, docs: list[dict]) -> None:
    """Replace this run's document list. Runs are immutable; reruns are not."""
    if not thread_id:
        return
    with _write() as conn:
        if conn is None:
            return
        conn.execute("DELETE FROM documents WHERE thread_id = ?", (thread_id,))
        conn.executemany(
            "INSERT INTO documents (thread_id, doc_id, filename, title, doc_date, blocks)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    thread_id,
                    doc.get("doc_id"),
                    doc.get("filename"),
                    doc.get("title"),
                    doc.get("doc_date"),
                    doc.get("blocks"),
                )
                for doc in docs
            ],
        )


def documents(thread_id: str) -> list[dict]:
    return _query("SELECT * FROM documents WHERE thread_id = ? ORDER BY id", (thread_id,))


# --- headline numbers --------------------------------------------------------


def totals() -> dict:
    """What the whole workspace adds up to, for the audit overview."""
    rows = _query(
        "SELECT kind, COUNT(*) AS n FROM audit GROUP BY kind"
    )
    by_kind = {row["kind"]: row["n"] for row in rows}
    tokens = _query(
        "SELECT payload FROM audit WHERE kind = 'llm_call'"
    )
    input_tokens = output_tokens = 0
    for row in tokens:
        try:
            call = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        input_tokens += call.get("input_tokens") or 0
        output_tokens += call.get("output_tokens") or 0

    return {
        "runs": (_query("SELECT COUNT(*) AS n FROM runs") or [{"n": 0}])[0]["n"],
        "drafts": (_query("SELECT COUNT(*) AS n FROM drafts") or [{"n": 0}])[0]["n"],
        "events": by_kind.get("event", 0),
        "llm_calls": by_kind.get("llm_call", 0),
        "edits": by_kind.get("edit", 0),
        "acks": by_kind.get("ack", 0),
        "llm_input_tokens": input_tokens,
        "llm_output_tokens": output_tokens,
    }
