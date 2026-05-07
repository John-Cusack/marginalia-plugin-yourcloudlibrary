"""XHTML-text extraction and base64 chapter decoding."""

from __future__ import annotations

import base64

from ycl.api.text import chapter_to_text, decode_chapter_body, xhtml_to_text


def test_decode_chapter_body_round_trip():
    raw_xhtml = "<?xml version=\"1.0\"?><html><body><p>hello</p></body></html>"
    encoded = base64.b64encode(raw_xhtml.encode("utf-8")).decode("ascii")
    assert decode_chapter_body(encoded) == raw_xhtml


def test_decode_chapter_body_tolerates_padding_and_whitespace():
    raw = "<p>x</p>"
    no_pad = base64.b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")
    body_with_noise = f"  {no_pad}\n"
    assert decode_chapter_body(body_with_noise) == raw


def test_xhtml_to_text_extracts_paragraph_breaks():
    xhtml = """<html><body>
        <p>Hello there.</p>
        <p>Second paragraph.</p>
        <p>Third.</p>
    </body></html>"""
    text = xhtml_to_text(xhtml)
    # Three lines, separated by blank lines.
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    assert paragraphs == ["Hello there.", "Second paragraph.", "Third."]


def test_xhtml_to_text_strips_script_and_style():
    xhtml = """<html><head>
        <style>body{color:red}</style>
        <script>alert(1)</script>
    </head><body><p>visible only</p></body></html>"""
    text = xhtml_to_text(xhtml)
    assert "alert" not in text
    assert "color:red" not in text
    assert "visible only" in text


def test_xhtml_to_text_preserves_inline_formatting_text():
    xhtml = "<p>Hello <em>world</em>, this is <b>bold</b>.</p>"
    text = xhtml_to_text(xhtml)
    assert "Hello world, this is bold." in text


def test_xhtml_to_text_collapses_whitespace_runs():
    xhtml = "<p>foo\t \tbar    baz</p>"
    text = xhtml_to_text(xhtml)
    assert text == "foo bar baz"


def test_xhtml_to_text_decodes_html_entities():
    xhtml = "<p>Church&#x2019;s Mission &amp; Other Things</p>"
    text = xhtml_to_text(xhtml)
    assert text == "Church’s Mission & Other Things"


def test_xhtml_to_text_handles_headings():
    xhtml = "<h1>Chapter One</h1><p>Body text follows.</p>"
    text = xhtml_to_text(xhtml)
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    assert paragraphs == ["Chapter One", "Body text follows."]


def test_chapter_to_text_full_pipeline():
    xhtml = "<html><body><h1>Title</h1><p>Para one.</p><p>Para two.</p></body></html>"
    encoded = base64.b64encode(xhtml.encode("utf-8")).decode("ascii")
    text = chapter_to_text(encoded)
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    assert paragraphs == ["Title", "Para one.", "Para two."]
