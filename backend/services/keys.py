"""Per-account API-key isolation.

TRUE MULTI-USER: each account has its own key vault, isolated from all others.
When a user context is active, PROVIDER_KEYS are read ONLY from that account's
vault — never from os.environ and never from another account.

When no user context is active (system background jobs with no owner), the
process environment is the fallback so a fresh install works without requiring
the admin to re-enter keys before the first job runs.

Active account is bound to a ContextVar:
  * request handlers  -> ``caller_keys`` dependency in server.py
  * background workers -> set per job from the job's story/book owner (job_queue.py)
"""
import os
from contextvars import ContextVar

# Keys that belong to a single account.  NEVER leak between accounts.
PROVIDER_KEYS: frozenset = frozenset({
    "OPENAI_API_KEY", "GEMINI_API_KEY", "FAL_KEY", "HF_TOKEN",
    "REPLICATE_API_TOKEN", "PEXELS_API_KEY", "STABILITY_API_KEY", "STUDIO_API_TOKEN",
})

# Sentinel to distinguish "no context set" from "context set to empty dict"
_UNSET = object()
_active: ContextVar = ContextVar("active_keys", default=_UNSET)


def set_active(values: dict):
    """Bind an account's key dict to the current async context. Returns a reset token."""
    clean = {k: str(v).strip() for k, v in (values or {}).items() if str(v).strip()}
    return _active.set(clean)


def reset(token) -> None:
    try:
        _active.reset(token)
    except Exception:
        pass


def active():
    """Return the active key dict, or None if no context has been set."""
    v = _active.get(_UNSET)
    return None if v is _UNSET else v


def get(name: str) -> str:
    """Resolve a credential for the active account.

    TRUE ISOLATION RULES
    --------------------
    * A user context IS active  → provider keys come ONLY from that account's
      vault.  os.environ is NEVER consulted for PROVIDER_KEYS, so one user
      cannot see or spend another user's credits.
      Non-provider keys (OLLAMA_BASE_URL, STUDIO_*, social OAuth) fall back to
      the process environment because they are operator-level infrastructure.
    * No user context active    → env fallback for everything so a fresh install
      works before the admin has entered keys via the UI.
    """
    ctx = active()

    if ctx is not None:
        # ── User context IS set ───────────────────────────────────────────────
        val = (ctx.get(name) or "").strip()
        if name in PROVIDER_KEYS:
            return val          # strict: vault only, never env
        return val or (os.environ.get(name) or "").strip()
    else:
        # ── No user context (system jobs / unauthenticated) ───────────────────
        return (os.environ.get(name) or "").strip()


# ── Async helpers used by server.py / job_queue.py ────────────────────────────

async def load_for_owner(owner_id: str) -> dict:
    """Return the provider keys stored in a single account's vault."""
    from db import db
    if not owner_id:
        return {}
    doc = await db.api_keys.find_one({"_id": owner_id}) or {}
    return {k: str(v).strip() for k, v in (doc.get("values") or {}).items() if str(v).strip()}


async def system_owner_id() -> str:
    """Account whose vault powers ownerless system jobs (news / engagement)."""
    from auth import admin_emails
    from db import db
    admin = await db.users.find_one({"role": "admin"})
    if not admin:
        emails = list(admin_emails())
        if emails:
            admin = await db.users.find_one({"email": {"$in": emails}})
    return (admin or {}).get("user_id", "")


async def save_for_owner(owner_id: str, values: dict) -> list:
    """Merge new key values into a user's vault. Returns saved key names."""
    from db import db
    from models import utcnow
    clean = {
        k: str(v).strip() for k, v in (values or {}).items()
        if k in PROVIDER_KEYS and str(v).strip()
    }
    if not clean or not owner_id:
        return []
    doc = await db.api_keys.find_one({"_id": owner_id}) or {"_id": owner_id, "values": {}}
    vals = doc.get("values", {})
    vals.update(clean)
    await db.api_keys.update_one(
        {"_id": owner_id},
        {"$set": {"values": vals, "updated_at": utcnow()}},
        upsert=True,
    )
    return list(clean.keys())


async def clear_for_owner(owner_id: str, name: str) -> None:
    """Wipe one key from a user's vault."""
    from db import db
    from models import utcnow
    if not owner_id:
        return
    await db.api_keys.update_one(
        {"_id": owner_id},
        {"$set": {f"values.{name}": "", "updated_at": utcnow()}},
        upsert=True,
    )


async def configured_for_owner(owner_id: str) -> dict:
    """Return {key_name: bool_set} for every PROVIDER_KEY in a user's vault."""
    vals = await load_for_owner(owner_id)
    return {k: bool(vals.get(k, "").strip()) for k in PROVIDER_KEYS}
