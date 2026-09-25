"""Iteration 14: verify Devanagari/Bengali font fix and fit_audio_to_budget behavior."""
import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from PIL import ImageFont

from services import media


def test_devanagari_font_is_real_truetype():
    text = "घायल पक्षी को बचाया। नदी बहने लगी।"
    font = media.font_for_text(text, 52)
    # Must be a real FreeType font, not the bitmap default
    assert isinstance(font, ImageFont.FreeTypeFont), "Devanagari fell back to bitmap load_default() → boxes"
    path = getattr(font, "path", "")
    assert path, "font.path is empty; not a real TTF"
    assert Path(path).exists(), f"font path missing: {path}"
    # Must be an Indic-capable font (Noto Devanagari, or fc-match resolved something for hi)
    print(f"[dev] font.path = {path}")


def test_raqm_enabled_and_layout_engine():
    assert media._RAQM is True, "RAQM/HarfBuzz not available in Pillow — Indic shaping will break"
    text = "घायल पक्षी"
    font = media.font_for_text(text, 52)
    # ImageFont.Layout.RAQM == 1
    assert int(font.layout_engine) == int(ImageFont.Layout.RAQM), \
        f"layout_engine={font.layout_engine}, expected RAQM"


def test_bengali_font_is_real_truetype():
    text = "আমি বাংলায় গান গাই"  # Bengali sample
    font = media.font_for_text(text, 52)
    assert isinstance(font, ImageFont.FreeTypeFont), "Bengali fell back to bitmap default"
    path = getattr(font, "path", "")
    assert path and Path(path).exists()
    print(f"[ben] font.path = {path}")


def test_render_caption_devanagari_writes_png(tmp_path):
    out = tmp_path / "cap.png"
    media.render_caption("घायल पक्षी को बचाया। नदी बहने लगी।", out)
    assert out.exists(), "caption PNG not written"
    size = out.stat().st_size
    assert size > 2048, f"caption PNG too small ({size} bytes) — likely blank/boxes"
    print(f"[cap] png size = {size} bytes")


def _make_sine_mp3(path: Path, seconds: float):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "2", str(path)],
        capture_output=True, check=True,
    )


def test_fit_audio_speeds_up_over_budget(tmp_path):
    src = tmp_path / "long.mp3"
    _make_sine_mp3(src, 6.0)
    orig = media.ffprobe_duration(src)
    assert 5.8 <= orig <= 6.2, f"seed duration off: {orig}"

    new_dur = asyncio.run(media.fit_audio_to_budget(src, 3.0))
    # Speeds up (>0.8s shorter), but capped at 1.35x → cannot go below ~4.44s
    assert new_dur < orig - 0.8, f"audio was not sped up enough ({orig:.2f}→{new_dur:.2f})"
    assert new_dur >= 4.0, f"audio dropped below 1.35x cap floor ({new_dur:.2f})"
    print(f"[fit-over] {orig:.2f}s → {new_dur:.2f}s (budget 3.0)")


def test_fit_audio_under_budget_untouched(tmp_path):
    src = tmp_path / "short.mp3"
    _make_sine_mp3(src, 3.0)
    orig = media.ffprobe_duration(src)
    new_dur = asyncio.run(media.fit_audio_to_budget(src, 9.0))
    assert abs(new_dur - orig) < 0.1, f"under-budget audio was modified ({orig:.2f}→{new_dur:.2f})"
    print(f"[fit-under] {orig:.2f}s → {new_dur:.2f}s (budget 9.0)")
