"""Admin console: monitor all accounts, channels, video performance, revenue estimate, queue health."""
import os
from asyncio import to_thread as _to_thread
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from auth import _token_from, _utc, is_admin_email
from db import db

admin_router = APIRouter(prefix="/api/admin")


def _fix(doc):
    if doc.get("_id") is not None and not isinstance(doc["_id"], str):
        doc["_id"] = str(doc["_id"])
    doc["id"] = doc.pop("_id")
    return doc


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


async def verify_admin(request: Request):
    token = _token_from(request)
    if not token:
        raise HTTPException(401, "login required")
    sess = await db.user_sessions.find_one({"session_token": token}, {"_id": 0})
    if not sess or _utc(sess.get("expires_at")) < datetime.now(timezone.utc):
        raise HTTPException(401, "session expired")
    user = await db.users.find_one({"user_id": sess["user_id"]}, {"_id": 0})
    if not user:
        raise HTTPException(401, "user not found")
    if not is_admin_email(user.get("email")):
        raise HTTPException(403, "admin access required")
    request.state.user_id = user["user_id"]
    return user


async def _yt_stats(video_ids):
    from services import social
    try:
        return await _to_thread(social.yt_video_stats, video_ids)
    except Exception as e:
        print(f"[admin] yt stats failed: {str(e)[:120]}", flush=True)
        return {}


async def _yt_channel():
    from services import social
    try:
        return await _to_thread(social.yt_channel_info)
    except Exception as e:
        print(f"[admin] yt channel info failed: {str(e)[:120]}", flush=True)
        return {}


async def _gather_accounts():
    accounts = []
    async for u in db.users.find({}, {"_id": 0}).sort("created_at", 1):
        d = {"user_id": u.get("user_id"), "email": u.get("email"), "name": u.get("name"),
             "role": u.get("role", "user"), "created_at": _iso(u.get("created_at"))}
        d["stories"] = await db.stories.count_documents({"owner_id": u["user_id"]})
        accounts.append(d)
    return accounts


async def _gather_channels():
    channels = []
    async for c in db.channels.find().sort("created_at", 1):
        d = _fix(dict(c))
        d["created_at"] = _iso(d.get("created_at"))
        d["stories"] = await db.stories.count_documents({"channel_id": d["id"]})
        d["published"] = await db.stories.count_documents({"channel_id": d["id"], "status": "published"})
        channels.append(d)
    return channels


async def _gather_published():
    """Published stories with live platform stats. Returns (rows, views, likes, comments)."""
    published, total_views, total_likes, total_comments = [], 0, 0, 0
    async for s in db.stories.find({"status": "published"}).sort("updated_at", -1).limit(100):
        pub = s.get("publish") or {}
        yt = pub.get("youtube") or {}
        row = {"id": s["id"], "title": s.get("title_english") or s.get("title_hindi", "Untitled"),
               "channel": s.get("channel_id"), "cost": (s.get("cost") or {}).get("total", 0)}
        if yt.get("video_id"):
            row["url"] = yt.get("url", f"https://www.youtube.com/shorts/{yt['video_id']}")
            row["platform"] = "youtube"
            stats = await _yt_stats([yt["video_id"]])
            st = stats.get(yt["video_id"], {})
            row.update(views=st.get("views", 0), likes=st.get("likes", 0), comments=st.get("comments", 0))
        elif pub.get("instagram", {}).get("post_id"):
            row.update(url=pub["instagram"].get("url", ""), platform="instagram",
                       views=0, likes=0, comments=0)
        else:
            row.update(url="", platform="", views=0, likes=0, comments=0)
        total_views += row.get("views", 0) or 0
        total_likes += row.get("likes", 0) or 0
        total_comments += row.get("comments", 0) or 0
        published.append(row)
    return published, total_views, total_likes, total_comments


@admin_router.get("/overview")
async def overview(request: Request):
    await verify_admin(request)

    accounts = await _gather_accounts()
    channels = await _gather_channels()
    published, total_views, total_likes, total_comments = await _gather_published()

    cost_agg = await db.stories.aggregate(
        [{"$group": {"_id": None, "total": {"$sum": "$cost.total"}}}]).to_list(1)
    rpm = float(os.environ.get("YT_RPM_USD", "0.05"))
    ch = await _yt_channel()

    return {
        "totals": {
            "accounts": len(accounts), "channels": len(channels),
            "videos_published": len(published),
            "views": total_views, "likes": total_likes, "comments": total_comments,
            "subscribers": (ch or {}).get("subscribers", 0),
            "est_revenue": round(total_views / 1000 * rpm, 2),
            "api_cost": round((cost_agg[0]["total"] if cost_agg else 0.0), 2),
            "rpm_used": rpm,
        },
        "accounts": accounts,
        "channels": channels,
        "videos": published,
        "youtube_channel": ch or {},
        "notes": "Revenue is an ESTIMATE (views x RPM, set YT_RPM_USD in backend/.env). Exact payouts require YouTube Analytics/AdSense access. Per-video views/likes/comments come live from the YouTube Data API.",
    }


@admin_router.get("/youtube/live")
async def youtube_live(request: Request, refresh: int = 0):
    """Live YouTube channel + per-video analytics from the YouTube Data & Analytics APIs
    (10-minute server-side cache keeps the Data API quota safe)."""
    import time as _t

    from services import social

    await verify_admin(request)
    if not social.cred_status()["youtube"]:
        raise HTTPException(400, "YouTube not connected — complete the OAuth flow in Settings first")
    if not refresh:
        doc = await db.yt_cache.find_one({"key": "live"})
        if doc and doc.get("data") and _t.time() - (doc.get("ts") or 0) < 600:
            out = dict(doc["data"])
            out["cached"] = True
            out["age_sec"] = int(_t.time() - doc["ts"])
            return out

    story_by_videoid = {}
    async for s in db.stories.find({"publish.youtube.video_id": {"$exists": True, "$ne": ""}}):
        y = (s.get("publish") or {}).get("youtube") or {}
        story_by_videoid[y["video_id"]] = {
            "story_id": s["id"], "cost": (s.get("cost") or {}).get("total", 0)}

    data = {"channel": {}, "videos": [], "analytics": {"scope_ok": False}, "cached": False}
    try:
        data["channel"] = await _to_thread(social.yt_channel_info)
        vids = await _to_thread(social.yt_channel_uploads, 30)
    except Exception as e:
        raise HTTPException(502, f"YouTube API error: {str(e)[:200]}")
    for v in vids:
        v.update(story_by_videoid.get(v["video_id"], {}))
    data["videos"] = vids
    try:
        data["analytics"] = await _to_thread(social.yt_analytics_28d)
    except Exception as e:
        data["analytics"] = {"scope_ok": False, "note": str(e)[:180]}

    data["rpm_used"] = float(os.environ.get("YT_RPM_USD", "0.05"))
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    await db.yt_cache.update_one(
        {"key": "live"}, {"$set": {"key": "live", "ts": _t.time(), "data": data}}, upsert=True)
    return data
