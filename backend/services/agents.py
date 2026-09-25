import json

from services.llm import ask_json, ask_json_fast, ask_json_reasoning, estimate_llm_cost, pick_creative_model

# ── System prompts ────────────────────────────────────────────────────────────

STORY_SYSTEM = (
    "You are an expert story analyst for Indian mythology (Puranas, Bhagwat, Ramayana, Mahabharata), "
    "Panchatantra and folk traditions (Thakurmar Jhuli, Bengali and Hindi folk tales). You find short, "
    "self-contained stories with strong visual and emotional arcs that will work as 60-120 second vertical videos. "
    "You always respond with valid JSON only."
)

SCRIPT_SYSTEM = (
    "You are a viral video scriptwriter for Indian mythology and folk story channels. You master the retention "
    "structure Hook > Story > Twist > Climax > Action > Lesson, and write gripping, emotional, visually vivid "
    "scripts. You always respond with valid JSON only."
)

QA_SYSTEM = (
    "You are a meticulous quality-control analyst for short-form video content. You evaluate scripts against "
    "beat integrity, character/visual consistency, pacing, content safety and retention. You always respond with valid JSON only."
)

METADATA_SYSTEM = (
    "You are a YouTube Shorts / Instagram Reels growth expert for Indian mythology and folk story channels. "
    "You write click-worthy titles, rich descriptions and trending hashtags. You always respond with valid JSON only."
)

EDITOR_SYSTEM = (
    "You are the chief editor and fact-checker of an AI story-video studio for mythology, folk tales and "
    "public-interest news. You verify scripts against source material, correct factual and continuity errors, "
    "strengthen hooks and payoffs, and score viral potential honestly. You always respond with valid JSON only."
)

IMPROVE_SYSTEM = (
    "You are the improvement coach of an AI story-video studio. After a video has been assessed you pinpoint "
    "the single weakest scope and propose minimal, targeted edits that can only raise the overall viral score. "
    "You never invent story facts, never repeat a scope that already failed to improve the score, and prefer "
    "zero edits over churn. You always respond with valid JSON only."
)


# ── Story identification ──────────────────────────────────────────────────────

async def identify_stories(book_title: str, text: str, channel_name: str):
    prompt = f"""Read the following OCR text from the scanned book "{book_title}" and identify ALL self-contained stories suitable for 60-120 second vertical videos for the channel "{channel_name}".

CRITERIA: each story must have a beginning, conflict, resolution and a moral or emotional payoff; must be self-contained; must have visual potential; emotional arc.
Extract for each story:
- title_hindi, title_english (translate if the text is Bengali/English, transliterate names)
- source (book + chapter reference), page_start, page_end (integers, best-effort from the text)
- category: Mythology | Moral | Folk | Ghost
- characters: [{{"name": str, "description": str (concise VISUAL description: age, clothing, build, aura)}}]
- setting, conflict, twist, climax, resolution, moral (one line each)
- emotional_tone (e.g. Devotional, Suspense, Horror, Hope), target_audience: Kids | General
- visual_style (e.g. "Indian miniature painting", "whimsical storybook illustration"), estimated_length: "60s"|"90s"|"120s"

Return JSON: {{"stories": [ ... ]}}. If no suitable story is found return {{"stories": []}}.

OCR TEXT:
{text[:22000]}"""
    # Creative/analytical task → main model (qwen2.5:72b)
    data = await ask_json(STORY_SYSTEM, prompt, session="story-id")
    cost = estimate_llm_cost(prompt, json.dumps(data))
    stories = data.get("stories", []) if isinstance(data, dict) else []
    return stories, cost


# ── Script writing ────────────────────────────────────────────────────────────

async def write_script(story: dict, channel: dict, target_seconds: int = 90):
    target_seconds = max(30, min(240, int(target_seconds or 90)))
    n_chunks = max(3, round(target_seconds / 10))
    kids = channel.get("is_kids")
    story_view = {k: v for k, v in story.items() if k not in ("source_text", "script", "media")}
    source = (story.get("source_text") or "").strip()
    source_block = f"\n\nSOURCE SCRIPT/PROMPT (user-provided — follow this content faithfully):\n{source[:12000]}" if source else ""
    prompt = f"""Create a ~{target_seconds} second vertical video script for this story, for the channel "{channel.get('name')}".

CHANNEL DIRECTION:
- Narration language: {channel.get('language', 'hi')} (write voiceover text in this language & script)
- Tone: {channel.get('tone')}
- Safety: {"STRICT for kids (7+): no gore, no terrifying imagery, ghosts must be playful/gentle, moral must be kind" if kids else "General audience 13+: tension and mild darkness allowed, no explicit content"}
- Visual style: {channel.get('style_prefix')}
- CTA: {channel.get('cta_text')}
{source_block}
STORY JSON:
{json.dumps(story_view, ensure_ascii=False, default=str)}

REQUIREMENTS:
- EXACTLY {n_chunks} chunks of ~10 seconds each (total ≈ {target_seconds}s; may run ±15%).
- Across the chunks, cover ALL SIX beats in order: hook (first chunk), story, twist, climax, action, lesson. Multiple chunks may share a beat (e.g. two 'story' chunks), but hook is only chunk 1 and lesson is the last chunk which must end with the CTA woven naturally.
- Each chunk: "beat" (hook|story|twist|climax|action|lesson), "voiceover" (max 28 words, spoken word for word; chunk 1 must be an irresistible question or shocking line), "visual" (one-line scene summary), "video_prompt" (DETAILED English prompt for AI image generation: style, subject, action, setting, lighting, mood; 9:16 vertical; visuals must REVEAL information the narration does not state verbatim), "camera" (zoom_in|zoom_out|pan_left|pan_right|static), "emotion", "music_mood" (devotional|suspense|horror|moral|action|sad|happy), "cast" (list of character IDs from the character sheet who are PHYSICALLY VISIBLE in this scene — use exact IDs, empty list [] for environment-only scenes).
- "character_sheet": {{"anchor": a single dense paragraph describing EVERY recurring character's exact appearance (age, face, hair, clothing colors, build, accessories) plus the global art style and color palette — this anchor will be reused verbatim for every generated image to keep characters identical across segments.}}
- "voice": {{"language", "tone"}}, "music": {{"instruments": [..]}}, "cta_text".

Return ONLY valid JSON."""
    # Creative writing → main model (qwen2.5:72b)
    data = await ask_json(SCRIPT_SYSTEM, prompt, session="script")
    cost = estimate_llm_cost(prompt, json.dumps(data))
    return data, cost


# ── Segment an existing script (LLM, CPU) ──────────────────────────────────────

SEGMENT_SYSTEM = (
    "You are a video pre-production supervisor. You take a WRITER'S EXISTING script (narration, "
    "dialogue and stage directions) and break it into ~10-second vertical-video scenes WITHOUT "
    "inventing new plot. You preserve the author's narration and dialogue wording faithfully, build "
    "one character-consistency sheet, assign the visible cast per scene, and write vivid English "
    "image/video prompts. You always respond with valid JSON only."
)


async def segment_script(story: dict, channel: dict, target_seconds: int = 90):
    """Turn a pasted freeform script into a character sheet + per-scene cast/narration/visuals.

    Unlike write_script (which invents a story from a prompt), this preserves the author's
    existing narration/dialogue and only STRUCTURES it. Runs on a CPU-fit Ollama model.
    """
    target_seconds = max(30, min(240, int(target_seconds or 90)))
    n_chunks = max(3, round(target_seconds / 10))
    lang = channel.get("language", "hi")
    source = (story.get("source_text") or "").strip()
    if not source:
        raise ValueError("no script text to segment")
    prompt = f"""Segment the EXISTING script below into a shot list for a ~{target_seconds}s vertical video on channel "{channel.get('name')}".

DO NOT invent a new story or change the plot. Preserve the author's narration/dialogue wording (translate into {lang} ONLY if it is not already in that language). Split long passages and merge tiny ones; aim for about {n_chunks} scenes of ~10 seconds each (may run ±15%).

Return ONLY valid JSON:
{{
  "title": "<short title taken from the script>",
  "characters": [{{"name": "<name>", "description": "<dense VISUAL description: age, face, hair, clothing colors, build, accessories>", "aliases": ["<other spellings>"]}}],
  "character_sheet": {{"anchor": "<ONE dense paragraph describing EVERY recurring character's exact appearance PLUS the global art style and color palette — reused verbatim for every generated image so characters stay identical>"}},
  "chunks": [
    {{
      "beat": "hook|story|twist|climax|action|lesson",
      "voiceover": "<the spoken narration/dialogue for this scene, in {lang}, max ~28 words, taken faithfully from the script>",
      "visual": "<one-line scene summary>",
      "video_prompt": "<DETAILED English prompt for AI image/video: subject, action, setting, lighting, mood; 9:16 vertical; no text; no watermark>",
      "camera": "zoom_in|zoom_out|pan_left|pan_right|static",
      "emotion": "<emotion>",
      "music_mood": "devotional|suspense|horror|moral|action|sad|happy",
      "cast": ["<exact character name from the characters list who is PHYSICALLY VISIBLE in this scene>"]
    }}
  ]
}}

RULES:
- Chunk 1 beat = hook. The last chunk beat = lesson and weaves the CTA "{channel.get('cta_text', '')}" in naturally.
- "cast" lists ONLY people physically visible in the frame (empty list [] for environment-only scenes). Use the SAME names as in "characters".
- Visual style for every prompt: {channel.get('style_prefix')}
- Safety: {"STRICT for kids (7+): gentle, no gore or terror" if channel.get('is_kids') else "General audience 13+"}

SCRIPT:
{source[:16000]}"""
    model = pick_creative_model()
    data = await ask_json(SEGMENT_SYSTEM, prompt, session="segment-script", model=model)
    cost = estimate_llm_cost(prompt, json.dumps(data, ensure_ascii=False))
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list) or not data["chunks"]:
        raise ValueError("script segmentation returned no scenes")
    return data, cost




async def regenerate_chunk(story: dict, channel: dict, index: int):
    chunks = (story.get("script") or {}).get("chunks") or []
    if index < 0 or index >= len(chunks):
        raise ValueError("invalid segment index")
    current  = chunks[index]
    previous = chunks[index - 1] if index > 0 else {}
    following = chunks[index + 1] if index + 1 < len(chunks) else {}
    prompt = f"""Rewrite only segment {index + 1} of this short vertical video script.

Keep the story facts, beat ({current.get('beat', 'story')}), language, recurring characters, and continuity intact.
Make the narration natural to speak in about 8-12 seconds. Make the visual reveal a specific cinematic moment rather than repeating the narration.
Return ONLY JSON with exactly these string fields: voiceover, visual, video_prompt, cast (list of visible character IDs for this scene — empty list [] for environment-only).
The video_prompt must be a detailed English prompt for a high-quality 9:16 image/video frame: clear subject, action, setting, lighting, mood, anatomy, composition, no text, no watermark.

CHANNEL: {json.dumps({k: channel.get(k) for k in ('name', 'language', 'tone', 'is_kids')}, ensure_ascii=False)}
CHARACTER SHEET: {(story.get('character_sheet') or {}).get('anchor', '')[:1800]}
PREVIOUS SEGMENT: {json.dumps(previous, ensure_ascii=False, default=str)}
CURRENT SEGMENT: {json.dumps(current, ensure_ascii=False, default=str)}
NEXT SEGMENT: {json.dumps(following, ensure_ascii=False, default=str)}"""
    # Creative rewrite → main model (qwen2.5:72b)
    data = await ask_json(SCRIPT_SYSTEM, prompt, session=f"segment-script-{index}")
    cost = estimate_llm_cost(prompt, json.dumps(data, ensure_ascii=False))
    if not isinstance(data, dict) or not data.get("voiceover") or not data.get("video_prompt"):
        raise ValueError("segment rewrite returned incomplete data")
    return data, cost


# ── Review edits ──────────────────────────────────────────────────────────────

async def apply_review_edits(story: dict, channel: dict, notes: str):
    prompt = f"""Apply the reviewer's requested edits to this short vertical video script.

Reviewer notes:
{notes[:4000]}

Return ONLY JSON with:
- edits: a list of at most 6 objects, each with index (integer) and any changed string fields among voiceover, visual, video_prompt, camera, emotion, cast (list of visible character IDs for that scene)
- summary: one short sentence describing what was changed

Only edit the segments needed by the notes. Preserve source facts, recurring-character continuity, the beat order, and the channel language. Keep video_prompt detailed, cinematic, English, vertical 9:16, with no text or watermark.

CHANNEL: {json.dumps({k: channel.get(k) for k in ('name', 'language', 'tone', 'is_kids')}, ensure_ascii=False)}
CHARACTER SHEET: {(story.get('character_sheet') or {}).get('anchor', '')[:1800]}
SCRIPT: {json.dumps(story.get('script') or {}, ensure_ascii=False, default=str)}"""
    # Editing/reasoning → deepseek-r1:32b
    data = await ask_json_reasoning(EDITOR_SYSTEM, prompt, session="review-edits")
    cost = estimate_llm_cost(prompt, json.dumps(data, ensure_ascii=False))
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise ValueError("review edit response was incomplete")
    return data, cost


# ── QA ────────────────────────────────────────────────────────────────────────

async def run_qa(story: dict, channel: dict):
    prompt = """QA-check this short-video script. Evaluate:
1. beats_present — all six beats hook/story/twist/climax/action/lesson appear across chunks (check the "beat" fields AND narrative content)
2. character_consistency — visual prompts + character sheet keep recurring characters/places visually identical; cast lists match image prompts
3. pacing — 6-8 chunks, no voiceover over ~32 words (chunk would overrun ~10s), hook lands in first 5s
4. content_safety — vs channel safety level "{safety}"{kids}
5. hook_strength — rate 0-10 (>7 to pass)
6. retention_prediction — predicted avg view duration percent 0-100 (>60 to pass)

Return JSON: {{"passed": bool, "score": 0-10, "checks": [{{"name": str, "passed": bool, "detail": str}}], "issues": [str], "suggestions": [str]}}

CHANNEL: {ch}
SCRIPT: {script}"""
    prompt = prompt.format(
        safety=channel.get("safety_level"),
        kids=" — age-appropriate for children, playful not terrifying" if channel.get("is_kids") else "",
        ch=json.dumps({k: channel.get(k) for k in ("name", "tone", "is_kids", "safety_level")},
                      ensure_ascii=False, default=str),
        script=json.dumps(story.get("script", {}), ensure_ascii=False, default=str),
    )
    # QA/evaluation → deepseek-r1:32b
    data = await ask_json_reasoning(QA_SYSTEM, prompt, session="qa")
    cost = estimate_llm_cost(prompt, json.dumps(data))
    return data, cost


# ── Metadata ──────────────────────────────────────────────────────────────────

async def make_metadata(story: dict, channel: dict):
    prompt = f"""Generate viral publishing metadata for a 60-120s YouTube Shorts / Instagram Reels video.

Story: {json.dumps({k: story.get(k) for k in ("title_hindi", "title_english", "moral", "category", "emotional_tone")}, ensure_ascii=False, default=str)}
Script hook: {story.get('script', {}).get('chunks', [{}])[0].get('voiceover', '')}
Channel: {channel.get('name')}

Return JSON: {{"title": str (<=95 chars, curiosity gap, Hinglish welcome), "description": str (2-3 lines + moral + call to action), "hashtags": [str] (12-18 relevant, mix broad + niche, no # symbol), "thumbnail_text": str (<=5 punchy words for the thumbnail, matching narration language or English)}}"""
    # Simple structured output → llama3.1:8b
    data = await ask_json_fast(METADATA_SYSTEM, prompt, session="meta")
    cost = estimate_llm_cost(prompt, json.dumps(data))
    return data, cost


# ── Editor pass ───────────────────────────────────────────────────────────────

async def editor_pass(story: dict, channel: dict, source_text: str):
    prompt = f"""Review this short-video script against the SOURCE TEXT below.

SOURCE TEXT (trust this over the script when they conflict):
{source_text[:14000]}

SCRIPT:
{json.dumps(story.get('script', {}), ensure_ascii=False, default=str)}

CHANNEL: {json.dumps({k: channel.get(k) for k in ("name", "language", "is_kids", "safety_level")}, ensure_ascii=False, default=str)}

TASKS:
1. FACT CHECK — verify every event, character, object and their order against the source (who did what to whom; e.g. WHO received/ate/gave the fruit, WHO gave birth to whom). List every segment whose voiceover or visuals need correction. Correct the script to MATCH THE SOURCE, never the other way around.
2. EDITORIAL PASS — make the hook (segment 0) an irresistible information gap in the narration language; ensure the twist is set up, the lesson pays off emotionally, and visual prompts REVEAL information instead of repeating the narration word-for-word. Do not invent facts.
3. VIRAL SCORE (0-100, weighted): concept 20, curiosity 15, emotion 15, novelty 10, thumbnail 10, title 10, hook 10, retention structure 5, shareability 5.

Return ONLY valid JSON:
{{"factual_ok": bool,
 "corrections": [{{"index": int, "voiceover": str, "visual": str, "video_prompt": str, "reason": str}}],
 "hook_line": str (improved voiceover for segment 0, or "" if already strong),
 "silence_before_index": int (segment index AFTER which 0.6s dramatic silence plays; -1 if none),
 "viral_score": {{"total": int, "concept": int, "curiosity": int, "emotion": int, "novelty": int, "thumbnail": int, "title": int, "hook": int, "structure": int, "shareability": int, "verdict": str (one line: reject <60 | weak 60-70 | interesting 70-80 | strong 80-90 | produce first 90+)}},
 "editor_notes": [str] (max 5 short human-readable notes)}}"""
    # Fact-checking/reasoning → deepseek-r1:32b
    data = await ask_json_reasoning(EDITOR_SYSTEM, prompt, session="editor")
    cost = estimate_llm_cost(prompt, json.dumps(data))
    return data, cost


# ── Improvement pass ──────────────────────────────────────────────────────────

async def improvement_pass(story: dict, channel: dict, history: list):
    prompt = f"""A short video was produced and assessed by the editor. Pinpoint the EXACT scope of improvement and propose minimal targeted edits that raise the overall viral score.

CURRENT SCORE BREAKDOWN:
{json.dumps(story.get('script', {}).get('viral_score', {}), ensure_ascii=False, default=str)}

EDITOR NOTES FROM ASSESSMENT:
{json.dumps(story.get('script', {}).get('editor', {}), ensure_ascii=False, default=str)}

PREVIOUS IMPROVEMENT ROUNDS (do not repeat these scopes; rounds marked accepted=false did NOT improve the score):
{json.dumps(history, ensure_ascii=False, default=str)}

SCRIPT:
{json.dumps(story.get('script', {}), ensure_ascii=False, default=str)}

CHANNEL: {json.dumps({k: channel.get(k) for k in ("name", "language", "tone", "is_kids")}, ensure_ascii=False, default=str)}

RULES:
- Pick ONE weakest scope: hook | twist setup | emotional stakes | visual reveal | pacing | lesson payoff | title-thumbnail curiosity.
- Edits for at most 3 chunks (plus optionally a new hook_line for chunk 0). Keep every edit factually identical to the source story — never invent events.
- If the script is already strong, return zero edits rather than churn.

Return ONLY valid JSON:
{{"scope": str, "why": str (one line), "edits": [{{"index": int, "voiceover": str, "visual": str, "video_prompt": str, "reason": str}}], "hook_line": str (improved chunk-0 voiceover, or "" if already strong), "expected_gain": int}}"""
    # Reasoning → deepseek-r1:32b
    data = await ask_json_reasoning(IMPROVE_SYSTEM, prompt, session="improve")
    cost = estimate_llm_cost(prompt, json.dumps(data))
    return data, cost
