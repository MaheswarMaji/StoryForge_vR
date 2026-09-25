import asyncio
import json
import os

from db import db
from models import Book, Story, utcnow
from services import agents, ocr


async def run_ocr_job(job, setp):
    book_id = job["ref_id"]
    await db.books.update_one({"_id": book_id}, {"$set": {"status": "ocr_running", "progress": 2, "error": ""}})
    total, langs = await ocr.run_ocr(book_id, setp)
    await db.books.update_one({"_id": book_id}, {"$set": {"status": "segmenting", "progress": 100}})
    from job_queue import enqueue
    await enqueue("segment", book_id, f"Segment stories: {book_id}")


async def run_segment_job(job, setp):
    book_id = job["ref_id"]
    book = await db.books.find_one({"_id": book_id})
    if not book or not book.get("pages"):
        raise RuntimeError("book has no OCR text")

    await setp(5, "Analyzing text")
    full_text = "\n\n".join(
        f"[Page {p['page']}]\n{p['text']}" for p in book.get("pages", []) if p.get("text"))
    channel = await db.channels.find_one({"_id": book["channel_id"]})
    stories, cost = await agents.identify_stories(
        book.get("filename", ""), full_text, (channel or {}).get("name", "Story channel"))

    await setp(70, f"Saving {len(stories)} stories")
    n = 0
    for i, s in enumerate(stories[:12]):
        if not isinstance(s, dict) or not (s.get("title_hindi") or s.get("title_english")):
            continue
        story = Story(
            book_id=book_id, channel_id=book["channel_id"],
            title_hindi=str(s.get("title_hindi", "")).strip(),
            title_english=str(s.get("title_english", "")).strip(),
            source=str(s.get("source", "")),
            page_start=int(s.get("page_start") or 0), page_end=int(s.get("page_end") or 0),
            category=str(s.get("category", "Folk")),
            characters=s.get("characters") if isinstance(s.get("characters"), list) else [],
            setting=str(s.get("setting", "")), conflict=str(s.get("conflict", "")),
            twist=str(s.get("twist", "")), climax=str(s.get("climax", "")),
            resolution=str(s.get("resolution", "")), moral=str(s.get("moral", "")),
            emotional_tone=str(s.get("emotional_tone", "")),
            target_audience=str(s.get("target_audience", "General")),
            visual_style=str(s.get("visual_style", "")),
            estimated_length=str(s.get("estimated_length", "90s")),
            cost={"llm": round(cost / max(len(stories), 1), 5), "tts": 0.0, "image": 0.0,
                  "total": round(cost / max(len(stories), 1), 5)},
        )
        await db.stories.insert_one(story.to_mongo())
        n += 1
    await db.books.update_one({"_id": book_id}, {"$set": {"status": "segmented", "progress": 100}})

    # auto-generate scripts for every extracted story; user picks which go to production later
    from job_queue import enqueue
    async for s in db.stories.find({"book_id": book_id, "status": "draft"}, {"_id": 1, "title_english": 1}):
        await enqueue("script", s["_id"], f"Script: {s.get('title_english', '')[:40]}")


async def run_script_job(job, setp):
    story_id = job["ref_id"]
    story = await db.stories.find_one({"_id": story_id})
    if not story:
        raise RuntimeError("story not found")
    channel = await db.channels.find_one({"_id": story["channel_id"]})
    await db.stories.update_one({"_id": story_id},
                                {"$set": {"status": "scripting", "stage": "Writing script", "error": ""}})
    await setp(20, "Writing retention script")
    script, cost = await agents.write_script(story, channel or {},
                                             int(story.get("target_seconds") or 90))
    cs = script.get("character_sheet") or {}
    current_sheet = story.get("character_sheet") or {}
    anchor = (current_sheet.get("anchor") if current_sheet.get("locked") else "") or cs.get("anchor", "")
    script["character_sheet"] = {**cs, "anchor": anchor}
    update = {
        "script": script, "status": "script_ready", "stage": "Script ready",
        "character_sheet": {
            "anchor": anchor,
            "text": anchor,
            "locked": bool(current_sheet.get("locked")),
            "version": int(current_sheet.get("version") or 1),
        },
        "updated_at": utcnow(),
    }
    await db.stories.update_one({"_id": story_id}, {"$set": update, "$inc": {
        "cost.llm": round(cost, 5), "cost.total": round(cost, 5)}})


async def run_script_segment_job(job, setp):
    """LLM-segment a pasted freeform script into scenes + cast + character sheet (CPU model)."""
    story_id = job["ref_id"]
    story = await db.stories.find_one({"_id": story_id})
    if not story:
        raise RuntimeError("story not found")
    channel = await db.channels.find_one({"_id": story["channel_id"]})
    await db.stories.update_one({"_id": story_id},
        {"$set": {"status": "scripting", "stage": "Segmenting your script (local CPU LLM)", "error": ""}})
    await setp(20, "Reading & segmenting your script")
    data, cost = await agents.segment_script(story, channel or {},
                                             int(story.get("target_seconds") or 90))

    chunks = [c for c in (data.get("chunks") or []) if isinstance(c, dict)][:24]
    for c in chunks:
        if isinstance(c.get("cast"), str):
            c["cast"] = [c["cast"]]
        elif not isinstance(c.get("cast"), list):
            c.pop("cast", None)
    characters = [c for c in (data.get("characters") or [])
                  if isinstance(c, dict) and c.get("name") and c.get("description")]
    cs = data.get("character_sheet") if isinstance(data.get("character_sheet"), dict) else {}
    anchor = (cs.get("anchor") or "").strip() or "; ".join(
        f"{c['name']}: {c['description']}" for c in characters)
    script = {
        "chunks": chunks,
        "character_sheet": {"anchor": anchor},
        "segmented_from_script": True,
        "production_notes": "",
    }
    await setp(80, f"Structured into {len(chunks)} scenes")
    title = (data.get("title") or "").strip()
    update = {
        "script": script, "status": "script_ready",
        "stage": f"Segmented into {len(chunks)} scenes — ready to render",
        "characters": characters,
        "character_sheet": {"anchor": anchor, "text": anchor, "locked": bool(anchor),
                            "visuals_stale": False, "version": 1},
        "updated_at": utcnow(),
    }
    if title and not (story.get("title_english") or "").strip():
        update["title_english"] = title[:100]
        update["title_hindi"] = title
    await db.stories.update_one({"_id": story_id}, {"$set": update, "$inc": {
        "cost.llm": round(cost, 5), "cost.total": round(cost, 5)}})


async def run_produce_job(job, setp):
    from pipeline import produce_video
    await produce_video(job["ref_id"], setp, job_id=job["_id"])


async def run_segment_fix_job(job, setp):
    from pipeline import regenerate_segment
    payload = job.get("payload") or {}
    await regenerate_segment(job["ref_id"], int(payload.get("index", 0)), setp,
                             kind=str(payload.get("kind") or "all"), notes=str(payload.get("notes") or ""))


async def run_edit_request_job(job, setp):
    from pipeline import apply_review_edits
    notes = str((job.get("payload") or {}).get("notes") or "").strip()
    await apply_review_edits(job["ref_id"], notes, setp)


async def run_improve_job(job, setp):
    from pipeline import improve_video
    await improve_video(job["ref_id"], setp, job_id=job["_id"])


async def run_publish_job(job, setp):
    import asyncio as _aio
    from db import db
    from models import utcnow
    from services import social

    story_id = job["ref_id"]
    platform = (job.get("payload") or {}).get("platform", "youtube")
    story = await db.stories.find_one({"_id": story_id})
    if not story:
        raise RuntimeError("story not found")
    if story["status"] not in ("approved", "published"):
        raise RuntimeError("story must be approved by human review before publishing")
    final = story.get("media", {}).get("final")
    if not final:
        raise RuntimeError("no final video rendered")
    from services.ocr import MEDIA_ROOT
    video_path = MEDIA_ROOT / "final" / f"{story_id}.mp4"
    if not video_path.exists():
        raise RuntimeError("final video file missing on disk")

    meta = story.get("metadata", {})
    title = meta.get("title") or story.get("title_english") or story.get("title_hindi", "Story")
    desc = meta.get("description", "")
    # YouTube requires disclosure for meaningful AI-generated content
    desc = desc + "\n\nThis video contains AI-generated visuals and voice-over."
    tags = meta.get("hashtags", [])
    channel = await db.channels.find_one({"_id": story["channel_id"]})
    base = os.environ.get("PUBLIC_BASE_URL") or ""

    await setp(30, f"Uploading to {platform}")
    if platform == "youtube":
        if not social.cred_status()["youtube"]:
            raise RuntimeError("YouTube credentials missing in backend/.env (YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN)")
        thumb = MEDIA_ROOT / "thumbs" / f"{story_id}.jpg"
        result = await _aio.to_thread(
            social.yt_upload, video_path, title, desc, tags,
            bool((channel or {}).get("is_kids")), thumb)
    elif platform == "instagram":
        if not social.cred_status()["instagram"]:
            raise RuntimeError("Instagram not connected — add a long-lived access token + user ID in Settings → Integrations")
        if not base:
            raise RuntimeError("PUBLIC_BASE_URL not set — Instagram needs a public video URL")
        video_url = f"{base.rstrip('/')}{final}"
        thumb_url = f"{base.rstrip('/')}/api/media/thumbs/{story_id}.jpg"
        caption = f"{title}\n\n{desc}\n\n" + " ".join(f"#{t}" for t in tags[:12])
        result = await _aio.to_thread(social.ig_publish, video_url, caption, thumb_url)
    else:
        raise RuntimeError(f"unknown platform {platform}")

    await db.stories.update_one({"_id": story_id}, {"$set": {
        f"publish.{platform}": {**result, "published_at": utcnow().isoformat()},
        "status": "published", "stage": f"Published to {platform}", "updated_at": utcnow()}})
    await setp(95, f"Published to {platform}")


async def run_engagement_job(job, setp):
    from services import engagement
    summary = await engagement.sync_once()
    await setp(90, f"engagement: {summary.get('fetched', 0)} fetched, "
                   f"{summary.get('drafts', 0)} drafts awaiting approval")


async def run_news_job(job, setp):
    import time as _time
    from db import db
    from models import Story, utcnow
    from services import news
    from job_queue import enqueue

    channel = await db.channels.find_one({"key": "public_interest"})
    if not channel:
        raise RuntimeError("public_interest channel missing")
    items = await news.fetch_items()
    await setp(20, f"{len(items)} items fetched")
    today = utcnow().strftime("%Y-%m-%d")
    deadline = _time.time() + 6 * 60  # stay within quota-friendly window
    created, deferred = 0, 0
    budget = items[:14]
    for i, item in enumerate(budget):
        if _time.time() > deadline:
            deferred = len(budget) - i
            await setp(60, f"quota window reached — {deferred} items deferred to next run")
            break
        try:
            verdict = await news.triage_item(item, channel)
        except Exception as e:
            deferred += 1
            print(f"[news] triage deferred: {str(e)[:100]}")
            continue
        approved = bool(verdict.get("approved"))
        existing = await db.stories.count_documents({"source_url": item["link"]})
        if existing:
            continue
        story = Story(
            book_id=f"news-{today}", channel_id=channel["_id"],
            title_hindi=verdict.get("story_title", ""),
            title_english=item["title"][:150],
            source=f"{item['source']} · {item['published']}",
            page_start=0, page_end=0,
            category=verdict.get("category", "News") if approved else "Rejected",
            setting="", conflict="", twist="", climax="", resolution="",
            moral="", emotional_tone="Informative", target_audience="General",
            visual_style="clean broadcast news style",
            source_url=item["link"],
            policy={"approved": approved, "reason": verdict.get("reason", ""),
                    "summary": verdict.get("summary", ""), "query": item["query"]},
            status="draft",
        )
        await db.stories.insert_one(story.to_mongo())
        created += 1
        await setp(20 + int((i + 1) / max(len(budget), 1) * 70),
                   f"triaged {i + 1}/{len(budget)} ({created} approved)")
    await setp(95, f"{created} approved, {deferred} deferred (quota-safe)")


def register_all():
    from job_queue import register
    register("ocr", run_ocr_job)
    register("segment", run_segment_job)
    register("script", run_script_job)
    register("script_segment", run_script_segment_job)
    register("produce", run_produce_job)
    register("segment_fix", run_segment_fix_job)
    register("edit_request", run_edit_request_job)
    register("publish", run_publish_job)
    register("improve", run_improve_job)
    register("engagement", run_engagement_job)
    register("news", run_news_job)
