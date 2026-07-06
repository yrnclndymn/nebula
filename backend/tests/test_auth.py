"""Auth helpers + toggle (no Firebase / no network)."""

import asyncio

from starlette.requests import Request

from app import auth
from app.config import settings


def _request(headers: dict) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def test_allowlist():
    settings.allowed_emails = "a@x.com, B@Y.com"
    assert auth._allowed("a@x.com")
    assert auth._allowed("b@y.com")  # case-insensitive
    assert not auth._allowed("c@z.com")
    assert not auth._allowed(None)


def test_bearer_extraction():
    assert auth._bearer(_request({"Authorization": "Bearer abc.def"})) == "abc.def"
    assert auth._bearer(_request({})) is None


def test_verify_user_noop_when_auth_off():
    settings.require_auth = False
    assert asyncio.run(auth.verify_user(_request({}))) is None
    assert asyncio.run(auth.verify_task(_request({}))) is None
