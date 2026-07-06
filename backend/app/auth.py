"""Auth dependencies.

Two guards, both no-ops when `require_auth` is off (local dev):
- `verify_user` — a Firebase ID token whose verified email is in `allowed_emails`.
  Applied to all user-facing routes.
- `verify_task` — a Google OIDC token from the Cloud Tasks service account.
  Applied to `/jobs/run` (called by Cloud Tasks, not the user).
"""

from fastapi import HTTPException, Request

from app.config import settings

_firebase_app = None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else None


def _allowed(email: str | None) -> bool:
    allow = {e.strip().lower() for e in settings.allowed_emails.split(",") if e.strip()}
    return bool(email) and email.lower() in allow


def _ensure_firebase():
    global _firebase_app
    if _firebase_app is None:
        import firebase_admin

        # Uses Application Default Credentials (the Cloud Run service account).
        _firebase_app = firebase_admin.initialize_app()
    return _firebase_app


async def verify_user(request: Request) -> str | None:
    """Require an allow-listed Firebase user. Returns the email (or None if auth off)."""
    if not settings.require_auth:
        return None
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")

    from firebase_admin import auth as fb_auth

    _ensure_firebase()
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
    email = decoded.get("email")
    if not decoded.get("email_verified") or not _allowed(email):
        raise HTTPException(status_code=403, detail="not authorized")
    return email


async def verify_task(request: Request) -> str | None:
    """Require a Google OIDC token from the Cloud Tasks service account."""
    if not settings.require_auth:
        return None
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing task token")

    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token

    try:
        claims = g_id_token.verify_oauth2_token(token, g_requests.Request())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid task token: {exc}") from exc
    if claims.get("email") != settings.tasks_service_account:
        raise HTTPException(status_code=403, detail="not the tasks service account")
    return claims.get("email")
