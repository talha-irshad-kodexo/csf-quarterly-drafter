"""Every template must compile against the environment the app actually uses.

Jinja loads templates from disk lazily, so a template referencing a filter that
main.py never registered compiles fine on its own and raises only when someone
opens that page. This walks the whole directory once, against the real
environment, so the failure lands in CI instead of in a director's browser.
"""

from __future__ import annotations

from app.config import PACKAGE_DIR
from app.main import templates

TEMPLATE_DIR = PACKAGE_DIR / "templates"


def template_names() -> list[str]:
    return sorted(
        p.relative_to(TEMPLATE_DIR).as_posix() for p in TEMPLATE_DIR.rglob("*.html")
    )


def test_there_are_templates_to_check():
    assert template_names(), "the loader is pointed at the wrong directory"


def test_every_template_compiles():
    """Catches an unregistered filter, a bad macro import, a syntax slip."""
    for name in template_names():
        templates.env.get_template(name)


def test_the_filters_templates_rely_on_are_registered():
    for name in ("fmt_date", "fmt_datetime", "claim_refs", "cite_tokens"):
        assert name in templates.env.filters, name


def test_cite_tokens_turns_claim_brackets_into_marks():
    from app.main import _cite_tokens

    para = _cite_tokens("The measure receded [E1.1, E2.1].")[0]
    assert para[0] == {"kind": "text", "text": "The measure receded "}
    assert para[1] == {"kind": "refs", "refs": ["E1.1", "E2.1"]}
    assert para[2] == {"kind": "text", "text": "."}

    hedge = _cite_tokens("Down from last quarter [prior submission context].")[0]
    assert len(hedge) == 1 and hedge[0]["kind"] == "text"
