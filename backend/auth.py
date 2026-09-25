"""Google Sign-In: the browser gets a Google ID token, we verify it directly with Google."""
import asyncio
import secrets
import uuid
from datetime import datetime, timedelta, timezone
import os

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from db import db

SESSION_COOKIE = "session_token"
SESSION_DAYS = 7

auth_router = APIRouter(prefix="/api/auth")

EXEMPT_PREFIXES = ("/api/auth", "/api/oauth/callback", "/api/media-health")


def _email_set(var):
    return {e.strip().lower() for e in os.environ.get(var, "").split(",") if e.strip()}


def admin_emails():
    """Only these Google accounts are admins (comma-separated ADMIN_EMAILS)."""
    return _email_set("ADMIN_EMAILS")


def is_admin_email(email):
    return (email or "").strip().lower() in admin_emails()


def auth_required():
    """AUTH_REQUIRED=true makes every /api route need a signed-in Google session."""
    return os.environ.get("AUTH_REQUIRED", "").strip().lower() in ("1", "true", "yes")


def email_allowed(email):
    """ALLOWED_EMAILS empty = any verified Google account may sign in. Admins are always allowed."""
    email = (email or "").strip().lower()
    allowed = _email_set("ALLOWED_EMAILS")
    return not allowed or email in allowed or email in admin_emails()


def google_client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def _cookie_flags():
    """The app is same-origin (Caddy serves the UI and /api), so Lax is enough.
    Set COOKIE_SECURE=true when the site is served over https."""
    secure = os.environ.get("COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
    return {"secure": secure, "samesite": "lax"}


def _utc(v):
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    if isinstance(v, datetime) and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v


def _token_from(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return token


async def verify_auth(request: Request):
    if not auth_required():
        return
    path = request.url.path
    if path in ("/api", "/api/") or any(path.startswith(p) for p in EXEMPT_PREFIXES):
        return
    token = _token_from(request)
    if not token:
        raise HTTPException(401, "login required")
    doc = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not doc or _utc(doc.get("expires_at")) < datetime.now(timezone.utc):
        raise HTTPException(401, "session expired — sign in again")
    if not email_allowed(doc.get("email")):
        raise HTTPException(403, "this account is not allowed")
    request.state.user_id = doc.get("user_id")


class SessionBody(BaseModel):
    credential: str  # Google ID token (JWT) from Google Identity Services


@auth_router.get("/config")
async def auth_config():
    return {"google_client_id": google_client_id(), "auth_required": auth_required()}


@auth_router.post("/session")
async def create_session(body: SessionBody, response: Response):
    client_id = google_client_id()
    if not client_id:
        raise HTTPException(503, "GOOGLE_CLIENT_ID is not set in backend/.env")
    try:
        data = await asyncio.to_thread(
            google_id_token.verify_oauth2_token, body.credential, google_requests.Request(), client_id,
            clock_skew_in_seconds=int(os.environ.get("GOOGLE_CLOCK_SKEW_SECONDS", "10")))
    except ValueError as e:
        print(f"[auth] google token rejected: {e}", flush=True)
        raise HTTPException(401, "Google sign-in could not be verified")
    email = (data.get("email") or "").lower()
    if not email or not data.get("email_verified"):
        raise HTTPException(401, "Google account email is not verified")
    if not email_allowed(email):
        raise HTTPException(403, "This Google account is not allowed to use StoryForge")
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user:
        user = {"user_id": f"user_{uuid.uuid4().hex[:12]}", "email": email,
                "name": data.get("name", ""), "picture": data.get("picture", ""),
                "role": "admin" if is_admin_email(email) else "user",
                "created_at": datetime.now(timezone.utc)}
        await db.users.insert_one(dict(user))
    else:
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"name": data.get("name", user.get("name", "")),
                      "picture": data.get("picture", user.get("picture", ""))}})
    session_token = secrets.token_urlsafe(32)
    await db.user_sessions.insert_one({
        "user_id": user["user_id"], "email": email, "session_token": session_token,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
        "created_at": datetime.now(timezone.utc)})
    response.set_cookie(SESSION_COOKIE, session_token, max_age=SESSION_DAYS * 86400,
                        path="/", httponly=True, **_cookie_flags())
    return {"user": {k: user[k] for k in ("user_id", "email", "name", "picture")}}


async def optional_user_id(request: "Request") -> str:
    """Best-effort user attribution when a session cookie/bearer is present; never raises."""
    try:
        token = _token_from(request)
        if not token:
            return ""
        sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
        return (sess or {}).get("user_id", "")
    except Exception:
        return ""


@auth_router.get("/me")
async def me(request: Request):
    token = _token_from(request)
    if not token:
        if auth_required():
            raise HTTPException(401, "login required")
        return Response(status_code=204)
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess or _utc(sess.get("expires_at")) < datetime.now(timezone.utc):
        raise HTTPException(401, "session expired — sign in again")
    user = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(401, "user not found")
    out = {k: user.get(k) for k in ("user_id", "email", "name", "picture")}
    out["is_admin"] = is_admin_email(user.get("email"))
    return out


@auth_router.post("/logout")
async def logout(request: Request, response: Response):
    token = _token_from(request)
    if token:
        await db.user_sessions.delete_one({"session_token": token})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
