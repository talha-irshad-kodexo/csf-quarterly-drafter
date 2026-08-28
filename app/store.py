"""Writing to the data folder.

The evidence folder is the input surface, and until now the only way to change
it was a text editor. That matters more than it sounds: the whole point of this
workflow is that someone can put new material in front of it and rerun, and if
that requires a terminal then the tool only works for the person who built it.

Everything here writes plain markdown to `DATA_DIR`. No database, no upload
store — the files stay exactly as readable and as diffable as they were when
someone dropped them in by hand.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from pathlib import Path

MARKDOWN_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_UPLOAD_BYTES = 1_000_000


class StoreError(ValueError):
    """Something the person can fix, phrased so they can fix it."""


def slugify(text: str) -> str:
    normalised = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalised).strip("-").lower()
    return slug[:60] or "document"


def safe_target(evidence_dir: Path, filename: str) -> Path:
    """Resolve a filename inside the evidence folder, or refuse.

    Anything with a path separator, a parent reference, or a resolved location
    outside the folder is rejected rather than sanitised — quietly rewriting a
    suspicious path is how a traversal bug survives review.
    """
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise StoreError(f"{filename!r} is not a valid document name")

    target = (evidence_dir / filename).resolve()
    if target.parent != evidence_dir.resolve():
        raise StoreError(f"{filename!r} is outside the evidence folder")
    if target.suffix.lower() not in MARKDOWN_SUFFIXES:
        raise StoreError(f"{target.suffix or 'that'} is not a markdown file")
    return target


def unique_path(evidence_dir: Path, slug: str) -> Path:
    candidate = evidence_dir / f"{slug}.md"
    counter = 2
    while candidate.exists():
        candidate = evidence_dir / f"{slug}-{counter}.md"
        counter += 1
    return candidate


def compose_markdown(title: str, body: str, date: str = "") -> str:
    """Build a document the loader can read back.

    The loader takes the date from the heading, falling back to an italic line
    just beneath it. Writing the date on its own line means a title does not
    have to be phrased in any particular way to be dated correctly.
    """
    title = title.strip()
    body = body.strip()
    lines = [f"# {title}", ""]
    if date.strip():
        lines += [f"*{date.strip()}*", ""]
    lines += [body, ""]
    return "\n".join(lines)


def add_pasted(evidence_dir: Path, title: str, body: str, date: str = "") -> Path:
    if not title.strip():
        raise StoreError("Give the document a title — it is what the reader sees first.")
    if not body.strip():
        raise StoreError("Paste the document text.")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(evidence_dir, slugify(title))
    path.write_text(compose_markdown(title, body, date), encoding="utf-8")
    return path


def add_uploaded(evidence_dir: Path, filename: str, content: bytes) -> Path:
    if not content:
        raise StoreError(f"{filename} is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise StoreError(f"{filename} is larger than 1 MB.")

    suffix = Path(filename).suffix.lower()
    if suffix not in MARKDOWN_SUFFIXES:
        raise StoreError(f"{filename} is not markdown or plain text.")

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StoreError(f"{filename} is not valid UTF-8 text.") from error

    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(evidence_dir, slugify(Path(filename).stem))

    # Give it a heading if it has none, so the loader has a title to show.
    if not text.lstrip().startswith("#"):
        text = f"# {Path(filename).stem}\n\n{text}"
    path.write_text(text, encoding="utf-8")
    return path


def remove(evidence_dir: Path, filename: str) -> None:
    target = safe_target(evidence_dir, filename)
    if not target.exists():
        raise StoreError(f"{filename} is not there.")
    target.unlink()


def read_document(evidence_dir: Path, filename: str) -> str:
    return safe_target(evidence_dir, filename).read_text(encoding="utf-8")


def write_document(evidence_dir: Path, filename: str, text: str) -> Path:
    if not text.strip():
        raise StoreError("A document cannot be empty. Delete it instead.")
    target = safe_target(evidence_dir, filename)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    return target


def resolve_workspace_document(settings, filename: str) -> tuple[Path, str]:
    """Find a named file wherever the workspace keeps it.

    The objective record and the prior row live next to the evidence folder,
    not inside it. The edit link is the same for every file on the setup
    screen, so this has to look in all three places rather than 404 on the
    two the pipeline actually needs.
    """
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise StoreError(f"{filename!r} is not a valid document name")

    named = {
        settings.objective_file.name: (settings.objective_file, "objective"),
        settings.prior_update_file.name: (settings.prior_update_file, "prior"),
    }
    special = named.get(filename)
    if special is not None and special[0].exists():
        return special

    target = safe_target(settings.evidence_dir, filename)
    if not target.exists():
        raise StoreError(f"{filename} is not there.")
    return target, "evidence"


def write_workspace_document(path: Path, text: str) -> Path:
    if not text.strip():
        raise StoreError("A document cannot be empty. Delete it instead.")
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


# --- roles -------------------------------------------------------------------


def set_role(settings, filename: str, role: str) -> str:
    """Move a document between evidence, the objective record and the prior row.

    A file's role in this build is its location, so the role dropdown moves it.
    Whatever it displaces is moved into the evidence folder rather than deleted:
    a mis-click on a dropdown must not destroy the objective record, and a file
    sitting in evidence is visible and recoverable.

    Returns a sentence describing what moved, for the toast.
    """
    evidence_dir = settings.evidence_dir
    destinations = {
        "objective": settings.objective_file,
        "prior": settings.prior_update_file,
    }

    source = None
    for candidate in (settings.objective_file, settings.prior_update_file):
        if candidate.name == filename and candidate.exists():
            source = candidate
    if source is None:
        source = safe_target(evidence_dir, filename)
    if not source.exists():
        raise StoreError(f"{filename} is not there.")

    target = destinations.get(role)

    if target is None:
        # Back to evidence.
        if source.parent == evidence_dir:
            return f"{filename} is already evidence."
        moved = unique_path(evidence_dir, slugify(source.stem))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        moved.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        source.unlink()
        return f"{filename} is now evidence, as {moved.name}."

    if source == target:
        return f"{filename} is already the {role} record."

    displaced = ""
    if target.exists():
        evidence_dir.mkdir(parents=True, exist_ok=True)
        kept = unique_path(evidence_dir, slugify(target.stem))
        kept.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        displaced = f" The previous one was kept as {kept.name}."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    if source.parent == evidence_dir:
        source.unlink()

    label = "objective record" if role == "objective" else "previous quarter row"
    return f"{filename} is now the {label}.{displaced}"


# --- the objective record ----------------------------------------------------


def write_field_table(path: Path, title: str, fields: dict[str, str]) -> None:
    """Write a `| Field | Value |` table, the shape the loader reads."""
    rows = "\n".join(f"| `{key}` | {value or '—'} |" for key, value in fields.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n\n| Field | Value |\n|---|---|\n{rows}\n", encoding="utf-8"
    )


def update_objective(path: Path, existing: dict[str, str], changes: dict[str, str]) -> None:
    """Apply changes, keeping every field the form did not carry.

    A form that shows four fields must not silently drop the ten it does not.
    """
    merged = dict(existing)
    merged.update({k: v for k, v in changes.items() if v is not None})
    merged["Last_Modified"] = dt.date.today().isoformat()
    write_field_table(path, f"Level2_Objectives — {merged.get('Objective_ID', 'record')}", merged)
