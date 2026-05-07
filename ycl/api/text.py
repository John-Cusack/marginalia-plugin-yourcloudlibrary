"""XHTML → plain text extraction for YCL chapter content.

The chapter responses are base64-encoded XHTML (lightweight obfuscation,
not real DRM). Once decoded, they're standard EPUB-style XHTML with
``<p>``, ``<h1>``, etc.

We do not rely on a full HTML library here — these documents are
well-formed XHTML produced by Innodata and similar EPUB toolchains, and
``html.parser`` from the stdlib handles them fine. Keeping the dependency
surface minimal also matches the plugin's "deps that come pre-installed
with the harness" preference.
"""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser

# Tags whose content is irrelevant chrome and should be stripped entirely.
_DROP_TAGS = {"script", "style", "head", "noscript"}

# Tags that introduce a paragraph break in the rendered text.
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "br",
    "hr",
    "tr",
    "table",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._suppress_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._suppress_depth += 1
        if tag in _BLOCK_TAGS:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._suppress_depth > 0:
            self._suppress_depth -= 1
        if tag in _BLOCK_TAGS:
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        self._buf.append(data)

    def text(self) -> str:
        return "".join(self._buf)


def decode_chapter_body(body_text: str) -> str:
    """Decode a base64-wrapped chapter response into raw XHTML.

    The wire format is ``base64(utf-8(xhtml))`` with no padding stripped.
    Whitespace (the YCL CDN sometimes injects a stray newline at the end)
    is removed before decoding.
    """
    cleaned = body_text.strip()
    padded = cleaned + "=" * (-len(cleaned) % 4)
    return base64.b64decode(padded).decode("utf-8", errors="replace")


def xhtml_to_text(xhtml: str) -> str:
    """Extract plain text from XHTML, preserving paragraph breaks.

    Returns text with consecutive blank lines collapsed to a single newline
    pair. Lines are trimmed of trailing whitespace; leading whitespace
    inside a paragraph is preserved.
    """
    extractor = _TextExtractor()
    extractor.feed(xhtml)
    extractor.close()
    raw = extractor.text()
    # Collapse runs of whitespace within each line, preserve paragraph breaks.
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in raw.splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(line)
            blank = False
    return "\n".join(out).strip()


def chapter_to_text(body_text: str) -> str:
    """Convenience: base64-wrapped chapter response → plain text."""
    return xhtml_to_text(decode_chapter_body(body_text))
