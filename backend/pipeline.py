import asyncio
import json
from pathlib import Path

from db import db
from models import utcnow
from services import agents, media
from services.ocr import MEDIA_ROOT


def _add_cost(cost: dict, kind: str, amount: float):
    cost[kind] = round(cost.get(kind, 0.0) + amount, 5)
    cost["total"] = round(cost.get("llm", 0.0) + cost.get("tts", 0.0) + cost.get("image", 0.0), 5)


async def _save_cost(story_id: str, kind: str, amount: float):
    await db.stories.update_one(
        {"_id": story_id},
        {"$inc": {f"cost.{kind}": round(amount, 5), "cost.total": round(amount, 5)},
         "$set": {"updated_at": utcnow()}},
    )


async def set_story_async(story_id, **kw):
    kw["updated_at"] = utcnow()
    await db.stories.update_one({"_id": story_id}, {"$set": kw})


async def _load_story(story_id):
    story = await db.stories.find_one({"_id": story_id})
    if not story:
        raise RuntimeError("story not found")
    return story


async def _load_channel(story):
    channel = await db.channels.find_one({"_id": story["channel_id"]})
    if not channel:
        from bson import ObjectId
        try:
            channel = await db.channels.find_one({"_id": ObjectId(story["channel_id"])})
        except Exception:
            channel = None
    if not channel:
        raise RuntimeError(f"channel {story['channel_id']} not found")
    return channel


# ================= AI-story pipeline =================

async def _source_text(story):
    book = await db.books.find_one({"_id": story["book_id"]}) if story.get("book_id") else None
    if story.get("policy", {}).get("summary"):
        return story["policy"]["summary"]
    if book and book.get("pages"):
        return "\n".join(p["text"] for p in book["pages"] if p.get("text"))
    return story.get("conflict", "") + " " + story.get("climax", "") + " " + story.get("moral", "")


async def _editor_pass(story_id, story, channel, set_story):
    """Editor & fact-check agent: verify the script against the source, apply corrections."""
    editor = {}
    try:
        editor, edit_cost = await agents.editor_pass(story, channel, await _source_text(story))
        await _save_cost(story_id, "llm", edit_cost)
        chunks = story["script"]["chunks"]
        applied = 0
        changed = set()
        for c in (editor.get("corrections") or []):
            if not isinstance(c, dict):
                continue
            idx = int(c.get("index", -1))
            if 0 <= idx < len(chunks):
                touched = False
                for k in ("voiceover", "visual", "video_prompt"):
                    # diff-check: only invalidate a segment when its text truly changes,
                    # so re-running produce reuses every already-rendered asset.
                    if c.get(k) and chunks[idx].get(k) != c[k]:
                        chunks[idx][k] = c[k]
                        changed.add(idx)
                        touched = True
                if touched:
                    applied += 1
        hook_line = editor.get("hook_line") or ""
        if hook_line and chunks and chunks[0].get("voiceover") != hook_line:
            chunks[0]["voiceover"] = hook_line
            changed.add(0)
        if changed:
            _invalidate_caches(story_id, changed)  # only the edited segments re-render
            story["script"]["chunks"] = chunks
        story["script"]["editor"] = {
            "factual_ok": editor.get("factual_ok"),
            "silence_before_index": editor.get("silence_before_index", -1),
            "corrections_applied": applied + (1 if hook_line else 0),
            "notes": editor.get("editor_notes", []),
        }
        story["script"]["viral_score"] = editor.get("viral_score", {})
        await set_story(script=story["script"])
        return story["script"]["chunks"], editor
    except Exception as e:
        print(f"[pipeline] editor pass unavailable: {str(e)[:120]}", flush=True)
        return story["script"].get("chunks") or [], editor


async def _character_sheet(story_id, story, mode, anchor, style):
    from services import router

    char_path = MEDIA_ROOT / "char" / f"{story_id}.png"
    if not char_path.exists():
        result = await router.image(
            f"STRICT VISUAL CONTINUITY REFERENCE. {anchor} "
            f"Render a clean full-body character lineup and small environment swatches in exactly this art direction: {style}. "
            "Show every named recurring character once, clearly separated, with stable face, hair, clothing colors, accessories, build and aura. "
            "Indian miniature-painting linework and materials where requested. Neutral backdrop, delicate ornamental border, no labels, no text, no watermark.",
            char_path, session=f"char-{story_id}", quality_required=True)
        if result.get("provider") not in ("procedural", "pexels"):
            await _save_cost(story_id, "image", media.IMAGE_PRICE)
    return char_path


async def _storyboard_poster(story_id, chunks, style, anchor, char_path, channel, set_story, setp, job_id):
    from job_queue import is_cancelled, JobCancelled
    from services import storyboard

    if job_id and is_cancelled(job_id):
        raise JobCancelled()
    await set_story_async(story_id, stage="Storyboard poster")
    await setp(8, "One-shot storyboard: all slides in a single image")
    slides = await storyboard.generate_storyboard(
        chunks, MEDIA_ROOT / "frames" / story_id, style, anchor, char_path, channel)
    await setp(30, f"Poster sliced into {len(slides)} slides")
    return slides


async def _render_segment(ctx, i, chunk, audio_urls, frame_urls, sem, setp, job_id):
    from job_queue import is_cancelled, JobCancelled
    from services import router

    aid = ctx["story_id"]
    channel = ctx["channel"]
    chunks = ctx["chunks"]
    async with sem:
        if job_id and is_cancelled(job_id):
            raise JobCancelled()
        audio_dir = MEDIA_ROOT / "audio" / aid
        aud = audio_dir / f"{i:02d}.mp3"
        if aud.exists():
            adur = media.ffprobe_duration(aud)
        else:
            adur = await media.synthesize_voice(
                chunk.get("voiceover", ""), channel.get("voice", ""),
                channel.get("voice_speed", 1.0), aud, lang_hint=channel.get("language", "hi"),
                direction=f"{chunk.get('emotion') or 'storytelling'} emotion, "
                          f"{chunk.get('pace') or 'medium'} pace",
                expressive=channel.get("expressive_voice", True))
            await _save_cost(aid, "tts", media.tts_cost(chunk.get("voiceover", "")))
            budget = ctx.get("seg_budget", 0.0)
            if budget:  # gently speed narration so the video hits its target length
                adur = await media.fit_audio_to_budget(aud, budget)
        dur = max(6.0, min(16.0, adur + 0.6))

        frame = MEDIA_ROOT / "frames" / aid / f"{i:02d}.png"
        if not frame.exists():
            res = await router.image(
                f"STRICT CONTINUITY LOCK — copy every visible recurring character from the attached reference image and this bible exactly: {ctx['anchor']} "
                f"ART DIRECTION LOCK: {ctx['style']}. "
                f"SCENE FOR THIS SEGMENT: {chunk.get('video_prompt', chunk.get('visual', ''))}. "
                "Vertical 9:16 composition, cohesive Indian miniature-painting treatment where specified, delicate linework, rich pigment, gold-leaf accents, "
                "natural anatomy and hands, expressive faces, layered depth, cinematic lighting, highly detailed, no text, no watermark. "
                "Do not redesign faces, clothing, palette, architecture, or illustration medium.",
                frame, ref_image=ctx["char_path"], session=f"frame-{aid}-{i}",
                query_hint=chunk.get("video_prompt") or chunk.get("visual") or "",
                require_reference=True, quality_required=True, segment_index=i)
            if res.get("provider") not in ("procedural",):
                await _save_cost(aid, "image", media.IMAGE_PRICE)

        cap = MEDIA_ROOT / "tmp" / f"{aid}-{i:02d}.png"
        await asyncio.to_thread(media.render_caption, chunk.get("voiceover", ""), cap)
        clip = MEDIA_ROOT / "clips" / aid / f"{i:02d}.mp4"
        if clip.exists():
            audio_urls.append((i, f"/api/media/audio/{aid}/{i:02d}.mp3"))
            frame_urls.append((i, f"/api/media/frames/{aid}/{i:02d}.png"))
            await setp(8 + int((i + 1) / len(chunks) * 50), f"Segments {i + 1}/{len(chunks)} (cached)")
            return
        silence_pad = 0.0
        if ctx["editor"].get("silence_before_index") == i:
            silence_pad = 0.6
        rate_mult = {"climax": 1.7, "action": 1.4, "twist": 1.2, "hook": 1.15}.get(chunk.get("beat"), 1.0)
        vid_res = {"provider": "kenburns"}
        if ctx["mode"] == "clip":
            vid_res = await router.video(
                f"STRICT CONTINUITY LOCK: {ctx['anchor']} ART DIRECTION: {ctx['style']}. "
                f"SCENE: {chunk.get('video_prompt', chunk.get('visual', ''))}. "
                "Keep identical faces, hair, clothing, accessories, body proportions, palette, linework, lighting language and recurring locations.",
                frame, MEDIA_ROOT / "tmp" / f"{aid}-raw-{i:02d}.mp4", dur + silence_pad,
                preserve_reference=True, segment_index=i)
        hook_card = None
        if i == 0:
            hook_card = await asyncio.to_thread(
                media.render_hook_card, chunks[0].get("voiceover", ""),
                MEDIA_ROOT / "tmp" / f"{aid}-hook.png")
        if vid_res.get("provider") == "kenburns":
            await media.make_segment_clip(frame, aud, cap, clip, chunk.get("camera", "zoom_in"),
                                          dur + silence_pad, rate_mult=rate_mult,
                                          hook_card=hook_card, silence_pad=silence_pad)
        else:
            await media.compose_ai_clip(MEDIA_ROOT / "tmp" / f"{aid}-raw-{i:02d}.mp4",
                                        aud, cap, clip, dur + silence_pad,
                                        hook_card=hook_card)
            await _save_cost(aid, "video", round(0.05 * dur, 3))
        audio_urls.append((i, f"/api/media/audio/{aid}/{i:02d}.mp3"))
        frame_urls.append((i, f"/api/media/frames/{aid}/{i:02d}.png"))
        await setp(8 + int((i + 1) / len(chunks) * 50), f"Segments {i + 1}/{len(chunks)}")


async def _render_all_segments(ctx, setp, job_id):
    from job_queue import is_cancelled, JobCancelled

    audio_urls, frame_urls = [], []
    sem = asyncio.Semaphore(2)
    await asyncio.gather(*[
        _render_segment(ctx, i, c, audio_urls, frame_urls, sem, setp, job_id)
        for i, c in enumerate(ctx["chunks"])])
    if job_id and is_cancelled(job_id):
        raise JobCancelled()
    return audio_urls, frame_urls


async def _assemble_video(ctx, setp):
    """Concat segments → mix music + ambience → append end card. Returns (main, mixed, final)."""
    aid = ctx["story_id"]
    channel = ctx["channel"]
    story = ctx["story"]

    await setp(62, "Stitching")
    await set_story_async(aid, stage="Stitching")
    clips = sorted((MEDIA_ROOT / "clips" / aid).glob("*.mp4"))
    main = MEDIA_ROOT / "final" / f"{aid}_main.mp4"
    await media.concat_clips(clips, main)
    total = media.ffprobe_duration(main)

    mood = (story["script"].get("chunks", [{}])[0].get("music_mood")
            or channel.get("music_mood") or "devotional")
    music = MEDIA_ROOT / "music" / f"{mood}.wav"
    if not music.exists():
        await asyncio.to_thread(media.synth_music, mood, music)
    ambience = MEDIA_ROOT / "music" / f"{mood}_amb.wav"
    if not ambience.exists():
        await asyncio.to_thread(media.synth_ambience, mood, ambience)
    mixed = MEDIA_ROOT / "final" / f"{aid}_mix.mp4"
    await media.mix_music(main, music, mixed, total, channel.get("music_volume", 0.16),
                          ambience=ambience)

    end_png = MEDIA_ROOT / "tmp" / f"{aid}-end.png"
    await asyncio.to_thread(media.render_endcard, channel.get("name", "StoryForge"),
                            channel.get("cta_text", "Subscribe for more stories"), end_png)
    end_clip = MEDIA_ROOT / "tmp" / f"{aid}-end.mp4"
    await media.make_endcard_clip(end_png, end_clip)
    final = MEDIA_ROOT / "final" / f"{aid}.mp4"
    await media.concat_clips([mixed, end_clip], final)
    return main, mixed, final


async def _qa_pass(story, channel):
    try:
        qa_, c_ = await agents.run_qa(story, channel)
        await _save_cost(story["_id"], "llm", c_)
        return qa_
    except Exception as e:
        print(f"[pipeline] QA unavailable: {str(e)[:120]}", flush=True)
        return {"passed": None, "score": None, "checks": [], "issues": [],
                "note": "QA agent temporarily unavailable (API quota) — rerun after quota resets"}


async def _metadata_pass(story, channel):
    try:
        m_, c_ = await agents.make_metadata(story, channel)
        await _save_cost(story["_id"], "llm", c_)
        return m_
    except Exception as e:
        print(f"[pipeline] metadata unavailable: {str(e)[:120]}", flush=True)
        return {"title": story.get("title_english") or story.get("title_hindi", ""),
                "description": story.get("moral", ""),
                "hashtags": ["stories", "india", "mythology", "shorts"],
                "thumbnail_text": story.get("title_english", "")[:40],
                "note": "auto-metadata unavailable (API quota) — rerun later"}


async def _thumbnail(story, channel, ctx, meta, char_path):
    from services import router

    aid = ctx["story_id"]
    main_char = (story.get("characters") or [{}])[0]
    # reuse the hook slide as thumbnail art — zero extra image-API calls (this was the slow tail)
    thumb_art = MEDIA_ROOT / "frames" / aid / "00.png"
    if not thumb_art.exists():
        thumb_art = MEDIA_ROOT / "tmp" / f"{aid}-thumb-raw.png"
        result = await router.image(
            f"Dramatic viral YouTube Shorts thumbnail artwork, vertical 9:16, extreme emotional close-up, "
            f"high contrast rim lighting, {ctx['style']}. Main character: {main_char.get('description', ctx['anchor'][:300])}. "
            f"Full continuity lock: {ctx['anchor']}. Scene: {story.get('climax', '')}. No text, no watermark.",
            thumb_art, ref_image=char_path, session=f"thumb-{aid}",
            require_reference=True, quality_required=True)
        if result.get("provider") not in ("procedural", "pexels"):
            await _save_cost(aid, "image", media.IMAGE_PRICE)
    thumb_path = MEDIA_ROOT / "thumbs" / f"{aid}.jpg"
    await asyncio.to_thread(
        media.overlay_title_on_image, thumb_art,
        meta.get("thumbnail_text") or story.get("title_english") or story.get("title_hindi", ""),
        channel.get("name", ""), thumb_path)


async def produce_video(story_id: str, setp, job_id=""):
    import shutil
    from job_queue import is_cancelled, JobCancelled
    from services.ocr import disk_guard

    disk_guard()
    story = await _load_story(story_id)
    channel = await _load_channel(story)
    mode = story.get("mode") or (channel.get("mode") or "slide")
    if mode == "stitch":
        return await produce_stitch(story_id, setp, job_id)

    async def set_story(**kw):
        await set_story_async(story_id, **kw)

    chunks = (story.get("script") or {}).get("chunks") or []
    if not chunks:
        raise RuntimeError("story has no script — generate the script first")
    chunks = chunks[:24]

    consistency_sheet = story.get("character_sheet") or {}
    if consistency_sheet.get("visuals_stale"):
        (MEDIA_ROOT / "char" / f"{story_id}.png").unlink(missing_ok=True)
        for sub in ("frames", "clips"):
            shutil.rmtree(MEDIA_ROOT / sub / story_id, ignore_errors=True)

    await set_story(status="rendering", stage="Fact check & edit")
    if (story.get("script") or {}).get("imported_verbatim"):
        editor = (story.get("script") or {}).get("editor") or {}
        await setp(2, "Using supplied dialogue and visuals verbatim")
        await set_story(stage="Supplied script locked")
    else:
        await setp(2, "Fact check & editor pass")
        chunks, editor = await _editor_pass(story_id, story, channel, set_story)
    chunks = chunks[:24]

    await set_story(status="rendering", stage="Character sheet", error="")
    await setp(5, "Character sheet")
    cs = story.get("character_sheet") or {}
    anchor = cs.get("anchor") or "; ".join(
        f"{c.get('name')}: {c.get('description')}" for c in story.get("characters", []))
    style = channel.get("style_prefix") or story.get("visual_style") or "Indian miniature painting style"
    char_path = await _character_sheet(story_id, story, mode, anchor, style)

    ctx = {"story_id": story_id, "story": story, "channel": channel, "chunks": chunks,
           "mode": mode, "style": style, "anchor": anchor, "char_path": char_path,
           "editor": editor,
           "seg_budget": max(4.0, (max(30, min(240, int(story.get("target_seconds") or 90))) - 3.2)
                             / max(len(chunks), 1))}
    if job_id and is_cancelled(job_id):
        raise JobCancelled()
    if mode == "storyboard":
        ctx["slides"] = await _storyboard_poster(story_id, chunks, style, anchor, char_path, channel, set_story, setp, job_id)

    audio_urls, frame_urls = await _render_all_segments(ctx, setp, job_id)
    main, mixed, final = await _assemble_video(ctx, setp)

    await setp(72, "QA & metadata")
    await set_story(stage="QA & metadata")
    qa, meta = await asyncio.gather(_qa_pass(story, channel), _metadata_pass(story, channel))

    await setp(86, "Thumbnail")
    await set_story(stage="Thumbnail")
    await _thumbnail(story, channel, ctx, meta if isinstance(meta, dict) else {}, char_path)

    audio_urls.sort(key=lambda t: t[0])
    frame_urls.sort(key=lambda t: t[0])
    for inter in (main, mixed):  # intermediates — final + segments can rebuild them
        Path(inter).unlink(missing_ok=True)
    await set_story(
        status="in_review", stage="Awaiting review",
        character_sheet={**(story.get("character_sheet") or {}), "visuals_stale": False},
        qa=qa if isinstance(qa, dict) else {},
        metadata=meta if isinstance(meta, dict) else {},
        media={
            "final": f"/api/media/final/{story_id}.mp4",
            "duration_sec": round(media.ffprobe_duration(final), 1),
            "char_sheet": f"/api/media/char/{story_id}.png",
            "audio": [u for _, u in audio_urls],
            "frames": [u for _, u in frame_urls],
            "thumbnail": f"/api/media/thumbs/{story_id}.jpg",
        },
    )


async def regenerate_segment(story_id: str, index: int, setp, kind: str = "all", notes: str = ""):
    from services import router
    from services.generation import generation_context
    story = await _load_story(story_id)
    channel = await _load_channel(story)
    chunks = (story.get("script") or {}).get("chunks") or []
    if index < 0 or index >= len(chunks):
        raise RuntimeError("invalid segment index")
    if kind not in {"script", "voice", "visual", "all"}:
        raise RuntimeError("invalid regeneration kind")
    chunk = chunks[index]
    aid = story_id
    had_video = bool((story.get("media") or {}).get("final"))

    if kind == "script":
        await set_story_async(story_id, stage=f"Regenerating script · segment {index + 1}", status="rendering")
        await setp(20, f"Rewriting segment {index + 1}")
        rewritten, cost = await agents.regenerate_chunk(story, channel, index)
        for key in ("voiceover", "visual", "video_prompt"):
            chunk[key] = str(rewritten.get(key) or chunk.get(key) or "")[:2500]
        _invalidate_caches(story_id, {index})
        story["script"]["chunks"] = chunks
        await _save_cost(story_id, "llm", cost)
        await set_story_async(
            story_id, script=story["script"],
            status="in_review" if had_video else "script_ready",
            stage=f"Script updated · segment {index + 1} ready to render")
        return

    await set_story_async(story_id, stage=f"Regenerating segment {index + 1}", status="rendering")
    await setp(10, f"Segment {index + 1}")

    audio_dir = MEDIA_ROOT / "audio" / aid
    aud = audio_dir / f"{index:02d}.mp3"
    if kind in {"all", "voice"} or not aud.exists():
        adur = await media.synthesize_voice(chunk.get("voiceover", ""), channel.get("voice", ""),
                                            channel.get("voice_speed", 1.0), aud,
                                            lang_hint=channel.get("language", "hi"),
                                            direction=f"{chunk.get('emotion') or 'storytelling'} emotion, "
                                                      f"{chunk.get('pace') or 'medium'} pace",
                                            expressive=channel.get("expressive_voice", True))
        await _save_cost(story_id, "tts", media.tts_cost(chunk.get("voiceover", "")))
    else:
        adur = media.ffprobe_duration(aud)
    dur = max(6.0, min(16.0, adur + 0.6))

    frame = MEDIA_ROOT / "frames" / aid / f"{index:02d}.png"
    char_path = MEDIA_ROOT / "char" / f"{aid}.png"
    if kind in {"all", "visual"} or not frame.exists():
        if (story.get("character_sheet") or {}).get("visuals_stale") or not char_path.exists():
            if (story.get("character_sheet") or {}).get("visuals_stale"):
                char_path.unlink(missing_ok=True)
            await _character_sheet(
                story_id, story, story.get("mode") or "slide",
                (story.get("character_sheet") or {}).get("anchor", ""),
                channel.get("style_prefix") or story.get("visual_style") or "Indian miniature painting style")
        visual_prompt = (
            f"STRICT CONTINUITY LOCK — copy every visible character from the reference and this bible exactly: {(story.get('character_sheet') or {}).get('anchor', '')}. "
            f"ART DIRECTION LOCK: {channel.get('style_prefix') or story.get('visual_style', '')}. "
            f"{chunk.get('video_prompt', chunk.get('visual', ''))}. "
            "Clear subject and readable facial expressions, natural anatomy and hands, strong foreground/midground/background separation, "
            "coherent lighting, rich material detail, cinematic composition, no text, no watermark. Do not redesign identity, clothing, palette, or medium."
        )
        gen_ctx = generation_context.get()
        if gen_ctx.get("job_id"):  # tells the Studio worker to make a new revision (with these comments)
            gen_ctx["regenerate"], gen_ctx["feedback"] = index, notes
        try:
            res = await router.image(visual_prompt, frame, ref_image=char_path if char_path.exists() else None,
                                     session=f"frame-{aid}-{index}-r", query_hint=chunk.get("visual"),
                                     require_reference=True, quality_required=True, segment_index=index)
        finally:
            gen_ctx.pop("regenerate", None)
            gen_ctx.pop("feedback", None)
        if res.get("provider") not in ("procedural",):
            await _save_cost(story_id, "image", media.IMAGE_PRICE)

    cap = MEDIA_ROOT / "tmp" / f"{aid}-{index:02d}.png"
    await asyncio.to_thread(media.render_caption, chunk.get("voiceover", ""), cap)
    clip = MEDIA_ROOT / "clips" / aid / f"{index:02d}.mp4"
    if kind == "voice" and clip.exists() and (story.get("mode") or "slide") == "clip":
        tmp_clip = clip.with_suffix(".voice.mp4")
        await media.run_ffmpeg("ffmpeg", "-y", "-i", str(clip), "-i", str(aud),
                               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                               "-b:a", "192k", "-shortest", str(tmp_clip))
        tmp_clip.replace(clip)
    elif (story.get("mode") or "slide") == "clip" and kind in {"visual", "all"}:
        raw = MEDIA_ROOT / "tmp" / f"{aid}-raw-{index:02d}-r.mp4"
        video_res = await router.video(
            f"STRICT CONTINUITY LOCK: {(story.get('character_sheet') or {}).get('anchor', '')}. "
            f"ART DIRECTION: {channel.get('style_prefix') or story.get('visual_style', '')}. "
            f"SCENE: {chunk.get('video_prompt', chunk.get('visual', ''))}. Match the reference image exactly.",
            frame, raw, dur, preserve_reference=True, segment_index=index)
        if video_res.get("provider") == "kenburns":
            await media.make_segment_clip(frame, aud, cap, clip, chunk.get("camera", "zoom_in"), dur)
        else:
            await media.compose_ai_clip(raw, aud, cap, clip, dur)
    else:
        await media.make_segment_clip(frame, aud, cap, clip, chunk.get("camera", "zoom_in"), dur)

    await setp(70, "Re-stitching")
    final = await _restitch(story, channel, aid, chunks)

    await set_story_async(
        story_id, status="in_review", stage="Awaiting review",
        media={**(story.get("media") or {}),
               "final": f"/api/media/final/{aid}.mp4",
               "duration_sec": round(media.ffprobe_duration(final), 1),
               "audio": [f"/api/media/audio/{aid}/{i:02d}.mp3" for i in range(len(chunks))],
               "frames": [f"/api/media/frames/{aid}/{i:02d}.png" for i in range(len(chunks))]})


async def apply_review_edits(story_id: str, notes: str, setp):
    story = await _load_story(story_id)
    channel = await _load_channel(story)
    chunks = (story.get("script") or {}).get("chunks") or []
    if not chunks:
        raise RuntimeError("story has no script to edit")
    await set_story_async(story_id, stage="Applying requested edits", status="rendering", error="")
    await setp(15, "Reading review notes")
    suggestion, cost = await agents.apply_review_edits(story, channel, notes)
    await _save_cost(story_id, "llm", cost)
    changed = set()
    for edit in suggestion.get("edits") or []:
        try:
            idx = int(edit.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(chunks):
            for key in ("voiceover", "visual", "video_prompt", "camera", "emotion"):
                if edit.get(key) is not None:
                    chunks[idx][key] = str(edit[key])[:2500]
                    changed.add(idx)
    _invalidate_caches(story_id, changed)
    script = story.get("script") or {}
    script["chunks"] = chunks
    await setp(80, f"Updated {len(changed)} segment(s)")
    await db.stories.update_one({"_id": story_id}, {"$set": {
        "script": script,
        "status": "edits_requested",
        "stage": suggestion.get("summary") or "Edits applied — ready to re-render",
        "review_notes": notes,
        "updated_at": utcnow(),
    }, "$push": {"edit_requests": {
        "notes": notes, "summary": suggestion.get("summary", ""),
        "segments": sorted(changed), "at": utcnow(),
    }}})


async def _restitch(story, channel, aid, chunks):
    """Re-concat all segment clips → music mix → end card. Returns the final path."""
    clips = sorted((MEDIA_ROOT / "clips" / aid).glob("*.mp4"))
    main = MEDIA_ROOT / "final" / f"{aid}_main.mp4"
    await media.concat_clips(clips, main)
    total = media.ffprobe_duration(main)
    mood = chunks[0].get("music_mood") or channel.get("music_mood") or "devotional"
    music = MEDIA_ROOT / "music" / f"{mood}.wav"
    if not music.exists():
        await asyncio.to_thread(media.synth_music, mood, music)
    mixed = MEDIA_ROOT / "final" / f"{aid}_mix.mp4"
    await media.mix_music(main, music, mixed, total, channel.get("music_volume", 0.16))
    end_clip = MEDIA_ROOT / "tmp" / f"{aid}-end.mp4"
    if not end_clip.exists():
        end_png = MEDIA_ROOT / "tmp" / f"{aid}-end.png"
        await asyncio.to_thread(media.render_endcard, channel.get("name", "StoryForge"),
                                channel.get("cta_text", "Subscribe"), end_png)
        await media.make_endcard_clip(end_png, end_clip)
    final = MEDIA_ROOT / "final" / f"{aid}.mp4"
    await media.concat_clips([mixed, end_clip], final)
    for inter in (main, mixed):  # intermediates are rebuildable
        Path(inter).unlink(missing_ok=True)
    return final


# ================= improvement coach =================

def _base_score(story):
    total = ((story.get("script") or {}).get("viral_score") or {}).get("total")
    if total is not None:
        return total
    q = story.get("qa") or {}
    return round(q["score"] * 10) if q.get("score") is not None else 0


def _apply_improvements(sug, chunks):
    """Apply the coach's targeted edits to the chunks; returns the set of changed indexes."""
    changed = set()
    for c in (sug.get("edits") or []):
        try:
            idx = int(c.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(chunks) and isinstance(c, dict):
            for k in ("voiceover", "visual", "video_prompt"):
                if c.get(k):
                    chunks[idx][k] = c[k]
                    changed.add(idx)
    hook_line = sug.get("hook_line") or ""
    if hook_line and chunks:
        chunks[0]["voiceover"] = hook_line
        changed.add(0)
    return changed


def _invalidate_caches(story_id, changed):
    for i in changed:  # only edited segments re-render
        for sub, ext in (("audio", "mp3"), ("frames", "png")):
            (MEDIA_ROOT / sub / story_id / f"{i:02d}.{ext}").unlink(missing_ok=True)
        (MEDIA_ROOT / "clips" / story_id / f"{i:02d}.mp4").unlink(missing_ok=True)


async def improve_video(story_id: str, setp, job_id=""):
    """Improvement coach: after assessment, pinpoint the weakest scope, apply targeted edits
    (max 2 rounds) and keep them ONLY if the overall viral score improves — auto-revert otherwise."""
    import json as _json

    story = await _load_story(story_id)
    attempts = story.get("improvements") or []
    if len(attempts) >= 2:
        raise RuntimeError("improvement budget exhausted (2 rounds max)")
    script = story.get("script") or {}
    chunks = script.get("chunks") or []
    if not chunks:
        raise RuntimeError("story has no script")
    base = _base_score(story)
    prev_script = _json.loads(_json.dumps(script))

    channel = await db.channels.find_one({"_id": story["channel_id"]})
    if not channel:
        from bson import ObjectId
        channel = await db.channels.find_one({"_id": ObjectId(story["channel_id"])})

    await set_story_async(story_id, status="rendering", stage="Improvement coach", error="")
    await setp(5, "Pinpointing weakest scope")
    sug, cost = await agents.improvement_pass(story, channel or {}, attempts)
    await _save_cost(story_id, "llm", cost)

    changed = _apply_improvements(sug, chunks)
    _invalidate_caches(story_id, changed)

    story["script"]["chunks"] = chunks
    await db.stories.update_one({"_id": story_id}, {"$set": {
        "script": story["script"], "status": "rendering",
        "stage": f"Improvement round {len(attempts) + 1}: re-render"}})

    await setp(15, "Re-rendering with targeted edits")
    await produce_video(story_id, setp, job_id=job_id)

    fresh = await db.stories.find_one({"_id": story_id})
    new_score = ((fresh.get("script") or {}).get("viral_score") or {}).get("total")
    accepted = new_score is not None and new_score > base
    if not accepted:
        await set_story_async(story_id, script=prev_script, stage="Reverting — no score gain")
        await setp(55, "Reverting to previous script")
        await produce_video(story_id, setp, job_id=job_id)

    iteration = {
        "round": len(attempts) + 1, "scope": sug.get("scope", ""), "why": sug.get("why", ""),
        "base_score": base, "new_score": new_score, "segments_edited": sorted(changed),
        "accepted": accepted, "attempted": True, "at": utcnow().isoformat(),
    }
    await db.stories.update_one(
        {"_id": story_id},
        {"$push": {"improvements": iteration}, "$set": {"updated_at": utcnow()}})
    print(f"[improve] round {iteration['round']}: {base} -> {new_score} "
          f"({'kept' if accepted else 'reverted'}) scope={str(iteration['scope'])[:80]}", flush=True)


# ================= stitch-my-own-media pipeline =================

def _beat_snap(duration: float, mood: str) -> float:
    """Snap a silent slide's duration to a whole number of music beats so cuts land on rhythm."""
    beat = 60.0 / media.MOODS.get(mood, media.MOODS["devotional"])["bpm"]
    return max(beat, round(duration / beat) * beat)


async def _stitch_item(ctx, i, ch, clips, sem, setp, job_id):
    from job_queue import is_cancelled, JobCancelled

    aid = ctx["story_id"]
    channel = ctx["channel"]
    chunks = ctx["chunks"]
    script = ctx["script"]
    async with sem:
        if job_id and is_cancelled(job_id):
            raise JobCancelled()
        src = Path(ch.get("media_path", ""))
        if not src.exists():
            raise RuntimeError(f"uploaded media missing: {src.name}")
        clip = MEDIA_ROOT / "clips" / aid / f"{i:02d}.mp4"
        vo = (ch.get("voiceover") or "").strip()
        aud = None
        if vo:
            aud = MEDIA_ROOT / "audio" / aid / f"{i:02d}.mp3"
            if not aud.exists():
                await media.synthesize_voice(
                    vo, channel.get("voice", ""), channel.get("voice_speed", 1.0), aud,
                    lang_hint=channel.get("language", "hi"),
                    direction=ch.get("direction") or (channel.get("tone") or "warm storyteller"),
                    expressive=channel.get("expressive_voice", True))
            await _save_cost(aid, "tts", media.tts_cost(vo))
        if ch.get("media_kind") == "video":
            if aud:
                adur = media.ffprobe_duration(aud)
                await media.compose_ai_clip(src, aud, None, clip,
                                            ch.get("duration") or max(3.0, adur + 0.6))
            else:
                await media.stitch_video_clip(
                    src, clip, min(60.0, ch.get("duration") or media.ffprobe_duration(src)))
        else:
            dur = ch.get("duration") or 4.0
            if aud:  # narration always plays in full — extend the slide to fit it
                dur = max(dur, media.ffprobe_duration(aud) + 0.6)
            elif script.get("beat_sync", True):  # silent slides cut on the music's beat
                dur = _beat_snap(dur, channel.get("music_mood") or "devotional")
            cap = None
            if vo:
                cap = MEDIA_ROOT / "tmp" / f"{aid}-s{i:02d}.png"
                await asyncio.to_thread(media.render_caption, vo, cap)
            await media.make_segment_clip(src, aud, cap, clip,
                                          "zoom_in" if i % 2 == 0 else "zoom_out", dur)
        clips.append(clip)
        await setp(10 + int((i + 1) / len(chunks) * 60), f"Item {i + 1}/{len(chunks)}")


async def _stitch_assemble(ctx, clips, setp):
    """Concat → optional music → optional end card → thumbnail. Returns (main, mixed, final)."""
    import shutil

    aid = ctx["story_id"]
    channel = ctx["channel"]
    script = ctx["script"]

    await setp(70, "Stitching")
    await set_story_async(aid, stage="Stitching")
    main = MEDIA_ROOT / "final" / f"{aid}_main.mp4"
    await media.concat_clips(clips, main)
    total = media.ffprobe_duration(main)
    mixed = main
    if script.get("music", True):
        mood = channel.get("music_mood") or "devotional"
        music = MEDIA_ROOT / "music" / f"{mood}.wav"
        if not music.exists():
            await asyncio.to_thread(media.synth_music, mood, music)
        mixed = MEDIA_ROOT / "final" / f"{aid}_mix.mp4"
        await media.mix_music(main, music, mixed, total, min(channel.get("music_volume", 0.12), 0.2))

    final = MEDIA_ROOT / "final" / f"{aid}.mp4"
    if script.get("endcard", True):
        end_clip = MEDIA_ROOT / "tmp" / f"{aid}-end.mp4"
        if not end_clip.exists():
            end_png = MEDIA_ROOT / "tmp" / f"{aid}-end.png"
            await asyncio.to_thread(media.render_endcard, channel.get("name", "StoryForge"),
                                    channel.get("cta_text", "Subscribe for more"), end_png)
            await media.make_endcard_clip(end_png, end_clip)
        await media.concat_clips([mixed, end_clip], final)
    elif mixed != final:
        shutil.copyfile(mixed, final)

    await setp(85, "Thumbnail & metadata")
    thumb = MEDIA_ROOT / "thumbs" / f"{aid}.jpg"
    await media.run_ffmpeg("ffmpeg", "-y", "-ss", "0.5", "-i", str(final),
                           "-frames:v", "1", "-q:v", "3", str(thumb))
    return main, mixed, final


async def _stitch_metadata(story, channel):
    try:
        meta_, c_ = await agents.make_metadata(story, channel)
        await _save_cost(story["_id"], "llm", c_)
        return meta_ if isinstance(meta_, dict) else {}
    except Exception as e:
        print(f"[pipeline] stitch metadata unavailable: {str(e)[:120]}", flush=True)
        return {"title": story.get("title_english") or story.get("title_hindi", "My video"),
                "description": story.get("moral", ""),
                "hashtags": ["shorts", "story"], "note": "fallback metadata (LLM quota)"}


async def produce_stitch(story_id: str, setp, job_id=""):
    """Stitch user-uploaded images/video clips into one vertical video (optional per-item narration)."""
    from job_queue import is_cancelled, JobCancelled
    from services.ocr import disk_guard

    disk_guard()
    story = await _load_story(story_id)
    channel = await _load_channel(story)
    script = story.get("script") or {}
    chunks = script.get("chunks") or []
    if not chunks:
        raise RuntimeError("no media items to stitch")
    chunks = chunks[:30]

    await set_story_async(story_id, status="rendering", stage="Stitching your media", error="")
    ctx = {"story_id": story_id, "story": story, "channel": channel,
           "script": script, "chunks": chunks}
    clips = []
    sem = asyncio.Semaphore(2)
    await asyncio.gather(*[
        _stitch_item(ctx, i, c, clips, sem, setp, job_id) for i, c in enumerate(chunks)])
    clips.sort()
    if job_id and is_cancelled(job_id):
        raise JobCancelled()

    main, mixed, final = await _stitch_assemble(ctx, clips, setp)
    meta = await _stitch_metadata(story, channel)

    await set_story_async(
        story_id, status="in_review", stage="Awaiting review",
        qa={"passed": None, "note": "QA agent skipped — stitched-media videos have no AI script"},
        metadata=meta,
        media={"final": f"/api/media/final/{story_id}.mp4",
               "duration_sec": round(media.ffprobe_duration(final), 1),
               "thumbnail": f"/api/media/thumbs/{story_id}.jpg"})

    for inter in (main, mixed if mixed != main else None):  # intermediates are rebuildable
        if inter:
            Path(inter).unlink(missing_ok=True)
    for ch in chunks:  # free the uploaded originals once the final render exists
        Path(ch.get("media_path", "")).unlink(missing_ok=True)
