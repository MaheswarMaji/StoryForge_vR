"""Deterministic parser for user-supplied scene scripts.

Structured scripts are source material, not prompts: their dialogue and visuals
must be preserved instead of rewritten by an LLM.

A script is considered "structured" when it contains at least one recognisable
scene block.  Recognised synonyms:

  * Scene header:   सीन N | दृश्य N | scene N (with optional 🎬/🎞️/🎥 prefixes
                    and an optional time-range or label after the number).
  * Voiceover:      वॉइसओवर | वॉयसओवर | नैरेशन | voice[- ]over | narration.
  * Dialogue block: संवाद:  followed by ``Speaker: "text"`` lines.
  * Image prompt:   विज़ुअल | visuals? | इमेज प्रॉम्प्ट | image prompt | 🖼️.
  * Video prompt:   वीडियो प्रॉम्प्ट | video prompt | 🎥.

Only the fields that are supplied are captured.  Missing ones are flagged on
the chunk (``needs_voiceover``, ``needs_visual``, ``needs_video_prompt``) so
that downstream generation can fill in only the gaps.
"""
import re


SCENE_RE = re.compile(
    r"(?im)^\s*(?:[🎬🎞️🎥]\s*)?"
    r"(?:सीन|दृश्य|scene)\s*(\d+)"
    r"(?:\s*\([^)]*\))?"       # optional (0:00–0:07) style timestamp
    r"\s*(?:[|:.\-–—]\s*)?"    # optional separator
    r"([^\n]*)$"               # rest of the header line is the label
)

# Individual speech / narration markers.  ``संवाद:`` is intentionally NOT here —
# it is a container that holds multiple ``Speaker: "text"`` lines and is parsed
# separately by :func:`_dialogue_quotes`.
SPEECH_RE = re.compile(
    r"(?im)("
    r"नैरेशन(?:\s*\(\s*वॉयसओवर\s*\))?(?:\s*\+\s*CTA)?"
    r"|वॉयसओवर|वॉइसओवर"
    r"|संवाद\s*[—–\-]\s*[^:\n]+"                 # legacy: संवाद — राजा:
    r"|narration(?:\s*\(\s*voiceover\s*\))?"
    r"|voice\s*-?\s*over"
    r"|dialogue\s*[—–\-]\s*[^:\n]+"
    r")\s*:"
)

# Any line that starts with ``संवाद`` (used to skip past the container marker
# so we can pick up the quotes underneath it).
DIALOGUE_HEADER_RE = re.compile(r"(?im)^\s*(?:🗣️\s*)?संवाद\s*:\s*$")

VISUAL_RE = re.compile(
    r"(?im)(?:🖼️\s*)?"
    r"(?:विज़ुअल|विजुअल|visuals?|scene\s+visual|"
    r"इमेज\s*प्रॉम्प्ट|image\s*prompt)\s*:"
)

VIDEO_PROMPT_RE = re.compile(
    r"(?im)(?:🎥\s*)?(?:वीडियो\s*प्रॉम्प्ट|video\s*prompt)\s*:"
)

NOTES_RE = re.compile(
    r"(?im)^\s*(?:🎵\s*|💡\s*)?"
    r"(?:प्रोडक्शन\s+नोट्स|production\s+notes|काम\s+की\s+टिप्स|tips?)\s*$"
)

BIBLE_START_RE = re.compile(
    r"(?im)^.*(?:पात्र\s+एवं\s+दृश्य\s+संगति\s+गाइड|"
    r"consistency\s+bible|character\s+(?:&|and)\s+style\s+guide|"
    r"कैरेक्टर\s*(?:कंसिस्टेंसी|रेफ़रेंस)|character\s*reference|"
    r"स्टाइल\s*ब्लॉक|style\s*block).*$"
)

SCRIPT_START_RE = re.compile(
    r"(?im)^.*(?:सीन-दर-सीन\s+स्क्रिप्ट|scene-by-scene\s+script|"
    r"दृश्य-दर-दृश्य|scene\s+by\s+scene).*$"
)

# Handles both straight ("...") and curly (“...”) quotes.
QUOTE_RE = re.compile(r"[\"“](.*?)[\"”]", re.DOTALL)

# ``Speaker: "quote"`` matches (used inside a संवाद: block).
SPEAKER_LINE_RE = re.compile(
    r"(?m)^\s*[^\n:]{1,60}\s*:\s*[\"“](.+?)[\"”]\s*$",
    re.DOTALL,
)

TITLE_LABEL_RE = re.compile(r"(?im)^\s*(?:शीर्षक|title)\s*:\s*(.+?)\s*$")

# Lines that look like format/metadata (skipped when guessing the title).
METADATA_PREFIXES = (
    "(", "फ़ॉर्मेट", "फॉर्मेट", "format", "फ़ॉर्मैट",
    "duration", "अवधि", "language", "भाषा",
)


def _clean(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value).strip(" \t\r\n")


def _camera(text: str) -> str:
    value = text.lower()
    if re.search(r"zoom[\s-]*out|ज़ूम[\s-]*आउट|जूम[\s-]*आउट", value):
        return "zoom_out"
    if re.search(r"pan[\s-]*left|पैन[\s-]*लेफ्ट", value):
        return "pan_left"
    if re.search(r"pan[\s-]*right|पैन[\s-]*राइट", value):
        return "pan_right"
    if re.search(r"static|स्थिर", value):
        return "static"
    return "zoom_in"


def _beat(index: int, total: int, label: str) -> str:
    value = label.lower()
    if index == 0 or "hook" in value or "हुक" in value:
        return "hook"
    if (index == total - 1 or "closing" in value or "क्लोज" in value
            or "समापन" in value or "lesson" in value or "सीख" in value):
        return "lesson"
    if "climax" in value or "प्रकटन" in value or "चरम" in value or "प्राकट्य" in value:
        return "climax"
    if "twist" in value or "मोड़" in value or "तपस्या" in value:
        return "twist"
    if "action" in value or "वरदान" in value:
        return "action"
    ratio = index / max(total - 1, 1)
    if ratio < 0.45:
        return "story"
    if ratio < 0.62:
        return "twist"
    if ratio < 0.78:
        return "climax"
    return "action"


def _emotion(label: str) -> str:
    parenthetical = re.findall(r"\(([^)]*)\)", label)
    return _clean(parenthetical[-1] if parenthetical else label) or "storytelling"


def _dialogue_quotes(block: str) -> list[str]:
    """Extract ``Speaker: "quote"`` lines from a संवाद: container."""
    quotes = []
    for match in SPEAKER_LINE_RE.finditer(block):
        text = _clean(match.group(1))
        if text:
            quotes.append(text)
    return quotes


def _spoken_text(body: str) -> str:
    """Collect voiceover / narration / dialogue text from a scene body."""
    parts: list[str] = []

    # 1) Standalone speech markers (नैरेशन:, वॉइसओवर:, संवाद — राजा:, etc.)
    matches = list(SPEECH_RE.finditer(body))
    for i, marker in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        # stop at the next section header (visual/video/notes) if it appears first
        for stopper in (VISUAL_RE, VIDEO_PROMPT_RE, NOTES_RE, DIALOGUE_HEADER_RE):
            m = stopper.search(body, marker.end(), end)
            if m and m.start() < end:
                end = m.start()
        content = body[marker.end():end]
        quotes = [_clean(q) for q in QUOTE_RE.findall(content) if _clean(q)]
        if quotes:
            parts.extend(quotes)
        else:
            plain = _clean(content)
            if plain:
                parts.append(plain)

    # 2) ``संवाद:`` container — extract every "Speaker: quote" line inside it.
    dialog = DIALOGUE_HEADER_RE.search(body)
    if dialog:
        end = len(body)
        for stopper in (VISUAL_RE, VIDEO_PROMPT_RE, NOTES_RE, SPEECH_RE):
            m = stopper.search(body, dialog.end())
            if m and m.start() < end:
                end = m.start()
        for q in _dialogue_quotes(body[dialog.end():end]):
            if q not in parts:
                parts.append(q)

    return "\n".join(parts)


def _prompt_text(body: str, marker: "re.Pattern[str]") -> str:
    """Return the text under a ``visual:`` / ``video prompt:`` heading."""
    match = marker.search(body)
    if not match:
        return ""
    end = len(body)
    for stopper in (SPEECH_RE, VISUAL_RE, VIDEO_PROMPT_RE, NOTES_RE, DIALOGUE_HEADER_RE):
        if stopper is marker:
            continue
        m = stopper.search(body, match.end())
        if m and m.start() < end:
            end = m.start()
    return _clean(body[match.end():end])


def _consistency_bible(text: str, first_scene_start: int) -> str:
    head = text[:first_scene_start]
    start = BIBLE_START_RE.search(head)
    if not start:
        return ""
    script_marker = SCRIPT_START_RE.search(head, start.end())
    end = script_marker.start() if script_marker else len(head)
    bible = _clean(head[start.end():end])
    return bible[:12000]


def _title(text: str) -> str:
    label = TITLE_LABEL_RE.search(text)
    if label:
        return _clean(label.group(1)).strip('"')[:150]
    for line in text.splitlines():
        clean = _clean(line).strip('"')
        if not clean:
            continue
        lower = clean.lower()
        if any(lower.startswith(prefix.lower()) for prefix in METADATA_PREFIXES):
            continue
        # Strip a leading emoji-plus-space if present so "🌟 Title" -> "Title".
        clean = re.sub(r"^[^\w\u0900-\u097F(]+", "", clean).strip()
        if clean:
            return clean[:150]
    return "Imported scene script"


def parse_scene_script(text: str) -> dict | None:
    matches = list(SCENE_RE.finditer(text))
    if not matches:
        return None

    chunks = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        # Trim off any "production notes" / "tips" section that follows the last scene.
        raw_body = text[match.end():end]
        notes_stop = NOTES_RE.search(raw_body)
        body = raw_body[:notes_stop.start()] if notes_stop else raw_body

        voiceover = _spoken_text(body)
        visual = _prompt_text(body, VISUAL_RE)
        video_prompt = _prompt_text(body, VIDEO_PROMPT_RE) or visual

        # Only keep a scene if the user supplied SOMETHING for it; otherwise the
        # scene is empty scaffolding and we skip it entirely.
        if not (voiceover or visual or video_prompt):
            continue

        label = _clean(match.group(2))
        chunk = {
            "chunk_id": f"scene-{match.group(1)}",
            "source_scene": int(match.group(1)),
            "source_label": label,
            "beat": "story",  # assigned after skipped/usable scenes are known
            "voiceover": voiceover[:4000],
            "visual": visual[:4000],
            "video_prompt": video_prompt[:4000],
            "camera": _camera(visual or video_prompt),
            "emotion": _emotion(label),
            "needs_voiceover": not bool(voiceover),
            "needs_visual": not bool(visual),
            "needs_video_prompt": not bool(video_prompt),
        }
        chunks.append(chunk)

    if not chunks:
        return None
    for idx, chunk in enumerate(chunks):
        chunk["beat"] = _beat(idx, len(chunks), chunk["source_label"])

    notes_match = NOTES_RE.search(text)
    notes = _clean(text[notes_match.end():]) if notes_match else ""

    missing = {
        "voiceover": [c["chunk_id"] for c in chunks if c["needs_voiceover"]],
        "visual": [c["chunk_id"] for c in chunks if c["needs_visual"]],
        "video_prompt": [c["chunk_id"] for c in chunks if c["needs_video_prompt"]],
    }

    return {
        "title": _title(text),
        "character_sheet": _consistency_bible(text, matches[0].start()),
        "chunks": chunks,
        "production_notes": notes[:8000],
        "missing": missing,
        "is_partial": any(missing.values()),
    }
