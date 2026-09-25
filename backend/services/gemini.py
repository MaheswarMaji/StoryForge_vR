import asyncio
import base64
import os
import re
import time
import wave

import httpx

from services.llm import parse_json

BASE = "https://generativelanguage.googleapis.com/v1beta"
TEXT_MODELS = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest"]
IMAGE_MODELS = ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]
TTS_MODEL = "gemini-2.5-flash-preview-tts"
RATE_PER_MIN = 8  # conservative free-tier budget shared by text+tts+image

_gate = asyncio.Lock()
_last_call = 0.0
MIN_INTERVAL = 8.0  # serialize all Gemini generate-content calls (~7.5 req/min)
_circuit = {"open_until": 0.0, "fails": 0}


async def _respect_rate():
    global _last_call
    async with _gate:
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.time()


def circuit_open():
    return time.time() < _circuit["open_until"]


def _record_429():
    _circuit["fails"] += 1
    if _circuit["fails"] >= 3:
        _circuit["open_until"] = time.time() + 600  # pause all gemini calls for 10 min
        print("[gemini] circuit OPEN — quota exhausted, pausing 10 min", flush=True)


def _record_ok():
    _circuit["fails"] = 0
    _circuit["open_until"] = 0.0


def gemini_key():
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def openai_key():
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def _retry_wait(text, attempt):
    m = re.search(r"retry in\s*([\d.]+)s", text)
    return float(m.group(1)) + 2 if m else 18.0 + 8 * attempt


async def chat_text(prompt: str, system: str = None) -> str:
    if circuit_open():
        raise RuntimeError("gemini quota circuit open — try again in a few minutes")
    last = None
    for model in TEXT_MODELS:
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 8192},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        async with httpx.AsyncClient(timeout=180) as client:
            for attempt in range(2):
                await _respect_rate()
                r = await client.post(f"{BASE}/models/{model}:generateContent?key={gemini_key()}", json=body)
                if r.status_code == 200:
                    _record_ok()
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    return "".join(p.get("text", "") for p in parts)
                last = f"{model}: {r.status_code} {r.text[:140]}"
                if r.status_code == 429:
                    _record_429()
                    if circuit_open():
                        raise RuntimeError("gemini quota circuit open")
                    await asyncio.sleep(_retry_wait(r.text, attempt))
                    continue
                break
    raise RuntimeError(f"gemini text failed: {last}")


async def chat_json(system: str, prompt: str, session: str = "job", retries: int = 2):
    last = None
    for attempt in range(retries + 1):
        try:
            txt = await chat_text(
                prompt if attempt == 0 else prompt + "\n\nCRITICAL: respond with ONLY valid JSON.",
                system=system)
            return parse_json(txt)
        except Exception as e:
            last = e
    raise RuntimeError(f"gemini json failed: {str(last)[:200]}")


async def gen_image(prompt: str, ref_image_bytes: bytes = None) -> bytes:
    from services.generation import ProviderFailure, response_failure
    if not gemini_key():
        raise ProviderFailure('GEMINI_API_KEY is not configured', code='NOT_CONFIGURED')
    last = None
    for model in IMAGE_MODELS:
        parts = [{"text": prompt}]
        if ref_image_bytes:
            parts.append({"inlineData": {"mimeType": "image/png",
                                         "data": base64.b64encode(ref_image_bytes).decode()}})
        async with httpx.AsyncClient(timeout=240) as client:
            for attempt in range(1):
                await _respect_rate()
                r = await client.post(
                    f"{BASE}/models/{model}:generateContent", headers={'x-goog-api-key': gemini_key()},
                    json={"contents": [{"parts": parts}],
                          "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}})
                if r.status_code == 200:
                    result = r.json()
                    candidates = result.get('candidates') or [{}]
                    cparts = candidates[0].get('content', {}).get('parts', [])
                    for p in cparts:
                        if "inlineData" in p:
                            return base64.b64decode(p["inlineData"]["data"])
                    reason = result.get('promptFeedback', {}).get('blockReason') or candidates[0].get('finishReason') or 'NO_IMAGE'
                    raise ProviderFailure(f'Gemini returned no image. Reason: {reason}. ' + ' '.join(p.get('text', '') for p in cparts), model=model, code=reason,
                                          action='Review safety feedback and the supplied prompt; do not keep retrying unchanged.')
                last = response_failure(r, model)
                if r.status_code not in (404,):
                    # Auth/billing/quota failures will not be repaired by repeatedly trying models.
                    raise last
                break
    raise last or ProviderFailure('Gemini returned no image', code='NO_IMAGE')


async def tts(text: str, voice: str, out_wav, direction: str = None) -> float:
    if circuit_open():
        raise RuntimeError("gemini quota circuit open")
    spoken = " ".join(str(text).split())[:3500]
    if direction:
        spoken = f"{str(direction).strip()}:\n\n{spoken}"
    body = {
        "contents": [{"parts": [{"text": spoken}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or "Kore"}}},
        },
    }
    async with httpx.AsyncClient(timeout=240) as client:
        for attempt in range(3):
            await _respect_rate()
            r = await client.post(f"{BASE}/models/{TTS_MODEL}:generateContent?key={gemini_key()}", json=body)
            if r.status_code == 200:
                _record_ok()
                parts = r.json()["candidates"][0]["content"]["parts"]
                audio = next((p["inlineData"]["data"] for p in parts if "inlineData" in p), None)
                if not audio:
                    raise RuntimeError("gemini tts: no audio in response")
                pcm = base64.b64decode(audio)
                out_wav = str(out_wav)
                with wave.open(out_wav, "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(24000)
                    f.writeframes(pcm)
                from pathlib import Path
                from services.media import ffprobe_duration
                return ffprobe_duration(Path(out_wav))
            if r.status_code == 429:
                _record_429()
                if circuit_open():
                    raise RuntimeError("gemini quota circuit open")
                await asyncio.sleep(_retry_wait(r.text, attempt))
                continue
            raise RuntimeError(f"gemini tts failed: {r.status_code} {r.text[:150]}")
    raise RuntimeError(f"gemini tts failed: {r.status_code} {r.text[:150]}")
