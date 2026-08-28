"""Showing an evidence document the way it was written.

The evidence viewer had one view: the file's bytes, numbered. That is exactly
right for checking a citation — the line numbers a citation carries only mean
anything against the original text — and exactly wrong for reading. A meeting
note arrives as `**Chair:**` and `| Milestone | Status |`, and a director asked
to judge whether a claim is fair should not have to read a table as pipes.

So the page shows both, and this module produces the readable one: markdown
rendered to HTML, split into blocks that still carry the line span they came
from, so the cited lines stay markable in the rendered view too.

Two things are deliberate here:

**Blocks, not one document.** Rendering the whole file in one pass would give
prettier HTML and no way to say which paragraph a citation landed on. Splitting
on blank lines first — the same grouping the block splitter in evidence.py uses
— keeps every rendered element addressable by line.

**The output is sanitised.** Evidence arrives by upload, and markdown passes raw
HTML straight through. Nothing in the demo pack contains a tag, but the input
surface is a file picker, and "our own files are fine" is not a property of the
input surface — it is a hope about it.
"""

from __future__ import annotations

from html.parser import HTMLParser

import markdown

# What a document is allowed to render as. Markdown's own output plus the
# inline marks its extensions produce — no forms, no scripts, no styles, no
# iframes, and nothing that loads a remote resource.
ALLOWED_TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "del", "ins", "sub", "sup", "mark",
    "blockquote", "code", "pre", "kbd", "samp", "var",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "a", "abbr", "span", "div",
}

# Attributes kept per tag. `href` is filtered again below: a `javascript:` URL
# in a markdown link is the one hole an allowlist of tags does not close.
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "th": {"align", "colspan", "rowspan"},
    "td": {"align", "colspan", "rowspan"},
    "ol": {"start"},
}

VOID_TAGS = {"br", "hr"}

SAFE_SCHEMES = ("http://", "https://", "mailto:", "#", "/")


class _Sanitiser(HTMLParser):
    """Keep the tags on the list, escape everything else into visible text.

    Dropping an unknown tag silently would quietly change what a document says.
    Escaping it means an evidence file containing `<script>` renders as the
    characters someone typed, which is both safe and honest.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.parts.append(_escape(self.get_starttag_text() or ""))
            return
        kept = []
        for name, value in attrs:
            if name not in ALLOWED_ATTRS.get(tag, set()):
                continue
            if name == "href" and not _safe_url(value or ""):
                continue
            kept.append(f' {name}="{_escape(value or "")}"')
        closing = " /" if tag in VOID_TAGS else ""
        self.parts.append(f"<{tag}{''.join(kept)}{closing}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.parts.append(_escape(data))

    def handle_comment(self, data: str) -> None:
        # A markdown comment is not content. Dropping it is not a silent edit:
        # it was never going to be visible.
        return

    def result(self) -> str:
        self.close()
        return "".join(self.parts)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_url(url: str) -> bool:
    stripped = url.strip().lower()
    if not stripped:
        return False
    if ":" not in stripped.split("/")[0]:
        return True  # relative — no scheme to be wrong about
    return stripped.startswith(SAFE_SCHEMES)


def sanitise(html: str) -> str:
    parser = _Sanitiser()
    parser.feed(html)
    return parser.result()


def to_html(text: str) -> str:
    """One markdown fragment, rendered and sanitised."""
    if not text.strip():
        return ""
    rendered = markdown.markdown(
        text,
        extensions=["tables", "sane_lists", "nl2br"],
        output_format="html",
    )
    return sanitise(rendered)


def blocks(text: str, cited_lines: set[int] | None = None) -> list[dict]:
    """The document as readable blocks, each carrying the lines it came from.

    Grouping is by blank line, which is markdown's own paragraph rule, so a
    table, a list and a blockquote each stay whole. A heading immediately
    followed by its paragraph is one group and renders as both, which is the
    right answer: the heading belongs to the text under it.
    """
    cited = cited_lines or set()
    rendered: list[dict] = []

    for lines, start in _groups(text.splitlines()):
        end = start + len(lines) - 1
        html = to_html("\n".join(lines))
        if not html:
            continue
        rendered.append(
            {
                "html": html,
                "line_start": start,
                "line_end": end,
                "cited": any(n in cited for n in range(start, end + 1)),
            }
        )
    return rendered


def _groups(lines: list[str]) -> list[tuple[list[str], int]]:
    """Blank-line-separated groups, with 1-based line numbers kept."""
    groups: list[tuple[list[str], int]] = []
    current: list[str] = []
    start = 1
    for number, line in enumerate(lines, start=1):
        if line.strip():
            if not current:
                start = number
            current.append(line)
        elif current:
            groups.append((current, start))
            current = []
    if current:
        groups.append((current, start))
    return groups
