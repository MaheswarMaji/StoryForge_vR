import asyncio
import base64
import math
import os
import subprocess
import uuid
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, features

from services import gemini
from services.ocr import MEDIA_ROOT

SR = 44100
FPS = 30
W, H = 1080, 1920
TTS_PRICE_PER_CHAR = 15e-6  # tts-1 $15 / 1M chars
IMAGE_PRICE = 0.03

_FONT_CANDIDATES = {
    "dev": ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/local/share/fonts/NotoSansDevanagari-Bold.ttf"],
    "ben": ["/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansBengaliUI-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansBengali-Bold.ttf"],
    "lat": ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}

# Complex scripts (Devanagari/Bengali) need HarfBuzz shaping — force RAQM when Pillow
# was built with it; the BASIC engine renders matras/conjuncts as broken glyphs ("boxes").
_RAQM = features.check("raqm")
_LAYOUT = ImageFont.Layout.RAQM if _RAQM else ImageFont.Layout.BASIC
_FC_LANG = {"dev": "hi", "ben": "bn", "lat": "en"}
_FC_CACHE: dict = {}


def _fc_match(lang: str) -> str:
    """Locate a font for a language via fontconfig — portable across distros/paths."""
    if lang in _FC_CACHE:
        return _FC_CACHE[lang]
    path = ""
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", f":lang={lang}"],
                             capture_output=True, text=True, timeout=5)
        cand = out.stdout.strip()
        if cand and Path(cand).exists():
            path = cand
    except Exception:
        path = ""
    _FC_CACHE[lang] = path
    return path


def _img_tts_keys():
    from services.llm import candidate_keys
    return candidate_keys()


def font_for_text(text: str, size: int) -> ImageFont.FreeTypeFont:
    if any("\u0900" <= c <= "\u097F" for c in text):
        kind = "dev"
    elif any("\u0980" <= c <= "\u09FF" for c in text):
        kind = "ben"
    else:
        kind = "lat"
    for p in _FONT_CANDIDATES[kind]:
        if Path(p).exists():
            return ImageFont.truetype(p, size, layout_engine=_LAYOUT)
    # Portable fallback: ask fontconfig for a font that covers this language.
    fc = _fc_match(_FC_LANG[kind])
    if fc:
        return ImageFont.truetype(fc, size, layout_engine=_LAYOUT)
    if kind != "lat":
        # Indic text with load_default() renders as boxes — surface the missing dependency.
        print(f"[media] WARNING: no {kind} font found (raqm={_RAQM}); "
              "install fonts-noto + a raqm-enabled Pillow to render Hindi/Bengali captions.",
              flush=True)
        for p in _FONT_CANDIDATES["lat"]:
            if Path(p).exists():
                return ImageFont.truetype(p, size, layout_engine=_LAYOUT)
    return ImageFont.load_default()


def tts_cost(text: str) -> float:
    return len(text) * TTS_PRICE_PER_CHAR


GEMINI_TO_OPENAI_VOICE = {
    "charon": "onyx", "kore": "coral", "puck": "nova", "leda": "fable",
    "aoede": "shimmer", "fenrir": "ash", "zephyr": "sage", "orbit": "echo",
}
OPENAI_VOICES = {"alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"}


async def synthesize_voice(text: str, voice: str, speed: float, out_path: Path,
                           lang_hint: str = None, direction: str = None,
                           expressive: bool = False) -> float:
    """Local-first TTS; Gemini is permitted only by an explicit gemini: voice."""
    from services import router

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not lang_hint:
        v = (voice or "").lower()
        lang_hint = {"charon": "hi", "kore": "bn", "leda": "bn", "aoede": "hi"}.get(
            v.split(":", 1)[-1], "hi" if v.startswith("gemini:") else "en")
    clean = " ".join(str(text).split())[:3500]
    res = await router.tts(clean, voice or "", lang_hint, out_path,
                           direction=direction, expressive=expressive, speed=speed)
    return res["duration"]


async def openai_tts(text: str, voice_spec: str, out_path: Path) -> float:
    from openai import AsyncOpenAI
    clean = " ".join(str(text).split())[:3500]
    last_err = None
    for key in _img_tts_keys():
        try:
            vo = voice_spec
            if vo.startswith("gemini:"):
                vo = GEMINI_TO_OPENAI_VOICE.get(vo.split(":", 1)[1].lower(), "onyx")
            if vo not in OPENAI_VOICES:
                vo = "onyx"
            client = AsyncOpenAI(api_key=key, timeout=120)
            audio = await client.audio.speech.create(
                model="tts-1", voice=vo, input=clean,
                speed=max(0.25, min(4.0, 1.0)), response_format="mp3")
            out_path.write_bytes(audio.content)
            return ffprobe_duration(out_path)
        except Exception as e:
            last_err = e
            if "budget" in str(e).lower() or "credit" in str(e).lower() or "429" in str(e):
                continue
    raise RuntimeError(f"TTS failed after retries: {str(last_err)[:200]}")


async def gtts_tts(text: str, lang: str, out_path: Path) -> float:
    from gtts import gTTS

    def _gen():
        gTTS(text=text[:2900], lang=lang if lang in ("hi", "bn", "en") else "en",
             slow=False).save(str(out_path))

    await asyncio.to_thread(_gen)
    return ffprobe_duration(out_path)


async def compose_ai_clip(ai_video: Path, narration: Path, caption: Path, out: Path, dur: float,
                          hook_card: Path = None):
    """Trim/loop a video (AI or user-uploaded) to the narration length, burn captions and mix voice."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fade_out = max(0.0, dur - 0.35)
    fc = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"fps={FPS},eq=saturation=1.1:contrast=1.04,"
        f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.2f}:d=0.32[bg]"
    )
    inputs = ["-stream_loop", "-1", "-i", str(ai_video), "-i", str(narration)]
    idx = 2
    if caption and Path(caption).exists():
        inputs += ["-i", str(caption)]
        fc += f";[{idx}:v]scale=960:-1[cap];[bg][cap]overlay=(W-w)/2:H-h-250[v]"
        idx += 1
    else:
        fc += ";[bg]null[v]"
    vmap = "[v]"
    if hook_card and Path(hook_card).exists():
        fc += f";[{idx}:v]scale={W}:-1[hook];[v][hook]overlay=(W-w)/2:150:enable='lte(t,2.6)'[v2]"
        vmap = "[v2]"
        inputs += ["-i", str(hook_card)]
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *inputs,
        "-filter_complex", fc, "-map", vmap, "-map", "1:a", "-af", "apad",
        "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        str(out) + ".tmp.mp4", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ai-clip failed: {err.decode()[-600:]}")
    os.replace(str(out) + ".tmp.mp4", str(out))


async def fit_audio_to_budget(src: Path, budget: float, max_tempo: float = 1.35) -> float:
    """Gently speed up narration (in place) so a segment fits its time budget.

    Only speeds UP (never slows), capped at max_tempo so the voice stays natural.
    Returns the resulting duration. Keeps total video length close to the target
    instead of overrunning (the 60s→78s drift).
    """
    cur = ffprobe_duration(src)
    if budget <= 0 or cur <= budget + 0.35:
        return cur
    tempo = min(max_tempo, cur / budget)
    if tempo <= 1.01:
        return cur
    tmp = Path(f"{src}.fit{src.suffix or '.mp3'}")
    codec = ["-c:a", "libmp3lame", "-q:a", "2"] if src.suffix == ".mp3" else ["-c:a", "pcm_s16le"]
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src), "-af", f"atempo={tempo:.3f}",
            "-ar", "44100", "-ac", "2", *codec, str(tmp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            print(f"[media] fit_audio skipped: {err.decode()[-140:]}", flush=True)
            tmp.unlink(missing_ok=True)
            return cur
        os.replace(tmp, src)
        return ffprobe_duration(src)
    except Exception:
        tmp.unlink(missing_ok=True)
        return cur


def ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return 10.0


async def generate_image(prompt: str, out_path: Path, ref_image: Path = None, session: str = "img",
                         require_reference: bool = False):
    """Delegates to the provider chain in imagegen. Returns True if an AI image was generated."""
    from services.imagegen import generate_image as _gen
    return await _gen(prompt, out_path, ref_image=ref_image, session=session,
                      require_reference=require_reference)


def render_caption(text: str, out_png: Path, width: int = 1080, size: int = 52):
    font = font_for_text(text, size)
    max_w = width - 140
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    lines = lines[:6]

    lh = int(size * 1.4)
    h = lh * len(lines) + 56
    canvas = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([24, 8, width - 24, h - 8], radius=20, fill=(0, 0, 0, 110))
    y = 28
    for ln in lines:
        d.text((width // 2, y), ln, font=font, fill=(255, 255, 255, 255),
               anchor="ma", stroke_width=7, stroke_fill=(0, 0, 0, 255))
        y += lh
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return out_png


MOODS = {
    "devotional": {"root": 196.0, "scale": [0, 2, 3, 5, 7, 10], "bpm": 66, "drums": "soft", "drone": True},
    "suspense": {"root": 110.0, "scale": [0, 1, 3, 5, 6], "bpm": 74, "drums": "pulse", "drone": True},
    "horror": {"root": 65.4, "scale": [0, 1, 4, 6], "bpm": 80, "drums": "creak", "drone": True},
    "moral": {"root": 261.6, "scale": [0, 2, 4, 5, 7, 9], "bpm": 78, "drums": "soft", "drone": True},
    "action": {"root": 146.8, "scale": [0, 2, 3, 5, 7, 8], "bpm": 132, "drums": "dhol", "drone": False},
    "sad": {"root": 220.0, "scale": [0, 2, 3, 5, 7, 8], "bpm": 56, "drums": None, "drone": True},
    "happy": {"root": 293.7, "scale": [0, 2, 4, 7, 9], "bpm": 112, "drums": "soft", "drone": False},
}


def _tone(freq, dur, vol=0.5, harmonics=(1.0, 0.35, 0.12), decay=None, attack=0.01):
    t = np.arange(int(SR * dur)) / SR
    w = np.zeros_like(t)
    for i, amp in enumerate(harmonics):
        w += amp * np.sin(2 * np.pi * freq * (i + 1) * t + 0.1 * i)
    env = np.minimum(1.0, t / max(attack, 1e-3))
    if decay:
        env = env * np.exp(-t / decay)
    return w * env * vol


def _noise_burst(dur, vol=0.3, decay=0.06):
    rng = np.random.default_rng(7)
    t = np.arange(int(SR * dur)) / SR
    w = rng.uniform(-1, 1, len(t)) * np.exp(-t / decay) * vol
    return np.convolve(w, np.ones(24) / 24, mode="same")


def synth_music(mood: str, out_path: Path, seconds: int = 40):
    cfg = MOODS.get(mood, MOODS["devotional"])
    beat = 60.0 / cfg["bpm"]
    total = int(SR * seconds)
    mix = np.zeros(total)
    root = cfg["root"]
    scale = cfg["scale"]

    if cfg["drone"]:
        t = np.arange(total) / SR
        lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.08 * t)
        drone = (np.sin(2 * np.pi * root * t) + 0.6 * np.sin(2 * np.pi * root * 1.5 * t)
                 + 0.25 * np.sin(2 * np.pi * root * 2.002 * t))
        mix += drone * 0.045 * (0.6 + 0.4 * lfo)

    step = int(SR * beat / 2)
    pos = 0
    k = 0
    while pos < total:
        deg = scale[(k * 3 + (1 if k % 3 else 0)) % len(scale)]
        oct = 2 if (k % 4 == 2) else 1
        freq = root * oct * (2 ** (deg / 12))
        note = _tone(freq, min(beat * 2.2, 2.5), vol=0.16, decay=beat * 1.4)
        end = min(pos + len(note), total)
        mix[pos:end] += note[: end - pos]
        if cfg["drums"] == "dhol" and k % 2 == 0:
            th = _tone(55, 0.3, vol=0.5, decay=0.09) + _noise_burst(0.12, vol=0.18)
            end2 = min(pos + len(th), total)
            mix[pos:end2] += th[: end2 - pos]
        elif cfg["drums"] == "pulse" and k % 4 == 0:
            th = _tone(70, 0.4, vol=0.3, decay=0.15)
            end2 = min(pos + len(th), total)
            mix[pos:end2] += th[: end2 - pos]
        elif cfg["drums"] == "creak" and k % 8 == 5:
            cr = _noise_burst(0.5, vol=0.10, decay=0.18)
            end2 = min(pos + len(cr), total)
            mix[pos:end2] += cr[: end2 - pos]
        elif cfg["drums"] == "soft" and k % 4 == 2:
            tk = _noise_burst(0.06, vol=0.05)
            end2 = min(pos + len(tk), total)
            mix[pos:end2] += tk[: end2 - pos]
        pos += step
        k += 1

    peak = np.max(np.abs(mix)) or 1.0
    mix = mix / peak * 0.85
    stereo = np.stack([mix, mix], axis=1)
    pcm = (stereo * 32767).astype(np.int16)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm.tobytes())
    return out_path


def render_hook_card(text: str, out_png: Path):
    """Bold hook title card overlaid on the first seconds (clarity + curiosity)."""
    font = font_for_text(text, 64)
    max_w = W - 160
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    lines = lines[:4]
    lh = int(64 * 1.3)
    h = lh * len(lines) + 60
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([20, 8, W - 20, h - 8], radius=26, fill=(9, 10, 15, 190))
    y = 30
    for ln in lines:
        d.text((W // 2, y), ln, font=font, fill=(251, 191, 36, 255), anchor="ma",
               stroke_width=8, stroke_fill=(0, 0, 0, 255))
        y += lh
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return out_png


def _kenburns_expr(camera: str, frames: int, rate_mult: float = 1.0):
    rate = 0.14 * max(0.5, min(2.5, rate_mult)) / max(frames, 1)
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    if camera == "zoom_out":
        return f"max(1.15-{rate}*on,1.0)", cx, cy
    if camera == "pan_left":
        return "1.12", f"(iw-iw/zoom)*on/{frames}", cy
    if camera == "pan_right":
        return "1.12", f"(iw-iw/zoom)*(1-on/{frames})", cy
    if camera == "static":
        return "1.06", cx, cy
    return f"1+{rate}*on", cx, cy  # zoom_in


async def make_segment_clip(frame: Path, narration: Path, caption: Path, out: Path,
                            camera: str, dur: float, rate_mult: float = 1.0,
                            hook_card: Path = None, silence_pad: float = 0.0):
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = max(30, round(dur * FPS))
    z, x, y = _kenburns_expr(camera or "zoom_in", frames, rate_mult)
    fade_out = max(0.0, dur - 0.35)
    af = "apad" if silence_pad <= 0 else f"adelay={int(silence_pad * 1000)}|{int(silence_pad * 1000)},apad"
    fc = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
        f"eq=saturation=1.14:contrast=1.05,vignette=PI/5,"
        f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.2f}:d=0.32[bg]"
    )
    inputs = ["-i", str(frame)]
    if narration and Path(narration).exists():
        inputs += ["-i", str(narration)]
    else:  # silent slide — synthetic silent track keeps the concat muxer happy
        inputs += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={SR}"]
    idx = 2
    if caption and Path(caption).exists():
        inputs += ["-i", str(caption)]
        fc += f";[{idx}:v]scale=960:-1[cap];[bg][cap]overlay=(W-w)/2:H-h-250[v]"
        idx += 1
    else:
        fc += ";[bg]null[v]"
    vmap = "[v]"
    if hook_card and Path(hook_card).exists():
        fc += f";[{idx}:v]scale={W}:-1[hook];[v][hook]overlay=(W-w)/2:150:enable='lte(t,2.6)'[v2]"
        vmap = "[v2]"
        inputs += ["-i", str(hook_card)]
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *inputs,
        "-filter_complex", fc, "-map", vmap, "-map", "1:a", "-af", af,
        "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        str(out) + ".tmp.mp4", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg clip failed: {err.decode()[-600:]}")
    os.replace(str(out) + ".tmp.mp4", str(out))


async def run_ffmpeg(*args):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode()[-600:]}")


async def concat_clips(clips, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    inputs, parts = [], []
    n = len(clips)
    for c in clips:
        inputs += ["-i", str(c)]
    for i in range(n):
        parts.append(f"[{i}:v][{i}:a]")
    fc = "".join(parts) + f"concat=n={n}:v=1:a=1[v][a]"
    await run_ffmpeg("ffmpeg", "-y", *inputs, "-filter_complex", fc,
                     "-map", "[v]", "-map", "[a]",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                     str(out))


def synth_ambience(mood: str, out_path: Path, seconds: int = 40):
    """Low-level ambience bed (air/room tone) per mood — mixed quietly under music."""
    rng = np.random.default_rng(11)
    total = int(SR * seconds)
    t = np.arange(total) / SR
    noise = rng.uniform(-1, 1, total)
    kernel = np.ones(220) / 220
    noise = np.convolve(noise, kernel, mode="same")
    lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.05 * t)
    drone_f = {"devotional": 196, "suspense": 98, "horror": 55, "moral": 261.6,
               "action": 110, "sad": 165, "happy": 220}.get(mood, 110)
    tone = 0.15 * np.sin(2 * np.pi * drone_f * t) * lfo
    bed = (noise * 0.5 + tone) * 0.35
    pcm = (np.stack([bed, bed], axis=1) * 32767 * 0.5).astype(np.int16)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm.tobytes())
    return out_path


async def mix_music(video: Path, music: Path, out: Path, total: float, volume: float = 0.16,
                    ambience: Path = None):
    out.parent.mkdir(parents=True, exist_ok=True)
    fade_start = max(0.0, total - 2.5)
    if ambience and Path(ambience).exists():
        fc = (
            f"[2:a]volume=0.06[a1];"
            f"[1:a]volume={volume},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start:.2f}:d=2.4[m];"
            f"[0:a][m][a1]amix=inputs=3:duration=first:normalize=0[a]"
        )
        await run_ffmpeg(
            "ffmpeg", "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(music),
            "-stream_loop", "-1", "-i", str(ambience),
            "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", str(out))
    else:
        fc = (
            f"[1:a]volume={volume},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_start:.2f}:d=2.4[m];"
            f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]"
        )
        await run_ffmpeg(
            "ffmpeg", "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(music),
            "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", str(out))


def render_endcard(channel_name: str, cta: str, out_png: Path):
    img = Image.new("RGB", (W, H), (9, 10, 15))
    d = ImageDraw.Draw(img)
    for yy in range(H):
        k = yy / H
        d.line([(0, yy), (W, yy)], fill=(int(9 + 24 * k), int(10 + 18 * k), int(15 + 40 * k)))
    d.rounded_rectangle([70, 620, W - 70, H - 620], radius=48, outline=(245, 158, 11), width=6)
    serif = None
    for p in ["/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]:
        if Path(p).exists():
            serif = ImageFont.truetype(p, 74)
            break
    body = font_for_text(channel_name + cta, 44)
    d.text((W // 2, 800), channel_name, font=serif or body, fill=(251, 191, 36), anchor="mm")
    d.text((W // 2, 930), "─" * 14, font=body, fill=(245, 158, 11, 120), anchor="mm")
    d.text((W // 2, 1040), cta[:90], font=body, fill=(248, 250, 252), anchor="mm")
    d.rounded_rectangle([W // 2 - 240, 1220, W // 2 + 240, 1340], radius=60, fill=(245, 158, 11))
    d.text((W // 2, 1278), "SUBSCRIBE", font=serif or body, fill=(9, 10, 15), anchor="mm")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


async def make_endcard_clip(img_png: Path, out: Path, seconds: float = 3.2):
    await run_ffmpeg(
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img_png),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{seconds:.2f}", "-vf",
        f"fade=t=in:st=0:d=0.3,fade=t=out:st={max(0, seconds - 0.5):.2f}:d=0.45",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-r", str(FPS), str(out))


def overlay_title_on_image(base_img: Path, title: str, subtitle: str, out_path: Path):
    img = Image.open(base_img).convert("RGB").resize((W, H))
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, W, 420], fill=(0, 0, 0, 130))
    f_sub = font_for_text(subtitle, 40)
    f_title = font_for_text(title, 88)
    d.text((W // 2, 90), subtitle[:40], font=f_sub, fill=(251, 191, 36), anchor="ma",
           stroke_width=5, stroke_fill=(0, 0, 0))
    max_w = W - 160
    lines, cur = [], ""
    for word in title.split():
        trial = (cur + " " + word).strip()
        if f_title.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    y = 160
    for ln in lines[:3]:
        d.text((W // 2, y), ln, font=f_title, fill=(255, 255, 255), anchor="ma",
               stroke_width=9, stroke_fill=(0, 0, 0))
        y += int(88 * 1.25)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, quality=90)
    return out_path


# ---------- expressive narration helpers (stitch + humanize) ----------
HUMANIZE_TEMPO = {"slow": 0.94, "medium": 1.0, "fast": 1.06}
ECHO_EMOTIONS = ("sad", "suspense", "horror", "mystery", "ghost", "melancholy", "eerie")


def _has_audio_stream(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


async def stitch_video_clip(src: Path, out: Path, dur: float):
    """User-uploaded clip → 9:16 normalized, original audio kept (or silence), looped/trimmed to dur."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fade_out = max(0.0, dur - 0.35)
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
          f"eq=saturation=1.08:contrast=1.03,"
          f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.2f}:d=0.32")
    args = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src)]
    if _has_audio_stream(src):
        args += ["-vf", vf, "-af", "apad", "-map", "0:v", "-map", "0:a"]
    else:
        args += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={SR}",
                 "-shortest", "-vf", vf, "-map", "0:v", "-map", "1:a"]
    args += ["-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
             str(out) + ".tmp.mp4"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg stitch clip failed: {err.decode()[-600:]}")
    os.replace(str(out) + ".tmp.mp4", str(out))


async def humanize_audio(src: Path, dst: Path, direction: str = "", speed: float = 1.0) -> float:
    """Prosody post-processing so free/local TTS stops sounding monotonic:
    per-emotion tempo, gentle echo for suspense/sad moods, dynamic-range compression."""
    d = (direction or "").lower()
    pace = next((p for p in ("slow", "medium", "fast") if f"{p} pace" in d), "medium")
    tempo = HUMANIZE_TEMPO[pace] * max(0.5, min(2.0, speed or 1.0))
    filters = [f"atempo={max(0.5, min(2.0, tempo)):.3f}",
               "acompressor=threshold=-26dB:ratio=3:attack=8:release=180:makeup=2"]
    if any(e in d for e in ECHO_EMOTIONS):
        filters.append("aecho=0.7:0.28:45:0.22")
    filters.append("alimiter=limit=0.97")
    tmp = Path(f"{dst}.hum{dst.suffix or '.wav'}")
    try:
        codec = ["-c:a", "libmp3lame", "-q:a", "2"] if dst.suffix == ".mp3" else ["-c:a", "pcm_s16le"]
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src), "-af", ",".join(filters),
            "-ar", "44100", "-ac", "2", *codec, str(tmp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            print(f"[media] humanize skipped: {err.decode()[-140:]}", flush=True)
            tmp.unlink(missing_ok=True)
            return ffprobe_duration(src)
        os.replace(tmp, dst)
        return ffprobe_duration(dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        return ffprobe_duration(src)

