"""Adapter for the local Studio still-image worker (studio_story_batch batch.py) on server27.

The worker has no HTTP API.  StoryForge launches it as a subprocess (never a shell
string), one process at a time, and reads <output>/batch_r001/response.json while it
runs.  Images appear individually.  COMPLETE only means "generated": acceptance stays
with StoryForge's own review pipeline.

Key fixes in this version
-------------------------
1. characters_from() handles THREE formats for the consistency sheet:
   (a) story.characters list (set by LLM script generation)
   (b) colon-delimited lines  "CharName: description…"  (_LINE regex)
   (c) pipe or tab-separated TABLE  (Dhruv-style production briefs)
2. build_request() uses key-presence to distinguish:
   - 'cast' key absent   → no declaration → _mentions() fallback
   - 'cast' = []         → explicit empty → environment-only scene (allowed)
   - 'cast' = [ids…]     → explicit list  → matched against character roster
3. resolved_request() skips env-only (cast=[]) scenes during director resolution.
"""
import asyncio
import hashlib
import io
import json
import os
import re
import time
import fcntl
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from db import db
from services.generation import ProviderFailure, generation_context, redact

MAX_SCENES      = 24
RELAUNCH_AFTER  = 120   # seconds before a finished launch can be reused


_GLOBAL   = asyncio.Lock()   # worker rejects concurrent runs → serialise here
_LAUNCHES: dict = {}


# ──────────────────────────────────────── configuration ──────────────────────

def paths():
    root   = Path(os.environ.get("STUDIO_ROOT",         "/home/siddhesh/studio"))
    python = Path(os.environ.get("STUDIO_PYTHON",       str(root / "runtime/envs/studio-v007/bin/python")))
    script = Path(os.environ.get("STUDIO_BATCH_SCRIPT", str(root / "tools/studio_story_batch_v0112/batch.py")))
    return root, python, script


def check():
    """(ok, message) — is the local worker installed and runnable?"""
    root, python, script = paths()
    problems = []
    if not root.is_dir():
        problems.append(f"studio root not found: {root}")
    if not (python.is_file() and os.access(python, os.X_OK)):
        problems.append(f"worker Python not executable: {python}")
    if not script.is_file():
        problems.append(f"worker script not found: {script}")
    if not problems and (root / "projects").exists() and not os.access(root / "projects", os.W_OK):
        problems.append(f"no write access to {root / 'projects'}")
    return (not problems), ("; ".join(problems) or f"Worker found at {script}")


def is_configured():
    return check()[0]


# ──────────────────────────────────── character parsing ──────────────────────

def _slug(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or f"char{len(taken) + 1}"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


# Matches: "Name: description…"  /  "**Name**: description…"  /  "- Name — description…"
_LINE = re.compile(
    r"^\s*(?:[-•*▪●]|\d+[.)])?\s*\**\s*([^:：\n]{1,40}?)\s*\**\s*"
    r"(?::|：|\s[—–-]\s)\s*(.{15,})$"
)

# Header cell values to skip when parsing tables
_TABLE_SKIP = {
    "id", "character", "name", "appearance", "fixed", "fixed appearance",
    "restrictions", "notes", "character id", "display name", "description",
}


def _parse_table_characters(anchor: str) -> list:
    """Extract (key, display_name, description) from pipe or tab-delimited tables.

    Handles the Dhruv-style production brief format:
      ID          Character    Fixed appearance                  Restrictions
      dhruv       ध्रुव        Five-year-old Indian boy…         No crown…
    """
    rows = []
    lines = anchor.splitlines()

    # ── Pipe table ────────────────────────────────────────────────────────────
    pipe_lines = [l.strip() for l in lines if "|" in l]
    if len(pipe_lines) >= 2:
        for line in pipe_lines:
            if re.match(r"^[\s|:=-]+$", line):   # separator row  |---|---|
                continue
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) < 3:
                continue
            key_raw = cols[0].lower().strip()
            if key_raw in _TABLE_SKIP or len(key_raw) < 2:
                continue
            name  = cols[1].strip() if len(cols) > 1 else ""
            desc  = cols[2].strip() if len(cols) > 2 else ""
            restr = cols[3].strip() if len(cols) > 3 else ""
            if len(desc) < 15:
                continue
            full = f"{desc}. Restrictions: {restr}" if restr and restr.lower() not in _TABLE_SKIP else desc
            rows.append((key_raw, name or key_raw, full[:1500]))
        if rows:
            return rows

    # ── Tab-delimited table ───────────────────────────────────────────────────
    tab_lines = [l for l in lines if "\t" in l]
    if len(tab_lines) >= 2:
        for line in tab_lines:
            cols = [c.strip() for c in line.split("\t")]
            if len(cols) < 3:
                continue
            key_raw = cols[0].lower().strip()
            if key_raw in _TABLE_SKIP or len(key_raw) < 2:
                continue
            name  = cols[1].strip() if len(cols) > 1 else ""
            desc  = cols[2].strip() if len(cols) > 2 else ""
            restr = cols[3].strip() if len(cols) > 3 else ""
            if len(desc) < 15:
                continue
            full = f"{desc}. Restrictions: {restr}" if restr and restr.lower() not in _TABLE_SKIP else desc
            rows.append((key_raw, name or key_raw, full[:1500]))
        if rows:
            return rows

    return []


def characters_from(story: dict) -> list:
    """Return [(id_key, display_name, description)] for all characters in the story.

    Priority
    --------
    1. story['characters'] list  (LLM-generated scripts)
    2. character_sheet.anchor text → colon-delimited  (_LINE regex)
    3. character_sheet.anchor text → pipe / tab table  (_parse_table_characters)
    """
    # 1) Explicit characters list
    char_list = []
    for c in story.get("characters") or []:
        if isinstance(c, dict) and (c.get("name") or "").strip() and (c.get("description") or "").strip():
            char_list.append((c["name"].strip(), c["name"].strip(), c["description"].strip()))

    if char_list:
        out, taken = [], set()
        for name, display, desc in char_list:
            key = _slug(name, taken)
            taken.add(key)
            out.append((key, display, desc[:1500]))
        if len(out) > 12:
            raise ProviderFailure("Studio supports at most 12 character definitions.", code="INVALID_REQUEST")
        return out

    # 2 + 3) Parse from character_sheet.anchor
    anchor = ((story.get("character_sheet") or {}).get("anchor") or "").strip()
    if not anchor:
        raise ProviderFailure(
            "No character definitions found. Add a character consistency sheet with one "
            "definition per line (\"Name: description\") or a table with "
            "ID | Name | Appearance | Restrictions columns.",
            code="INVALID_REQUEST",
        )

    # 2) Colon-delimited lines
    colon_rows = []
    for line in anchor.splitlines():
        m = _LINE.match(line)
        if m:
            colon_rows.append((m.group(1).strip(" *"), m.group(2).strip()))
    if colon_rows:
        out, taken = [], set()
        for name, desc in colon_rows:
            key = _slug(name, taken)
            taken.add(key)
            out.append((key, name, desc[:1500]))
        if len(out) > 12:
            raise ProviderFailure("Studio supports at most 12 character definitions.", code="INVALID_REQUEST")
        return out

    # 3) Table format
    table_rows = _parse_table_characters(anchor)
    if table_rows:
        out, taken = [], set()
        for key_raw, display, desc in table_rows:
            key = _slug(key_raw, taken)
            taken.add(key)
            out.append((key, display, desc))
        if len(out) > 12:
            raise ProviderFailure("Studio supports at most 12 character definitions.", code="INVALID_REQUEST")
        return out

    raise ProviderFailure(
        "Could not extract character definitions from the consistency sheet. "
        "Use one definition per line (\"CharacterName: full visual description\") "
        "or a table with columns ID | Name | Appearance | Restrictions.",
        code="INVALID_REQUEST",
    )


def _mentions(text: str, name: str) -> bool:
    text = text.lower()
    parts = {name.lower(), name.split()[0].lower()} if name.split() else {name.lower()}
    return any(p and re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text) for p in parts)


def _aliases(key: str, name: str, story: dict) -> list:
    found = [name, key]
    for item in story.get("characters") or []:
        if isinstance(item, dict) and item.get("name") == name:
            extra = item.get("aliases", [])
            if isinstance(extra, list):
                found += [x for x in extra if isinstance(x, str)]
    bilingual = {
        "ध्रुव":     ["Dhruv", "Dhruva", "dhruv"],
        "सुरुचि":    ["Suruchi", "suruchi"],
        "सुनीति":    ["Suniti", "Sunithi", "suniti"],
        "नारद":      ["Narada", "Narad", "narada"],
        "विष्णु":    ["Vishnu", "vishnu"],
        "उत्तानपाद": ["Uttanapada", "uttanapada"],
        "उत्तम":     ["Uttama", "uttama"],
    }
    found += bilingual.get(name, [])
    return found


# ───────────────────────────────────── request building ──────────────────────

def build_request(story: dict, channel: dict, project_id: str,
                  prefs_refs=None, allow_unresolved: bool = False) -> dict:
    style = (channel.get("style_prefix") or story.get("visual_style") or "Indian miniature painting style").strip()
    if "9:16" not in style:
        style += ", vertical 9:16"

    chars  = characters_from(story)
    chunks = ((story.get("script") or {}).get("chunks") or [])
    if len(chunks) > MAX_SCENES:
        raise ProviderFailure("At most 24 scenes supported by this adapter.", code="INVALID_REQUEST")
    if not chars:
        raise ProviderFailure("Named character descriptions are required.", code="INVALID_REQUEST")

    scenes = []
    for i, chunk in enumerate(chunks):

        # ── Text blob for mention-based fallback ──────────────────────────────
        text = " ".join(
            str(chunk.get(k) or "")
            for k in ("image_prompt", "visual", "video_prompt", "voiceover", "source_label")
        )

        # ── Declared cast: distinguish key-absent from empty list ─────────────
        # 'cast' key absent       → no declaration  → _mentions() fallback
        # 'cast' = []             → env-only scene  → no characters rendered
        # 'cast' = ["id", …]      → explicit list   → matched against roster
        if "cast" in chunk:
            declared = chunk["cast"]
        elif "character_ids" in chunk:
            declared = chunk["character_ids"]
        else:
            declared = None

        if declared is not None and (
            not isinstance(declared, list)
            or any(not isinstance(x, str) for x in declared)
        ):
            raise ProviderFailure(
                f"Scene {i + 1} cast must be a list of name/ID strings.", code="INVALID_REQUEST"
            )

        # ── Resolve cast ──────────────────────────────────────────────────────
        if declared is not None:
            if len(declared) == 0:
                cast = []   # explicit environment-only scene — valid
            else:
                cast = []
                for item in declared:
                    matched = [
                        k for k, name, _ in chars
                        if any(item.casefold() == a.casefold()
                               for a in _aliases(k, name, story))
                    ]
                    if len(matched) != 1:
                        raise ProviderFailure(
                            f"Unknown or ambiguous cast member \"{item}\" in scene {i + 1}. "
                            f"Available character IDs: {[k for k, _, _ in chars]}",
                            code="INVALID_REQUEST",
                        )
                    if matched[0] not in cast:
                        cast.append(matched[0])
        else:
            # No declared cast → infer from text mentions
            cast = [k for k, name, _ in chars
                    if any(_mentions(text, a) for a in _aliases(k, name, story))]

        # ── Validate cast size ────────────────────────────────────────────────
        if declared is not None:
            # Declared cast: 0 (env-only) or 1–4
            if len(cast) > 4:
                raise ProviderFailure(
                    f"Scene {i + 1} declares {len(cast)} characters (max 4). "
                    "Reduce the cast list or split the scene.",
                    code="INVALID_REQUEST",
                )
        else:
            # Inferred cast: must be 1–4 unless allow_unresolved
            if not 1 <= len(cast) <= 4 and not allow_unresolved:
                raise ProviderFailure(
                    f"Scene {i + 1} inferred {len(cast)} characters. "
                    "Add an explicit \"**Visible cast:**\" line with backtick-quoted IDs, "
                    "or an empty cast for environment-only scenes.",
                    code="INVALID_REQUEST",
                )

        prompt = (chunk.get("image_prompt") or chunk.get("visual") or "").strip()
        if not prompt:
            raise ProviderFailure(
                f"Scene {i + 1} needs an image_prompt or visual description.", code="INVALID_REQUEST"
            )

        scenes.append({"id": f"S{i + 1:03d}", "cast": cast, "image_prompt": prompt[:3000]})

    request = {
        "project_id": project_id,
        "style":      style[:600],
        "characters": {k: d for k, _, d in chars},
        "scenes":     scenes,
    }
    refs = _safe_references(prefs_refs or {}, {k for k, _, _ in chars})
    if refs:
        request["character_reference_paths"] = refs
    return request


# ──────────────────── CPU director: cast resolution for unresolved scenes ────

def validate_cast_resolution(answer: dict, needed: dict, known: set) -> dict:
    if not isinstance(answer, dict) or not isinstance(answer.get("casts"), dict) \
            or set(answer["casts"]) != set(needed):
        raise ProviderFailure(
            "Director did not resolve exactly the requested scenes.", code="CAST_RESOLUTION_FAILED"
        )
    for scene, cast in answer["casts"].items():
        if not isinstance(cast, list) or len(cast) > 4 \
                or any(not isinstance(k, str) or k not in known for k in cast) \
                or len(set(cast)) != len(cast):
            raise ProviderFailure(
                f"Director returned invalid cast for {scene}. "
                "Each scene must have 0–4 unique known character IDs.",
                code="CAST_RESOLUTION_FAILED",
            )
    return answer["casts"]


def _cast_api(path: str, payload=None):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(
        "http://127.0.0.1:11434" + path, data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.load(r)


def _cast_cpu_check():
    data   = _cast_api("/api/ps")
    models = data.get("models")
    if not isinstance(models, list):
        raise ProviderFailure("Cannot verify Ollama CPU placement.", code="DIRECTOR_UNAVAILABLE")
    for m in models:
        if type(m.get("size_vram")) not in (int, float) or m["size_vram"] != 0:
            raise ProviderFailure(
                "Ollama model is on GPU; keep the Studio director on CPU.",
                code="DIRECTOR_UNAVAILABLE",
            )


def _resolve_cast_cached(payload: dict, cache_dir: Path) -> dict:
    model = os.environ.get("STUDIO_DIRECTOR_MODEL", "deepseek-r1:32b")
    if "cloud" in model.lower():
        raise ProviderFailure("Local director model required.", code="DIRECTOR_UNAVAILABLE")
    cache_key = hashlib.sha256(
        json.dumps({"version": 115, "model": model, "input": payload},
                   sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (cache_key + ".json")
    with locked_file(cache_dir / (cache_key + ".lock")):
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get("status") == "COMPLETE":
                return validate_cast_resolution(saved["answer"], payload["scenes"], set(payload["characters"]))
            raise ProviderFailure(
                f"Previous cast resolution failed; see {path}.", code="CAST_RESOLUTION_FAILED"
            )
        record = {"status": "RUNNING", "model": model, "input": payload}

        def _save():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            os.replace(tmp, path)

        _save()
        try:
            _cast_cpu_check()
            sys_prompt = (
                "Resolve the visible cast of each story illustration. "
                "Return ONLY JSON {\"casts\":{\"SCENE_ID\":[\"character_id\"]}}. "
                "Use ONLY the character IDs provided. "
                "Select only people physically depicted — not everyone mentioned in dialogue. "
                "Environment-only scenes (no people) must have an empty list []. "
                "Max four characters per scene. Never add or invent identities. "
                "Data: " + json.dumps(payload, ensure_ascii=False)
            )
            result = _cast_api("/api/chat", {
                "model": model, "stream": False, "format": "json", "keep_alive": "10m",
                "messages": [{"role": "user", "content": sys_prompt}],
                "options": {"num_gpu": 0, "num_ctx": 8192, "num_predict": 1600, "temperature": 0},
            })
            _cast_cpu_check()
            if result.get("done") is not True or result.get("done_reason") == "length":
                raise ValueError("Incomplete director response")
            from services.llm import parse_json
            answer = parse_json(result.get("message", {}).get("content", ""))
            casts  = validate_cast_resolution(answer, payload["scenes"], set(payload["characters"]))
            record.update(status="COMPLETE", answer=answer)
            _save()
            return casts
        except Exception as exc:
            record.update(status="FAILED", error=str(exc))
            _save()
            raise ProviderFailure(
                f"CPU director cast resolution failed: {exc}", code="CAST_RESOLUTION_FAILED"
            ) from exc


async def resolved_request(story: dict, channel: dict, project: str,
                            refs: dict, base: Path) -> dict:
    request = build_request(story, channel, project, refs, allow_unresolved=True)
    # Collect scenes that are neither resolved (1–4) nor explicitly empty (env-only)
    needed = {
        s["id"]: s["image_prompt"]
        for s in request["scenes"]
        if len(s["cast"]) == 0 and "cast" not in (
            ((story.get("script") or {}).get("chunks") or [])[int(s["id"][1:]) - 1]
            if int(s["id"][1:]) - 1 < len((story.get("script") or {}).get("chunks") or [])
            else {}
        )
    } | {
        s["id"]: s["image_prompt"]
        for s in request["scenes"]
        if not 0 <= len(s["cast"]) <= 4 or (
            len(s["cast"]) == 0 and
            "cast" not in (
                ((story.get("script") or {}).get("chunks") or [])[int(s["id"][1:]) - 1]
                if 0 < int(s["id"][1:]) <= len((story.get("script") or {}).get("chunks") or [])
                else {}
            )
        )
    }
    # Simpler: just collect scenes with inferred-empty cast (no declaration, nothing mentioned)
    chunks_list = ((story.get("script") or {}).get("chunks") or [])
    needed = {}
    for s in request["scenes"]:
        idx = int(s["id"][1:]) - 1
        chunk = chunks_list[idx] if 0 <= idx < len(chunks_list) else {}
        has_declared = "cast" in chunk or "character_ids" in chunk
        if not has_declared and len(s["cast"]) == 0:
            # Inferred empty — no declared cast and mentions found nothing → needs director
            needed[s["id"]] = s["image_prompt"]
        elif len(s["cast"]) > 4:
            needed[s["id"]] = s["image_prompt"]

    if not needed:
        return request

    payload = {
        "characters": {key: {"name": name, "description": desc}
                       for key, name, desc in characters_from(story)},
        "scenes": needed,
    }
    casts = await asyncio.to_thread(_resolve_cast_cached, payload, base / "cast_resolution")
    for scene in request["scenes"]:
        if scene["id"] in casts:
            scene["cast"] = casts[scene["id"]]
    return request


# ───────────────────────────────── reference path safety ─────────────────────

def _safe_references(refs: dict, keys: set) -> dict:
    root, _, _ = paths()
    from services import storage
    media_root = Path(__file__).resolve().parent.parent / "media"
    allowed = [root.resolve(), storage.STORAGE_ROOT.resolve(), media_root.resolve()]
    out = {}
    for key, value in refs.items():
        try:
            p = Path(str(value)).resolve()
        except Exception:
            continue
        if (key in keys
                and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                and p.is_file()
                and any(a == p or a in p.parents for a in allowed)):
            out[key] = str(p)
    return out


def _signature(request: dict) -> str:
    data = {k: request[k] for k in ("style", "characters", "scenes", "character_reference_paths")
            if k in request}
    data["reference_hashes"] = {
        k: hashlib.sha256(Path(v).read_bytes()).hexdigest()
        for k, v in request.get("character_reference_paths", {}).items()
    }
    data["worker_path"] = str(paths()[2])
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:10]


def _safe_id(value) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value))[:40] or "story"


# ───────────────────────────────────── process management ────────────────────

class Launch:
    def __init__(self, argv, cwd, log_path):
        self.argv, self.cwd, self.log_path = argv, cwd, log_path
        self.proc        = None
        self.returncode  = None
        self.error       = ""
        self.started_at  = 0.0
        self.finished_at = 0.0
        self.started     = asyncio.Event()
        self.done        = asyncio.Event()
        self.task        = asyncio.create_task(self._run())

    async def _run(self):
        try:
            async with _GLOBAL:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "ab") as log:
                    self.proc = await asyncio.create_subprocess_exec(
                        *self.argv, cwd=str(self.cwd),
                        env=dict(os.environ, CUDA_VISIBLE_DEVICES="0"),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=log, stderr=log,
                        start_new_session=True,
                    )
                    self.started_at = time.time()
                    self.started.set()
                    self.returncode = await self.proc.wait()
        except Exception as error:
            self.error = str(error)
        finally:
            self.finished_at = time.time()
            self.started.set()
            self.done.set()

    def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    def log_tail(self, lines: int = 12) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""


def _launch(key, argv, cwd, log_path) -> Launch:
    old = _LAUNCHES.get(key)
    if old and not old.done.is_set():
        return old
    if old and old.returncode not in (0, None):
        return old
    if old and time.time() - old.finished_at < RELAUNCH_AFTER:
        return old
    launch = Launch(argv, cwd, log_path)
    _LAUNCHES[key] = launch
    return launch


def _mtime(path) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _find_output(data, scene_id):
    for out in (data or {}).get("outputs") or []:
        if (out.get("scene_id") == scene_id
                and out.get("status") == "COMPLETE"
                and out.get("path")
                and Path(out["path"]).is_file()):
            return out
    return None


async def _cancelled(ctx: dict) -> bool:
    job_id = ctx.get("job_id")
    if not job_id:
        return False
    from job_queue import is_cancelled
    local = await db.jobs.find_one({"_id": job_id}, {"status": 1})
    return bool(is_cancelled(job_id) or (local or {}).get("status") == "cancelled")


async def _wait_for(launch: Launch, response_path: Path, scene_id: str, config, ctx: dict):
    poll, stall = max(1, config.studio_poll_seconds), config.studio_timeout_seconds
    seen, last_progress = -1, time.monotonic()
    while True:
        if await _cancelled(ctx):
            from job_queue import JobCancelled
            raise JobCancelled()
        data  = _read_json(response_path)
        found = _find_output(data, scene_id)
        if found:
            return found
        fresh = bool(data) and _mtime(response_path) >= launch.started_at - 2
        if launch.done.is_set() or (fresh and (data or {}).get("status") == "FAILED"):
            data  = _read_json(response_path)
            found = _find_output(data, scene_id)
            if found:
                return found
            errors = "; ".join(str(e)[:300] for e in ((data or {}).get("errors") or [])[:3])
            raise ProviderFailure(
                f"Studio did not produce scene {scene_id}. "
                f"{errors or launch.error or launch.log_tail() or 'No details in response.json.'}",
                code="STUDIO_FAILED",
                action=f"Check the worker log: {launch.log_path}",
            )
        progress = (
            len((data or {}).get("outputs") or []),
            _mtime(Path(response_path).parent / "progress.json"),
            _mtime(launch.log_path),
            _mtime(Path(response_path).parent.parent / "queue_progress.json"),
        )
        if progress != seen or not launch.started.is_set():
            seen, last_progress = progress, time.monotonic()
        if time.monotonic() - last_progress > stall:
            raise ProviderFailure(
                f"No Studio progress for {stall}s (scene {scene_id}). "
                "Worker left running; retry will reattach to recorded work.",
                code="POLL_TIMEOUT",
            )
        await asyncio.sleep(poll)


# ──────────────────────────────────── delivery + records ─────────────────────

async def _deliver(output: dict, out_path):
    root, _, _ = paths()
    src = Path(output["path"]).resolve()
    if root.resolve() not in src.parents:
        raise ProviderFailure(
            "Studio returned a path outside the studio root; refusing to read it.",
            code="INVALID_ARTIFACT",
        )
    data = src.read_bytes()
    if output.get("sha256") and hashlib.sha256(data).hexdigest() != output["sha256"]:
        raise ProviderFailure("Studio image checksum mismatch.", code="INVALID_ARTIFACT")
    from PIL import Image
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception as exc:
        raise ProviderFailure("Studio returned an invalid PNG.", code="INVALID_ARTIFACT") from exc
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)


async def _record(story_id, sig, scene_id, segment, status, run, revision, error="", qc=None):
    _id = f"{story_id}:{sig}:{scene_id}:{revision}"
    await db.studio_jobs.update_one({"_id": _id}, {"$set": {
        "id": _id, "job_id": f"{run}/{revision}", "story_id": story_id, "segment": segment,
        "operation": "generate_image", "status": status, "production_pass": False,
        "qc": qc or {}, "error": redact(error)[:1500], "base_url": "local-worker",
        "created_at": time.time(),
    }}, upsert=True)


@contextmanager
def locked_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def reserve_revision(base: Path, state_id: str, scene_id: str,
                     parent: str, request_id: str, feedback: str):
    if not request_id:
        raise ProviderFailure("Regeneration requires a persistent job ID.", code="INVALID_REQUEST")
    con = sqlite3.connect(str(base / "revision_reservations.sqlite"), timeout=30)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS reservations "
            "(story TEXT, scene TEXT, request TEXT, revision TEXT, parent TEXT, feedback TEXT, "
            "PRIMARY KEY(story,scene,request), UNIQUE(story,scene,revision))"
        )
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT revision,parent,feedback FROM reservations WHERE story=? AND scene=? AND request=?",
            (state_id, scene_id, request_id),
        ).fetchone()
        if row:
            if row[2] != feedback:
                raise ProviderFailure("Same regeneration job has changed comments.", code="INVALID_REQUEST")
            con.commit()
            return row[1], row[0]
        nums = [int(x[0][1:]) for x in con.execute(
            "SELECT revision FROM reservations WHERE story=? AND scene=?", (state_id, scene_id))]
        number = max([int(parent[1:])] + nums) + 1
        if number > 999:
            raise ProviderFailure("Revision limit reached; create a new run.", code="INVALID_REQUEST")
        revision = "r%03d" % number
        con.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?)",
                    (state_id, scene_id, request_id, revision, parent, feedback))
        con.commit()
        return parent, revision
    finally:
        con.close()


def _character_sheet_artifact(run_dir: Path, request: dict) -> dict:
    from PIL import Image, ImageOps, ImageDraw
    entries = []
    for key in request["characters"]:
        p = run_dir / "characters" / key / "candidate.png"
        rec = _read_json(p.parent / "asset.json") or {}
        if not p.is_file() or rec.get("sha256") != hashlib.sha256(p.read_bytes()).hexdigest():
            raise ProviderFailure(f"Character reference unavailable or changed: {key}", code="INVALID_ARTIFACT")
        entries.append((key, p))
    cols    = min(3, len(entries))
    rows    = (len(entries) + cols - 1) // cols
    canvas  = Image.new("RGB", (cols * 384, rows * 704), (236, 228, 207))
    draw    = ImageDraw.Draw(canvas)
    for idx, (key, p) in enumerate(entries):
        with Image.open(p) as im:
            pic = ImageOps.contain(im.convert("RGB"), (374, 660))
        x = idx % cols * 384
        y = idx // cols * 704
        canvas.paste(pic, (x + (384 - pic.width) // 2, y + 30))
        draw.text((x + 8, y + 8), key, fill="black")
    dest = run_dir / "storyforge_character_sheet.png"
    with locked_file(dest.with_suffix(".lock")):
        tmp = dest.with_suffix(".tmp.png")
        canvas.save(tmp)
        os.replace(tmp, dest)
    return {
        "path":      str(dest),
        "sha256":    hashlib.sha256(dest.read_bytes()).hexdigest(),
        "width":     canvas.width,
        "height":    canvas.height,
        "qc_status": "NOT_RUN",
    }


# ───────────────────────────────────── public entry point ────────────────────

async def generate_image(prompt, out_path, config, segment=None):
    ok, message = check()
    if not ok:
        raise ProviderFailure(
            f"Studio worker is not available: {message}", code="NOT_CONFIGURED",
            action="Check STUDIO_ROOT / STUDIO_PYTHON / STUDIO_BATCH_SCRIPT in backend/.env.",
        )
    root, python, script = paths()
    ctx      = generation_context.get()
    story_id = ctx.get("story_id", "")
    story    = await db.stories.find_one({"_id": story_id}) or {}
    channel  = await db.channels.find_one({"_id": story.get("channel_id")}) or {}
    prefs_doc= await db.story_engines.find_one({"_id": story_id}) or {}
    refs     = ((prefs_doc.get("studio") or {}).get("references")) or {}
    project  = f"storyforge_{_safe_id(story_id)}"
    base     = root / "projects" / project / "stills_v011"
    request  = await resolved_request(story, channel, project, refs, base)

    if not request["scenes"]:
        raise ProviderFailure("The story has no scenes to send to Studio.", code="NO_SCENES")

    auxiliary = None
    if segment is None:
        if Path(out_path).parent.name == "char":
            auxiliary = "character_sheet"
        elif "thumb" in Path(out_path).name.lower():
            auxiliary = "thumbnail"
        else:
            raise ProviderFailure(
                "Unrecognized auxiliary image request; expected character sheet or thumbnail.",
                code="INVALID_REQUEST",
            )
        scene_id = request["scenes"][0]["id"]
    else:
        if type(segment) is not int or segment < 0 or segment >= len(request["scenes"]):
            raise ProviderFailure("Segment index outside the story.", code="NO_SCENES")
        scene_id = f"S{segment + 1:03d}"

    sig            = _signature(request)
    run_dir        = base / f"run_{sig}"
    record_scene_id = ("CHARACTERS" if auxiliary == "character_sheet"
                       else "THUMBNAIL" if auxiliary
                       else scene_id)

    request_path = base / f"request_{sig}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(request_path.with_suffix(".lock")):
        if request_path.exists() and json.loads(request_path.read_text()) != request:
            raise ProviderFailure("Request content changed inside an existing run.", code="INVALID_REQUEST")
        if not request_path.exists():
            request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")

    state_id = f"{story_id}:{sig}"
    state    = await db.studio_runs.find_one({"_id": state_id}) or {}
    revision = (state.get("scenes") or {}).get(scene_id, "r001")
    regenerate   = segment is not None and ctx.get("regenerate") == segment
    batch_response = run_dir / "batch_r001" / "response.json"
    batch_argv     = [str(python), str(script), "--story", str(request_path), "--output", str(run_dir)]
    current = batch_response if revision == "r001" else run_dir / f"{scene_id}_{revision}" / "response.json"
    if regenerate and not _find_output(_read_json(current), scene_id):
        regenerate = False

    if regenerate:
        feedback = (ctx.get("feedback") or "").strip() or \
            "Regenerate this scene with a fresh composition. Keep the same characters, costumes, art style and setting."
        parent, revision = reserve_revision(
            base, state_id, scene_id, revision, str(ctx.get("job_id") or ""), feedback)
        feedback_path = run_dir / f"feedback_{scene_id}_{revision}.txt"
        run_dir.mkdir(parents=True, exist_ok=True)
        with locked_file(feedback_path.with_suffix(".lock")):
            if feedback_path.exists() and feedback_path.read_text() != feedback:
                raise ProviderFailure("Reserved revision feedback changed.", code="INVALID_REQUEST")
            if not feedback_path.exists():
                feedback_path.write_text(feedback, encoding="utf-8")
        argv = [str(python), str(script), "--story", str(request_path), "--output", str(run_dir),
                "--scene", scene_id, "--revision", revision, "--parent-revision", parent,
                "--feedback-file", str(feedback_path)]
        response_path = run_dir / f"{scene_id}_{revision}" / "response.json"
        launch_key    = (story_id, sig, scene_id, revision)
    else:
        argv, response_path, launch_key = batch_argv, batch_response, (story_id, sig)
        if revision != "r001":
            latest = run_dir / f"{scene_id}_{revision}" / "response.json"
            if _find_output(_read_json(latest), scene_id):
                response_path = latest
            else:
                revision = "r001"

    output = _find_output(_read_json(response_path), scene_id)
    try:
        if output is None:
            await _record(story_id, sig, record_scene_id, segment, "running", run_dir.name, revision)
            launch = _launch(launch_key, argv, root, run_dir / "storyforge_worker.log")
            output = await _wait_for(launch, response_path, scene_id, config, ctx)
        if auxiliary == "character_sheet":
            output = _character_sheet_artifact(run_dir, request)
        await _deliver(output, out_path)
    except Exception as error:
        await _record(story_id, sig, record_scene_id, segment, "failed", run_dir.name, revision,
                      error=str(error))
        raise

    await db.studio_runs.update_one(
        {"_id": state_id},
        {"$max": {f"scenes.{scene_id}": revision}, "$set": {"story_id": story_id}},
        upsert=True,
    )
    await _record(story_id, sig, record_scene_id, segment, "succeeded", run_dir.name, revision,
                  qc={"qc_status": output.get("qc_status", ""),
                      "sha256":    output.get("sha256", ""),
                      "size":      f"{output.get('width','?')}x{output.get('height','?')}"})
    return {
        "provider":           "studio",
        "job_id":             f"{run_dir.name}/{scene_id}/{revision}",
        "production_pass":    False,
        "generation_complete": True,
        "acceptance_owner":   "storyforge",
    }


async def self_test(timeout: int = 60) -> str:
    root, python, script = paths()
    proc = await asyncio.create_subprocess_exec(
        str(python), str(script), "--help", cwd=str(root),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProviderFailure(f"batch.py --help timed out after {timeout}s", code="POLL_TIMEOUT")
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise ProviderFailure(f"batch.py --help exited {proc.returncode}: {text[-400:]}", code="WORKER_ERROR")
    return text[:1500]
