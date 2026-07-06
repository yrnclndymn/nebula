"""Logo image MIME filtering — Gemini rejects non-raster types (SVG etc.)."""

from app.tools.web import _gemini_image_mime


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
