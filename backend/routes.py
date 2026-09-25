import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from auth import admin_emails, optional_user_id
from db import db
from job_queue import HEARTBEAT, QUEUE_PAUSED, enqueue
from models import Book, Story, Channel, utcnow
from services.ocr import MEDIA_ROOT

router = APIRouter(prefix="/api")


def fix(doc):
    if doc and isinstance(doc.get("_id"), object) and not isinstance(doc.get("_id"), str):
        doc["_id"] = str(doc["_id"])
    return doc


# ---------- ownership helpers ----------
async def _current_user(request: Request) -> dict:
    """Return the authenticated user (or {}) — used to enforce content ownership."""
    uid = getattr(request.state, "user_id", None) or await optional_user_id(request)
    if not uid:
        return {}
    user = await db.users.find_one({"user_id": uid}, {"_id": 0}) or {}
    if not user:
        return {"user_id": uid}
    user["is_admin"] = (user.get("role") == "admin"
                       or user.get("email", "").lower() in admin_emails())
    return user


async def _owner_filter(request: Request) -> dict:
    """Return a Mongo filter enforcing per-account content isolation (admins bypass)."""
    user = await _current_user(request)
    if user.get("is_admin"):
        return {}
    uid = user.get("user_id") or ""
    # Empty owner_id string means "legacy pre-scoping data" — only the owner and admins
    # will see anything.  Anonymous requests see nothing.
    return {"owner_id": uid}


async def _owned_story(request: Request, story_id: str) -> dict:
    """Fetch a story only if the caller is its owner (or an admin).  404 otherwise."""
    filt = {"_id": story_id, **(await _owner_filter(request))}
    story = await db.stories.find_one(filt)
    if not story:
        raise HTTPException(404, "story not found")
    return story


async def _owned_book(request: Request, book_id: str) -> dict:
    filt = {"_id": book_id, **(await _owner_filter(request))}
    book = await db.books.find_one(filt)
    if not book:
        raise HTTPException(404, "book not found")
    return book


# ---------- health ----------
@router.get("/")
async def root():
    return {"message": "StoryForge API is running", "time": utcnow().isoformat()}


# ---------- channels ----------
@router.get("/channels")
async def list_channels():
    out = []
    async for c in db.channels.find().sort("created_at", 1):
        out.append(Channel.from_mongo(c).model_dump())
    return out


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: str, body: dict):
    allowed = {"name", "description", "language", "tone", "voice", "voice_speed", "music_mood",
               "music_volume", "safety_level", "style_prefix", "cta_text", "is_kids", "expressive_voice"}
    patch = {k: v for k, v in body.items() if k in allowed}
    res = await db.channels.update_one({"_id": channel_id}, {"$set": patch})
    if res.matched_count == 0:
        raise HTTPException(404, "channel not found")
    c = await db.channels.find_one({"_id": channel_id})
    return Channel.from_mongo(c).model_dump()


# ---------- books ----------
@router.post("/books/upload")
async def upload_book(request: Request, file: UploadFile = File(...), channel_id: str = Form(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    data = await file.read()
    if len(data) > 120 * 1024 * 1024:
        raise HTTPException(400, "PDF too large (max 120MB)")
    book = Book(filename=file.filename, channel_id=channel_id, size_bytes=len(data),
                owner_id=await optional_user_id(request))
    from services import storage
    result = storage.put_object(
        f"{storage.APP_NAME}/uploads/{book.id}/{file.filename}",
        data, "application/pdf")
    book.storage_path = result["path"]
    await db.books.insert_one(book.to_mongo())
    await enqueue("ocr", book.id, f"OCR: {file.filename}")
    return book.model_dump()


@router.get("/books")
async def list_books(request: Request):
    out = []
    async for b in db.books.find(await _owner_filter(request)).sort("created_at", -1):
        story_count = await db.stories.count_documents({"book_id": b["_id"]})
        d = Book.from_mongo(b).model_dump()
        d["story_count"] = story_count
        out.append(d)
    return out


@router.get("/books/{book_id}")
async def get_book(book_id: str, request: Request):
    b = await _owned_book(request, book_id)
    stories = []
    async for s in db.stories.find({"book_id": book_id}).sort("created_at", 1):
        stories.append(Story.from_mongo(s).model_dump())
    d = Book.from_mongo(b).model_dump()
    d["pages"] = d["pages"][:3]  # preview only
    d["stories"] = stories
    return d


# ---------- stories ----------
@router.post("/books/{book_id}/reocr")
async def reocr_book(book_id: str, request: Request):
    b = await _owned_book(request, book_id)
    if b["status"] in ("ocr_running", "segmenting"):
        raise HTTPException(409, "OCR already in progress")
    await db.books.update_one({"_id": book_id}, {"$set": {
        "status": "uploaded", "progress": 0, "pages": [], "structured": {},
        "error": "", "total_pages": 0}})
    job_id = await enqueue("ocr", book_id, f"Re-OCR: {b.get('filename', '')}")
    return {"job_id": job_id}


@router.get("/stories")
async def list_stories(request: Request, channel_id: Optional[str] = None, status: Optional[str] = None):
    q = dict(await _owner_filter(request))
    if channel_id and channel_id != "all":
        q["channel_id"] = channel_id
    if status and status != "all":
        q["status"] = status
    out = []
    async for s in db.stories.find(q).sort("created_at", -1):
        out.append(Story.from_mongo(s).model_dump())
    return out


@router.get("/stories/{story_id}")
async def get_story(story_id: str, request: Request):
    s = await _owned_story(request, story_id)
    return Story.from_mongo(s).model_dump()


@router.post("/stories/{story_id}/script")
async def gen_script(story_id: str, request: Request):
    s = await _owned_story(request, story_id)
    if s["status"] in ("scripting",):
        raise HTTPException(409, "script already being generated")
    await db.stories.update_one({"_id": story_id}, {"$set": {"status": "scripting", "error": ""}})
    job_id = await enqueue("script", story_id, f"Script: {s.get('title_english') or s.get('title_hindi', story_id)}")
    return {"job_id": job_id}


@router.post("/stories/{story_id}/produce")
async def produce(story_id: str, request: Request):
    s = await _owned_story(request, story_id)
    if not s.get("script", {}).get("chunks"):
        raise HTTPException(409, "generate the script first")
    await db.stories.update_one({"_id": story_id}, {"$set": {"status": "rendering", "stage": "Queued", "error": ""}})
    job_id = await enqueue("produce", story_id, f"Produce: {s.get('title_english') or s.get('title_hindi', story_id)}")
    return {"job_id": job_id}


@router.post("/stories/{story_id}/stop")
async def stop_story(story_id: str, request: Request):
    """Cancel the running/queued produce (or improve) job for this story."""
    await _owned_story(request, story_id)
    j = await db.jobs.find_one(
        {"ref_id": story_id, "type": {"$in": ["produce", "improve", "segment_fix", "edit_request"]},
         "status": {"$in": ["queued", "running"]}},
        sort=[("created_at", -1)])
    if not j:
        raise HTTPException(409, "no active render job for this story")
    from job_queue import cancel_job
    await db.jobs.update_one({"_id": j["_id"]}, {"$set": {
        "status": "cancelled", "error": "stopped by user", "finished_at": utcnow()}})
    cancel_job(j["_id"])
    await db.stories.update_one(
        {"_id": story_id, "status": "rendering"},
        {"$set": {"status": "script_ready", "stage": "Stopped by user", "error": ""}})
    return {"ok": True, "job_id": j["_id"]}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str, request: Request):
    from job_queue import cancel_job
    j = await db.jobs.find_one({"_id": job_id})
    if not j:
        raise HTTPException(404, "job not found")
    ref_id = j.get("ref_id") or ""
    if ref_id and ref_id != "system":
        # Only the owner (or admin) may cancel a job attached to a story.
        await _owned_story(request, ref_id)
    else:
        user = await _current_user(request)
        if not user.get("is_admin"):
            raise HTTPException(404, "job not found")
    await db.jobs.update_one({"_id": job_id, "status": {"$in": ["queued", "running"]}},
                             {"$set": {"status": "cancelled", "error": "stopped by user",
                                       "finished_at": utcnow()}})
    cancel_job(job_id)
    if ref_id and ref_id != "system":
        await db.stories.update_one(
            {"_id": ref_id, "status": "rendering"},
            {"$set": {"status": "script_ready", "stage": "Stopped by user", "error": ""}})
    return {"ok": True}


class PauseBody(BaseModel):
    paused: bool


@router.post("/queue/pause")
async def queue_pause(body: PauseBody):
    from job_queue import QUEUE_PAUSED
    QUEUE_PAUSED["paused"] = bool(body.paused)
    await db.settings.update_one({"key": "queue"},
                                 {"$set": {"paused": bool(body.paused)}}, upsert=True)
    return {"paused": QUEUE_PAUSED["paused"]}


@router.get("/settings/scheduler")
async def get_scheduler():
    doc = await db.settings.find_one({"key": "scheduler"}) or {}
    return {"engagement_hours": float(doc.get("engagement_hours", 6.0)),
            "news_hours": float(doc.get("news_hours", 6.0)),
            "queue_paused": QUEUE_PAUSED["paused"]}


class SchedulerBody(BaseModel):
    engagement_hours: Optional[float] = None
    news_hours: Optional[float] = None


@router.put("/settings/scheduler")
async def put_scheduler(body: SchedulerBody):
    patch = {}
    if body.engagement_hours is not None:
        patch["engagement_hours"] = max(0.25, min(72.0, float(body.engagement_hours)))
    if body.news_hours is not None:
        patch["news_hours"] = max(0.5, min(72.0, float(body.news_hours)))
    if patch:
        await db.settings.update_one({"key": "scheduler"},
                                     {"$set": {**patch, "updated_at": utcnow()}}, upsert=True)
    return await get_scheduler()


class SegmentRegenerateBody(BaseModel):
    kind: str = "all"  # script | voice | visual | all
    notes: str = ""    # optional review comments (used by the Studio image worker)


@router.post("/stories/{story_id}/segments/{index}/regenerate")
async def regen_segment(story_id: str, index: int, request: Request, body: Optional[SegmentRegenerateBody] = None):
    s = await _owned_story(request, story_id)
    chunks = (s.get("script") or {}).get("chunks") or []
    if index < 0 or index >= len(chunks):
        raise HTTPException(400, "invalid segment index")
    kind = (body.kind if body else "all").strip().lower()
    if kind not in {"script", "voice", "visual", "all"}:
        raise HTTPException(400, "kind must be script|voice|visual|all")
    job_id = await enqueue("segment_fix", story_id, f"Regenerate {kind} · segment {index + 1}",
                           payload={"index": index, "kind": kind, "notes": (body.notes if body else "").strip()[:2000]})
    return {"job_id": job_id}


@router.post("/stories/{story_id}/improve")
async def improve_story(story_id: str, request: Request):
    s = await _owned_story(request, story_id)
    if not s.get("script", {}).get("chunks"):
        raise HTTPException(409, "generate the script first")
    if len(s.get("improvements") or []) >= 2:
        raise HTTPException(409, "improvement budget exhausted (2 rounds max)")
    if s["status"] == "rendering":
        raise HTTPException(409, "pipeline busy")
    await db.stories.update_one({"_id": story_id}, {"$set": {
        "status": "rendering", "stage": "Improvement coach queued", "error": ""}})
    job_id = await enqueue("improve", story_id,
                           f"Improve: {s.get('title_english') or s.get('title_hindi', story_id)[:40]}")
    return {"job_id": job_id}


# ---------- prompt / script based creation ----------
class CreateBody(BaseModel):
    title: str = ""
    source_text: str
    video_type: str = "mythology_moral"
    length_seconds: int = 90
    mode: str = "slide"  # slide | clip


@router.post("/stories/create")
async def create_story(body: CreateBody, request: Request):
    from script_parser import parse_scene_script
    from services.video_types import VIDEO_TYPES
    cfg = VIDEO_TYPES.get(body.video_type)
    if not cfg:
        raise HTTPException(400, "unknown video_type")
    if not body.source_text.strip():
        raise HTTPException(400, "paste a script or prompt first")
    target = max(30, min(240, int(body.length_seconds or 90)))
    mode = body.mode if body.mode in ("slide", "clip", "storyboard") else "slide"
    key = f"vt-{body.video_type}"
    ch = await db.channels.find_one({"key": key})
    if not ch:
        ch_doc = Channel(key=key, name=cfg["name"],
                         description=f"{cfg['name']} ({cfg['audience']}) — voice & music auto-selected",
                         language=cfg["language"], tone=cfg["tone"], voice=cfg["voice"],
                         music_mood=cfg["music_mood"], music_volume=cfg["music_volume"],
                         safety_level=cfg["safety_level"], is_kids=cfg["is_kids"],
                         style_prefix=cfg["style_prefix"], cta_text=cfg["cta_text"],
                         mode=mode, video_type=body.video_type)
        await db.channels.insert_one(ch_doc.to_mongo())
        ch = await db.channels.find_one({"key": key})
    parsed = parse_scene_script(body.source_text)
    supplied_title = body.title.strip() or ((parsed or {}).get("title") or "")
    bible = ((parsed or {}).get("character_sheet") or "").strip()
    owner_id = await optional_user_id(request)
    script_payload = {}
    if parsed:
        script_payload = {
            "chunks": parsed["chunks"],
            "production_notes": parsed["production_notes"],
            "imported_verbatim": True,
            "character_sheet": {"anchor": bible},
            "missing": parsed["missing"],
            "is_partial": parsed["is_partial"],
        }
    story = Story(book_id=f"prompt-{utcnow().strftime('%Y%m%d-%H%M%S')}", channel_id=ch["_id"],
                  owner_id=owner_id,
                  title_hindi=supplied_title, title_english=supplied_title[:100],
                  source="Pasted script / prompt", category=cfg["name"],
                  target_seconds=target, mode=mode,
                  source_text=body.source_text.strip()[:20000],
                  emotional_tone=cfg["tone"][:60], target_audience=cfg["audience"],
                  visual_style=cfg["style_prefix"][:120], estimated_length=f"{target}s",
                  status="script_ready" if parsed else "draft",
                  stage=(f"Loaded {len(parsed['chunks'])} supplied scenes"
                         + (" · gaps queued for AI fill" if parsed and parsed["is_partial"] else "")
                         if parsed else ""),
                  script=script_payload,
                  character_sheet={"anchor": bible, "text": bible, "locked": bool(bible),
                                   "visuals_stale": False, "version": 1} if parsed else {})
    await db.stories.insert_one(story.to_mongo())
    if parsed:
        return {"story_id": story.id, "job_id": None, "imported": True,
                "imported_segments": len(parsed["chunks"]),
                "is_partial": parsed["is_partial"],
                "missing": parsed["missing"]}
    job_id = await enqueue("script", story.id, f"Script: {supplied_title[:40] or story.id}")
    return {"story_id": story.id, "job_id": job_id, "imported": False, "imported_segments": 0,
            "is_partial": False, "missing": {"voiceover": [], "visual": [], "video_prompt": []}}


@router.get("/video-types")
async def video_types():
    from services.video_types import VIDEO_TYPES
    return [{"key": k, **v} for k, v in VIDEO_TYPES.items()]


class ScriptPatchBody(BaseModel):
    chunks: List[dict]


@router.patch("/stories/{story_id}/script")
async def patch_script(story_id: str, body: ScriptPatchBody, request: Request):
    s = await _owned_story(request, story_id)
    if s["status"] == "rendering":
        raise HTTPException(409, "pipeline busy — wait for the render to finish")
    chunks = (s.get("script") or {}).get("chunks") or []
    changed = set()
    for edit in body.chunks:
        if not isinstance(edit, dict):
            continue
        try:
            idx = int(edit.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(chunks):
            for k in ("voiceover", "visual", "video_prompt", "camera", "emotion"):
                if edit.get(k) is not None:
                    chunks[idx][k] = str(edit[k])[:2000]
                    changed.add(idx)
    for i in changed:  # invalidate caches so re-render picks up edits
        for sub, ext in (("audio", "mp3"), ("frames", "png")):
            (MEDIA_ROOT / sub / story_id / f"{i:02d}.{ext}").unlink(missing_ok=True)
        (MEDIA_ROOT / "clips" / story_id / f"{i:02d}.mp4").unlink(missing_ok=True)
    s["script"]["chunks"] = chunks
    await db.stories.update_one({"_id": story_id}, {"$set": {
        "script": s["script"],
        "status": "edits_requested" if changed else s.get("status", "script_ready"),
        "stage": "Edits saved — ready to re-render" if changed else s.get("stage", "Script ready"),
        "updated_at": utcnow()}})
    return {"ok": True, "edited": sorted(changed)}


class ConfigBody(BaseModel):
    mode: Optional[str] = None
    target_seconds: Optional[int] = None


@router.put("/stories/{story_id}/config")
async def story_config(story_id: str, body: ConfigBody, request: Request):
    import shutil

    s = await _owned_story(request, story_id)
    if s["status"] == "rendering":
        raise HTTPException(409, "pipeline busy")
    patch = {}
    if body.mode in ("slide", "clip", "storyboard") and body.mode != s.get("mode"):
        # mode switch invalidates AI clips (slide mode ignores them, clip mode needs fresh ones)
        clips_dir = MEDIA_ROOT / "clips" / story_id
        if clips_dir.exists():
            shutil.rmtree(clips_dir, ignore_errors=True)
        patch["mode"] = body.mode
    if body.target_seconds is not None:
        patch["target_seconds"] = max(30, min(240, int(body.target_seconds)))
    if patch:
        patch["updated_at"] = utcnow()
        await db.stories.update_one({"_id": story_id}, {"$set": patch})
    return {"ok": True, **patch}


class CharacterSheetBody(BaseModel):
    anchor: str


@router.patch("/stories/{story_id}/character-sheet")
async def update_character_sheet(story_id: str, body: CharacterSheetBody, request: Request):
    story = await _owned_story(request, story_id)
    if story.get("status") == "rendering":
        raise HTTPException(409, "pipeline busy — wait for the render to finish")
    anchor = body.anchor.strip()
    if len(anchor) < 80:
        raise HTTPException(400, "consistency sheet must be at least 80 characters")
    anchor = anchor[:12000]

    script = story.get("script") or {}
    script_sheet = script.get("character_sheet") or {}
    script["character_sheet"] = {**script_sheet, "anchor": anchor}
    version = int((story.get("character_sheet") or {}).get("version") or 0) + 1
    sheet = {"anchor": anchor, "text": anchor, "locked": True, "visuals_stale": True, "version": version,
             "updated_at": utcnow().isoformat()}
    await db.stories.update_one({"_id": story_id}, {"$set": {
        "character_sheet": sheet,
        "script": script,
        "status": "edits_requested",
        "stage": "Consistency sheet saved — all visuals will regenerate",
        "updated_at": utcnow(),
    }})
    return {"ok": True, "character_sheet": sheet, "invalidated": ["character_reference", "frames", "clips"]}


class ReviewBody(BaseModel):
    action: str  # approve | reject | request_edits
    notes: str = ""


@router.post("/stories/{story_id}/review")
async def review(story_id: str, body: ReviewBody, request: Request):
    s = await _owned_story(request, story_id)
    mapping = {"approve": "approved", "reject": "rejected", "request_edits": "edits_requested"}
    if body.action not in mapping:
        raise HTTPException(400, "action must be approve|reject|request_edits")
    if body.action == "request_edits" and not body.notes.strip():
        raise HTTPException(400, "edit notes are required")
    stage = {"approve": "Ready for upload", "reject": "Rejected",
             "request_edits": "Edit request queued"}[body.action]
    await db.stories.update_one(
        {"_id": story_id},
        {"$set": {"status": mapping[body.action], "review_notes": body.notes,
                  "stage": stage, "updated_at": utcnow()}})
    job_id = None
    if body.action == "request_edits":
        job_id = await enqueue("edit_request", story_id, "Apply requested story edits",
                               payload={"notes": body.notes.strip()})
    response = await get_story(story_id, request)
    if isinstance(response, dict):
        response["edit_job_id"] = job_id
    return response


# ---------- jobs & dashboard ----------
async def _visible_story_ids(request: Request) -> Optional[set]:
    """Return the set of story IDs the caller can see, or ``None`` for admins."""
    user = await _current_user(request)
    if user.get("is_admin"):
        return None
    uid = user.get("user_id") or ""
    ids: set[str] = set()
    async for s in db.stories.find({"owner_id": uid}, {"_id": 1}):
        ids.add(s["_id"])
    return ids


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 30):
    ids = await _visible_story_ids(request)
    filt: dict = {} if ids is None else {"$or": [
        {"ref_id": {"$in": list(ids)}},
        {"ref_id": "system"},
    ]}
    out = []
    async for j in db.jobs.find(filt).sort("created_at", -1).limit(min(limit, 100)):
        d = fix(dict(j))
        for f in ("created_at", "started_at", "finished_at"):
            if isinstance(d.get(f), datetime):
                d[f] = d[f].isoformat()
        out.append(d)
    return out


@router.get("/dashboard")
async def dashboard(request: Request):
    story_filter = await _owner_filter(request)
    ids = await _visible_story_ids(request)
    if ids is None:
        jobs_filter: dict = {}
    else:
        jobs_filter = {"$or": [{"ref_id": {"$in": list(ids)}}, {"ref_id": "system"}]}

    status_counts = {}
    async for s in db.stories.find(story_filter, {"status": 1}):
        status_counts[s["status"]] = status_counts.get(s["status"], 0) + 1

    pipeline_t = [
        {"$match": story_filter} if story_filter else {"$match": {}},
        {"$group": {"_id": None, "llm": {"$sum": "$cost.llm"}, "tts": {"$sum": "$cost.tts"},
                    "image": {"$sum": "$cost.image"}, "total": {"$sum": "$cost.total"},
                    "n": {"$sum": 1},
                    "produced": {"$sum": {"$cond": [
                        {"$in": ["$status", ["in_review", "approved", "edits_requested"]]},
                        1, 0]}}}}]
    agg = await db.stories.aggregate(pipeline_t).to_list(1)
    cost = agg[0] if agg else {"llm": 0, "tts": 0, "image": 0, "total": 0, "n": 0, "produced": 0}
    avg = round(cost["total"] / cost["produced"], 3) if cost["produced"] else 0
    jobs = []
    async for j in db.jobs.find(jobs_filter).sort("created_at", -1).limit(25):
        d = fix(dict(j))
        for f in ("created_at", "started_at", "finished_at"):
            if isinstance(d.get(f), datetime):
                d[f] = d[f].isoformat()
        jobs.append(d)

    job_counts = {}
    async for j in db.jobs.find(jobs_filter, {"status": 1}):
        job_counts[j["status"]] = job_counts.get(j["status"], 0) + 1

    beat_coverage = {"hook": 0, "story": 0, "twist": 0, "climax": 0, "action": 0, "lesson": 0}
    async for s in db.stories.find({**story_filter, "script.chunks": {"$exists": True, "$ne": []}},
                                   {"script.chunks.beat": 1}):
        seen = {c.get("beat") for c in s.get("script", {}).get("chunks", []) if isinstance(c, dict)}
        for b in beat_coverage:
            if b in seen:
                beat_coverage[b] += 1

    recent = []
    async for s in db.stories.find(story_filter).sort("updated_at", -1).limit(8):
        recent.append(Story.from_mongo(s).model_dump())

    return {
        "status_counts": status_counts,
        "cost": {"llm": round(cost["llm"], 3), "tts": round(cost["tts"], 3),
                 "image": round(cost["image"], 3), "total": round(cost["total"], 3),
                 "avg_per_video": avg, "videos_produced": cost["produced"], "n": cost["n"]},
        "beat_coverage": beat_coverage,
        "queue": {"depth": await db.jobs.count_documents({**jobs_filter, "status": "queued"}),
                  "active": HEARTBEAT["active"],
                  "workers": HEARTBEAT["workers"],
                  "paused": QUEUE_PAUSED["paused"],
                  "last_beat": HEARTBEAT["last_beat"].isoformat() if HEARTBEAT["last_beat"] else None,
                  "job_counts": job_counts},
        "jobs": jobs,
        "recent_stories": recent,
    }


# ---------- bulk curation ----------
@router.post("/books/{book_id}/script-all")
async def script_all(book_id: str, request: Request):
    await _owned_book(request, book_id)
    n = 0
    async for s in db.stories.find({"book_id": book_id, "status": "draft"}, {"_id": 1, "title_english": 1}):
        await enqueue("script", s["_id"], f"Script: {s.get('title_english', '')[:40]}")
        n += 1
    if n == 0:
        raise HTTPException(409, "no draft stories left to script")
    return {"queued": n}


class BatchBody(BaseModel):
    ids: List[str]


@router.post("/stories/batch-produce")
async def batch_produce(body: BatchBody, request: Request):
    owner_filter = await _owner_filter(request)
    queued = []
    for sid in body.ids[:30]:
        s = await db.stories.find_one({"_id": sid, **owner_filter})
        if not s or not s.get("script", {}).get("chunks"):
            continue
        if s["status"] in ("rendering",):
            continue
        await db.stories.update_one({"_id": sid}, {"$set": {"status": "rendering", "stage": "Queued", "error": ""}})
        await enqueue("produce", sid, f"Produce: {s.get('title_english') or s.get('title_hindi', sid)[:40]}")
        queued.append(sid)
    if not queued:
        raise HTTPException(409, "no script-ready stories in selection")
    return {"queued": len(queued)}


# ---------- publishing ----------
class PublishBody(BaseModel):
    platform: str  # youtube | instagram


@router.post("/stories/{story_id}/publish")
async def publish_story(story_id: str, body: PublishBody, request: Request):
    await _owned_story(request, story_id)
    if body.platform not in ("youtube", "instagram"):
        raise HTTPException(400, "platform must be youtube|instagram")
    job_id = await enqueue("publish", story_id, f"Publish to {platform_label(body.platform)}",
                           payload={"platform": body.platform})
    return {"job_id": job_id}


def platform_label(p):
    return {"youtube": "YouTube", "instagram": "Instagram Reels"}.get(p, p)


@router.get("/social/credentials")
async def social_credentials():
    from services import social
    return social.cred_status()


@router.get("/social/comments")
async def social_comments():
    out = []
    async for c in db.social_comments.find().sort("created_at", -1).limit(80):
        d = fix(dict(c))
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


class ReplyBody(BaseModel):
    text: str


@router.post("/social/comments/{comment_id}/reply")
async def manual_reply(comment_id: str, body: ReplyBody):
    doc = await db.social_comments.find_one({"comment_id": comment_id})
    if not doc:
        raise HTTPException(404, "comment not tracked")
    from services import social
    try:
        if doc["platform"] == "youtube":
            social.yt_reply(comment_id, body.text)
        else:
            social.ig_reply(comment_id, body.text)
    except Exception as e:
        raise HTTPException(502, f"reply failed: {str(e)[:200]}")
    await db.social_comments.update_one(
        {"comment_id": comment_id},
        {"$set": {"reply_text": body.text, "posted": True, "handled": True,
                  "triage.action": "manual_reply", "triage.needs_reply": True}})
    return {"ok": True}


@router.post("/social/engagement/sync")
async def engagement_sync():
    job_id = await enqueue("engagement", "system", "Manual engagement sync")
    return {"job_id": job_id}


# ---------- per-user API key vault ----------
# Each account keeps its own provider keys — completely isolated from other users.
# PROVIDER_KEYS is the authoritative list; keys outside it are silently ignored.
from services.keys import PROVIDER_KEYS as _PROVIDER_KEYS

# Keep backwards compat alias used by server.py _load_key_vault
ALLOWED_VAULT_KEYS = set(_PROVIDER_KEYS) | {
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID",
}

KEY_LABELS = {
    "OPENAI_API_KEY":         "OpenAI — DALL·E / GPT image generation",
    "GEMINI_API_KEY":         "Google Gemini — Veo video, image generation, TTS",
    "GEMINI_API_KEY":         "Google Gemini — Veo video, image generation, TTS",
    "FAL_KEY":                "fal.ai — FLUX / SDXL images, Wan video clips",
    "REPLICATE_API_TOKEN":    "Replicate — FLUX images, Wan 2.1 clips",
    "HF_TOKEN":               "Hugging Face — free hosted image / text APIs",
    "STABILITY_API_KEY":      "Stability AI — Stable Image Core / SD3.5",
    "PEXELS_API_KEY":         "Pexels — free real photos & video clips",
    "STUDIO_API_TOKEN":       "Studio HTTP API token (future remote worker)",
}


@router.get("/settings/api-keys")
async def get_api_keys(request: Request):
    """Return the signed-in account's configured key names (values never returned)."""
    uid = getattr(request.state, "user_id", None) or await optional_user_id(request)
    from services import keys as _keys
    if uid:
        configured = await _keys.configured_for_owner(uid)
    else:
        # Unauthenticated: show env-level status (admin convenience)
        configured = {k: bool((os.environ.get(k) or "").strip()) for k in _PROVIDER_KEYS}
    return {
        k: {
            "set":   configured.get(k, False),
            "label": KEY_LABELS.get(k, k),
            "hint":  "configured" if configured.get(k) else "",
        }
        for k in sorted(_PROVIDER_KEYS)
    }


class KeysBody(BaseModel):
    values: dict


@router.put("/settings/api-keys")
async def save_api_keys(body: KeysBody, request: Request):
    """Save provider keys into the signed-in account's vault."""
    from services import keys as _keys, social
    uid = getattr(request.state, "user_id", None) or await optional_user_id(request)
    if not uid:
        raise HTTPException(401, "sign in to save API keys")
    saved = await _keys.save_for_owner(uid, body.values or {})
    if saved:
        # Reload active context so the current request sees the new keys
        vals = await _keys.load_for_owner(uid)
        _keys.set_active(vals)
        from services.generation import reset_provider_health
        reset_provider_health()
        if "INSTAGRAM_ACCESS_TOKEN" in saved or "INSTAGRAM_USER_ID" in saved:
            ak = await _keys.load_for_owner(uid)
            social.set_ig_creds(ak.get("INSTAGRAM_ACCESS_TOKEN", ""),
                                ak.get("INSTAGRAM_USER_ID", ""), "vault")
    return {"saved": saved}


@router.delete("/settings/api-keys/{name}")
async def delete_api_key(name: str, request: Request):
    """Wipe one provider key from the signed-in account's vault."""
    from services import keys as _keys, social
    if name not in _PROVIDER_KEYS:
        raise HTTPException(400, "unknown key")
    uid = getattr(request.state, "user_id", None) or await optional_user_id(request)
    if not uid:
        raise HTTPException(401, "sign in to clear API keys")
    await _keys.clear_for_owner(uid, name)
    from services.generation import reset_provider_health
    reset_provider_health()
    if name in ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"):
        ak = await _keys.load_for_owner(uid)
        social.set_ig_creds(ak.get("INSTAGRAM_ACCESS_TOKEN", ""),
                            ak.get("INSTAGRAM_USER_ID", ""), "vault")
    return {"cleared": name}


@router.get("/settings/instagram")
async def get_ig_settings():
    from services import social
    creds = social.get_ig_creds()
    connected = bool(creds.get("access_token") and creds.get("user_id"))
    out = {"connected": connected, "user_id": creds.get("user_id", "") if connected else "",
           "token_hint": (creds.get("access_token", "")[:6] + "…") if creds.get("access_token") else "",
           "source": creds.get("source", "")}
    if connected:
        try:
            out["username"] = await asyncio.to_thread(social.ig_validate, creds["access_token"], creds["user_id"])
        except Exception as e:
            out["error"] = f"token check failed: {str(e)[:140]}"
    return out


class InstagramCredsBody(BaseModel):
    access_token: str
    user_id: str


@router.put("/settings/instagram")
async def save_ig_settings(body: InstagramCredsBody):
    from services import social
    token, uid = body.access_token.strip(), body.user_id.strip()
    if not token or not uid:
        raise HTTPException(400, "access_token and user_id are required")
    try:
        username = await asyncio.to_thread(social.ig_validate, token, uid)
    except Exception as e:
        raise HTTPException(400, f"Instagram rejected these credentials: {str(e)[:200]}")
    await db.settings.update_one(
        {"key": "instagram"},
        {"$set": {"key": "instagram", "access_token": token, "user_id": uid, "updated_at": utcnow()}},
        upsert=True)
    social.set_ig_creds(token, uid, "settings")
    return {"connected": True, "username": username, "user_id": uid}


@router.delete("/settings/instagram")
async def delete_ig_settings():
    from services import social
    await db.settings.delete_one({"key": "instagram"})
    social.set_ig_creds("", "", "")
    return {"connected": False}


# ---------- news desk ----------
@router.post("/news/fetch")
async def news_fetch():
    job_id = await enqueue("news", "system", "Fetch & triage daily news")
    return {"job_id": job_id}


@router.get("/news/items")
async def news_items(request: Request):
    out = []
    q = {"policy": {"$ne": {}}, **(await _owner_filter(request))}
    async for s in db.stories.find(q).sort("created_at", -1).limit(60):
        out.append(Story.from_mongo(s).model_dump())
    return out


@router.get("/social/youtube/auth-url")
async def yt_auth_url():
    from services import social
    if not social.cred_status()["youtube_oauth_setup"]:
        raise HTTPException(400, "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET missing in backend/.env")
    base = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
    return {"auth_url": social.youtube_auth_url(f"{base}/api/oauth/callback"),
            "redirect_uri": f"{base}/api/oauth/callback",
            "note": "Add this redirect URI in Google Cloud Console > Credentials > your OAuth client, then open auth_url, approve, and the refresh token is saved automatically"}


@router.get("/oauth/callback")
async def oauth_callback(code: str = None, error: str = None):
    from fastapi.responses import HTMLResponse
    from services import social
    if error:
        return HTMLResponse(f"<h3>OAuth failed: {error}</h3>", status_code=400)
    base = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
    try:
        social.youtube_exchange_code(code, f"{base}/api/oauth/callback")
        return HTMLResponse("<h2 style='font-family:sans-serif'>✓ YouTube connected — refresh token saved. You can close this tab and upload Shorts directly.</h2>")
    except Exception as e:
        return HTMLResponse(f"<h3 style='font-family:sans-serif'>Token exchange failed: {str(e)[:300]}</h3>", status_code=400)


@router.get("/router-status")
async def router_status():
    from services import router
    return router.status()


@router.get("/media-health")
async def media_health():
    return {"media_root": str(MEDIA_ROOT), "exists": MEDIA_ROOT.exists()}


# ---------- stitch my own media ----------
STITCH_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
STITCH_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".m4v"}
STITCH_MAX_FILE = 150 * 1024 * 1024
STITCH_MAX_TOTAL = 250 * 1024 * 1024


@router.post("/stitch/upload")
async def stitch_upload(file: UploadFile = File(...)):
    import uuid

    ext = Path(file.filename or "").suffix.lower()
    if ext not in (STITCH_IMAGE_EXTS | STITCH_VIDEO_EXTS):
        raise HTTPException(400, "file must be an image (jpg/png/webp) or video (mp4/mov/webm/avi)")
    data = await file.read()
    if len(data) > STITCH_MAX_FILE:
        raise HTTPException(400, "file too large (max 150MB)")
    media_id = uuid.uuid4().hex[:16]
    dst = MEDIA_ROOT / "stitch" / f"{media_id}{ext}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    kind = "video" if ext in STITCH_VIDEO_EXTS else "image"
    from services import media, storage
    try:  # keep a second copy in the storage dir so the media dir isn't the only one
        storage.put_object(f"{storage.APP_NAME}/stitch/{media_id}{ext}", data,
                           "video/mp4" if kind == "video" else "image/jpeg")
    except Exception as e:
        print(f"[stitch] storage copy failed (keeping local copy): {str(e)[:120]}", flush=True)
    return {"media_id": media_id, "kind": kind, "size": len(data),
            "duration": round(media.ffprobe_duration(dst), 2) if kind == "video" else None}


class StitchItemIn(BaseModel):
    media_id: str
    narration: str = ""
    duration: Optional[float] = None


class StitchBody(BaseModel):
    title: str = ""
    video_type: str = "mythology_moral"
    items: List[StitchItemIn]
    music: bool = True
    endcard: bool = True
    beat_sync: bool = True


@router.post("/stitch")
async def stitch_create(body: StitchBody, request: Request):
    import re

    from services.video_types import VIDEO_TYPES

    if not body.items:
        raise HTTPException(400, "upload at least one image or video clip")
    if len(body.items) > 30:
        raise HTTPException(400, "max 30 media items per video")
    cfg = VIDEO_TYPES.get(body.video_type)
    if not cfg:
        raise HTTPException(400, "unknown video_type")

    chunks, total = [], 0
    for it in body.items:
        if not re.fullmatch(r"[0-9a-f]{16}", it.media_id):
            raise HTTPException(400, "invalid media_id")
        srcs = sorted((MEDIA_ROOT / "stitch").glob(f"{it.media_id}.*"))
        if not srcs:
            raise HTTPException(404, f"upload not found: {it.media_id} — re-upload the file")
        src = srcs[0]
        total += src.stat().st_size
        kind = "video" if src.suffix.lower() in STITCH_VIDEO_EXTS else "image"
        dur = None
        if kind == "image":
            dur = max(1.0, min(20.0, float(it.duration or 4.0)))
        elif it.duration:
            dur = max(1.0, min(60.0, float(it.duration)))
        chunks.append({"voiceover": (it.narration or "").strip()[:2000],
                       "media_path": str(src), "media_kind": kind, "duration": dur,
                       "beat": "story", "camera": "zoom_in"})
    if total > STITCH_MAX_TOTAL:
        raise HTTPException(400, "total media too large (max 250MB per video)")

    key = f"vt-{body.video_type}"
    ch = await db.channels.find_one({"key": key})
    if not ch:
        ch_doc = Channel(key=key, name=cfg["name"],
                         description=f"{cfg['name']} ({cfg['audience']}) — voice & music auto-selected",
                         language=cfg["language"], tone=cfg["tone"], voice=cfg["voice"],
                         music_mood=cfg["music_mood"], music_volume=cfg["music_volume"],
                         safety_level=cfg["safety_level"], is_kids=cfg["is_kids"],
                         style_prefix=cfg["style_prefix"], cta_text=cfg["cta_text"],
                         mode="slide", video_type=body.video_type)
        await db.channels.insert_one(ch_doc.to_mongo())
        ch = await db.channels.find_one({"key": key})

    story = Story(book_id=f"stitch-{utcnow().strftime('%Y%m%d-%H%M%S')}", channel_id=ch["_id"],
                  owner_id=await optional_user_id(request),
                  title_hindi=body.title.strip(),
                  title_english=body.title.strip()[:100] or "My stitched video",
                  source="Uploaded images & clips", category=cfg["name"], mode="stitch",
                  target_audience=cfg["audience"], visual_style=cfg["style_prefix"][:120],
                  script={"chunks": chunks, "music": body.music, "endcard": body.endcard,
                          "beat_sync": body.beat_sync},
                  status="script_ready", stage="Queued for stitching")
    await db.stories.insert_one(story.to_mongo())
    job_id = await enqueue("produce", story.id, f"Stitch: {(body.title or 'my media')[:40]}")
    return {"story_id": story.id, "job_id": job_id}


# ---------- voice preview ----------
PREVIEW_LINES = {"hi": "नमस्कार! यह आवाज़ आपकी हर कहानी को जीवंत कर देगी।",
                 "bn": "নমস্কার! এই কণ্ঠই আপনার গল্পকে প্রাণ দেবে।",
                 "en": "Hi! This is exactly how your stories will sound."}


class VoicePreviewBody(BaseModel):
    voice: str = "local:auto"
    language: str = "hi"
    tone: str = ""


@router.post("/tts/preview")
async def tts_preview(body: VoicePreviewBody):
    """Short expressive sample of a voice so it can be auditioned before rendering (cached)."""
    import hashlib

    from services import media

    lang = (body.language or "hi").split("-")[0]
    text = PREVIEW_LINES.get(lang, PREVIEW_LINES["en"])
    # Do not reuse samples produced by the old implicit Gemini-expressive path.
    key = hashlib.sha1(f"local-tts-v1|{body.voice}|{lang}|{body.tone}".encode()).hexdigest()[:12]
    out = MEDIA_ROOT / "tmp" / f"voice-preview-{key}.mp3"
    lock = _PREVIEW_LOCKS.setdefault(key, asyncio.Lock())
    if not out.exists() or out.stat().st_size == 0:
        async with lock:
            if not out.exists() or out.stat().st_size == 0:
                direction = (body.tone or "").strip() or "warm dramatic storyteller, medium pace"
                try:
                    await media.synthesize_voice(text, body.voice, 1.0, out, lang_hint=lang,
                                                 direction=direction, expressive=True)
                except HTTPException:
                    raise
                except Exception as e:
                    out.unlink(missing_ok=True)
                    raise HTTPException(502, f"voice preview failed: {str(e)[:160]}")
    return {"url": f"/api/media/tmp/{out.name}", "voice": body.voice, "language": lang}


_PREVIEW_LOCKS: dict = {}
