"""Logo image MIME filtering — Gemini rejects non-raster types (SVG etc.),
plus UTF-8-safe page decoding (#89)."""

import requests

from app.tools.web import _fetch_page_live, _gemini_image_mime


def test_accepts_supported_raster_types():
    assert _gemini_image_mime("image/png", b"\x89PNG\r\n") == "image/png"
    assert _gemini_image_mime("image/jpeg; charset=binary", b"\xff\xd8\xff") == "image/jpeg"
    assert _gemini_image_mime("IMAGE/WEBP", b"RIFF....WEBP") == "image/webp"


def test_skips_svg_and_other_unsupported_types():
    assert _gemini_image_mime("image/svg+xml", b"<svg></svg>") is None
    assert _gemini_image_mime("image/gif", b"GIF89a") is None
    assert _gemini_image_mime("image/x-icon", b"\x00\x00\x01\x00") is None
    assert _gemini_image_mime("", b"\x89PNG") is None


def test_skips_svg_mislabeled_as_raster():
    # A server that claims image/png but actually returns SVG/XML text.
    assert _gemini_image_mime("image/png", b"<?xml version='1.0'?><svg/>") is None


# --- #89: UTF-8 page text survives fetch when the server omits a charset --------


def _html_resp(body: bytes, content_type: str) -> requests.Response:
    resp = requests.Response()
    resp._content = body
    resp.status_code = 200
    resp.headers["Content-Type"] = content_type
    resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
    return resp


def test_fetch_page_preserves_utf8_without_charset_header(monkeypatch):
    # A charset-less UTF-8 page with curly quotes / accents in body text and an
    # external LinkedIn link — both the readable text and the social scrape must
    # come through un-mangled. Fictional (Acme).
    html = (
        "<html><body><p>Acme’s café — “launch” of €5m round…</p>"
        '<a href="https://www.linkedin.com/company/acme">LinkedIn</a>'
        "</body></html>"
    ).encode("utf-8")
    monkeypatch.setattr("app.tools.web.requests.get", lambda *a, **k: _html_resp(html, "text/html"))
    page = _fetch_page_live("https://acme.example/")
    assert "Acme’s café" in page["text"]
    assert "“launch”" in page["text"] and "€5m" in page["text"]
    assert page["social"].get("linkedin") == "https://www.linkedin.com/company/acme"
