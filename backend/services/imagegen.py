"""AI image providers: Gemini (user key) -> OpenAI -> Stability -> procedural fallback."""
import asyncio
import base64
import os
from pathlib import Path

from services import gemini
from services.llm import stability_key, _stability_image
from services.ocr import MEDIA_ROOT

IMAGE_PRICE = 0.03

# epoch until which cloud image providers are skipped (set after a full-chain failure)
_IMAGE_DEAD_UNTIL = 0.0


def _ref_bytes(ref_image):
    if ref_image and Path(ref_image).exists():
        return Path(ref_image).read_bytes()
    return None


async def generate_image(prompt: str, out_path: Path, ref_image: Path = None, session: str = "img",
                         require_reference: bool = False):
    """AI image with hard 35s caps per provider; after one full-chain failure the cloud providers
    are skipped for 10 minutes (avoids retry storms that make renders crawl)."""
    global _IMAGE_DEAD_UNTIL
    import time

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ref = _ref_bytes(ref_image)

    if require_reference and not ref:
        raise RuntimeError("a visual reference image is required for continuity-locked generation")

    if time.time() < _IMAGE_DEAD_UNTIL:
        if require_reference:
            raise RuntimeError("reference-aware image providers are temporarily unavailable")
        await asyncio.to_thread(procedural_frame, prompt, out_path)
        return False

    if gemini.gemini_key() and not gemini.circuit_open():
        try:
            data = await asyncio.wait_for(gemini.gen_image(prompt, ref), timeout=35)
            out_path.write_bytes(data)
            return True
        except Exception as e:
            print(f"[media] gemini image failed: {str(e)[:120]}", flush=True)

    # OpenAI/Stability and local fallbacks in this adapter do not consume the reference image.
    # Failing explicitly avoids silently replacing locked characters with lookalikes.
    if require_reference:
        raise RuntimeError("reference-aware image generation failed; retry instead of accepting character drift")

    ok = gemini.openai_key()
    if ok:
        try:
            await asyncio.wait_for(_gen_openai_image(ok, prompt, out_path), timeout=35)
            return True
        except Exception as e:
            print(f"[media] openai image failed: {str(e)[:120]}", flush=True)

    if stability_key():
        try:
            await asyncio.wait_for(
                _stability_image(prompt, out_path), timeout=60)
            return True
        except Exception as e:
            print(f"[media] stability image failed: {str(e)[:120]}", flush=True)

    _IMAGE_DEAD_UNTIL = time.time() + 600  # all cloud providers dead — stop hammering for 10 min
    await asyncio.to_thread(procedural_frame, prompt, out_path)
    return False


async def _gen_openai_image(key, prompt, out_path):
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, timeout=240)
    resp = await client.images.generate(model="gpt-image-1", prompt=prompt, quality="medium", size="1024x1536")
    if not resp.data or not resp.data[0].b64_json:
        raise RuntimeError("no images returned")
    out_path.write_bytes(base64.b64decode(resp.data[0].b64_json))


# ---------- procedural fallback frames (free, no API) ----------
MOOD_PALETTES = {
    "devotional": [(38, 20, 4), (120, 63, 8), (245, 158, 11)],
    "suspense": [(6, 8, 24), (30, 27, 75), (99, 102, 241)],
    "horror": [(4, 6, 10), (40, 12, 24), (127, 29, 29)],
    "moral": [(10, 30, 28), (19, 78, 74), (45, 212, 191)],
    "action": [(30, 8, 4), (127, 29, 29), (239, 68, 68)],
    "sad": [(12, 16, 32), (30, 41, 59), (148, 163, 184)],
    "happy": [(30, 24, 4), (133, 77, 14), (251, 191, 36)],
}


def procedural_frame(prompt: str, out_path: Path):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    text = str(prompt).lower()
    mood = next((m for m in MOOD_PALETTES if m in text), "devotional")
    c1, c2, c3 = MOOD_PALETTES[mood]
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H), c1)
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / H
        r = int(c1[0] + (c2[0] - c1[0]) * k)
        g = int(c1[1] + (c2[1] - c1[1]) * k)
        b = int(c1[2] + (c2[2] - c1[2]) * k)
        d.line([(0, y), (W, y)], fill=(r, g, b))
    # radiant mandala motif
    cx, cy = W // 2, int(H * 0.42)
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    dg = ImageDraw.Draw(glow)
    import math
    for ring in range(9, 0, -1):
        rad = 90 + ring * 68
        col = tuple(int(c * (1 - ring / 14)) for c in c3)
        dg.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=col, width=10)
        for i in range(24):
            a = math.pi * 2 * i / 24 + ring * 0.13
            dg.line([(cx + rad * math.cos(a), cy + rad * math.sin(a)),
                     (cx + (rad + 46) * math.cos(a), cy + (rad + 46) * math.sin(a))], fill=col, width=6)
    glow = glow.filter(ImageFilter.GaussianBlur(6))
    img = Image.blend(img, Image.composite(glow, img, glow.convert("L").point(lambda v: min(255, v * 2))), 0.55)
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle([0, 0, W, H], fill=(0, 0, 0, 60))
    d.ellipse([cx - 190, cy - 190, cx + 190, cy + 190], fill=c3 + (230,))
    d.ellipse([cx - 150, cy - 150, cx + 150, cy + 150], fill=c1 + (255,))
    f = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 40)
    words = str(prompt).split()[:6]
    d.text((cx, cy), " ".join(words[:3]), font=f, fill=(20, 12, 2), anchor="mm")
    d.text((cx, cy + 54), " ".join(words[3:6]), font=f, fill=(20, 12, 2), anchor="mm")
    # film grain + vignette
    import numpy as np
    arr = np.array(img).astype(np.float64)
    rng = np.random.default_rng(3)
    arr += rng.integers(-9, 9, arr.shape)
    yy, xx = np.mgrid[0:H, 0:W]
    vig = np.clip(1 - ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / ((W / 2) ** 2) / 2.4, 0.25, 1)
    arr = arr * vig[:, :, None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    img.save(out_path)
    return out_path
