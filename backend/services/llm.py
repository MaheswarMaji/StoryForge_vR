import asyncio
import json
import os
import re
from pathlib import Path

from services.keys import candidate_keys

# Override with OPENAI_TEXT_MODEL in backend/.env to use any chat model your OpenAI key can access.
MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-5.4")
LLM_IN_PRICE = 2.5e-6   # $ per input token (estimate)
LLM_OUT_PRICE = 1.0e-5  # $ per output token (estimate)


async def _openai_text(key: str, system: str, prompt: str) -> str:
    """One chat completion through the official OpenAI SDK."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=key, timeout=180)
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    resp = await client.chat.completions.create(model=MODEL, messages=messages)
    return resp.choices[0].message.content or ""


def estimate_llm_cost(prompt: str, response: str) -> float:
    return len(prompt) / 4 * LLM_IN_PRICE + len(response) / 4 * LLM_OUT_PRICE


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text).strip()
    m = re.search(r"[\[{]", text)
    if not m:
        raise ValueError("no JSON found in LLM response")
    obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
    return obj


def ollama_url():
    return (os.environ.get("OLLAMA_BASE_URL") or "").strip()


async def _ollama_json(system: str, prompt: str):
    """Local Qwen (Ollama) for small processing — activated by setting OLLAMA_BASE_URL."""
    import httpx

    base = ollama_url().rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:32b")
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{base}/api/chat", json={
            "model": model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return parse_json(r.json()["message"]["content"])


def hf_key():
    return (os.environ.get("HF_TOKEN") or "").strip()


def stability_key():
    return (os.environ.get("STABILITY_API_KEY") or "").strip()


async def _stability_image(prompt: str, out_path, aspect: str = "9:16", model: str = "core") -> bool:
    """Stability AI (Stable Image Core / SD3.5) — funded real AI images, ~5-15s per call."""
    import httpx

    key = stability_key()
    if not key:
        raise RuntimeError("STABILITY_API_KEY not set")
    endpoint = "core" if model == "core" else "sd3"
    fields = {"prompt": prompt, "output_format": "png", "aspect_ratio": aspect}
    if model != "core":
        fields["model"] = model
    r = await asyncio.to_thread(
        httpx.post,
        f"https://api.stability.ai/v2beta/stable-image/generate/{endpoint}",
        headers={"Authorization": f"Bearer {key}", "Accept": "image/*"},
        files={k: (None, v) for k, v in fields.items()},
        timeout=110)
    if r.status_code == 200 and r.content[:3] in (b"\x89PN", b"\xff\xd8\xff"):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(r.content)
        return True
    from services.generation import response_failure
    raise response_failure(r, f'stability-{endpoint}')


async def _hf_json(system: str, prompt: str):
    """Free hosted Qwen via Hugging Face Inference Providers (free monthly credits)."""
    import httpx

    if not hf_key():
        raise RuntimeError("HF_TOKEN not set")
    model = os.environ.get("HF_TEXT_MODEL", "Qwen/Qwen2.5-72B-Instruct")
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("https://router.huggingface.co/v1/chat/completions",
                              headers={"Authorization": f"Bearer {hf_key()}"},
                              json={"model": model,
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return parse_json(r.json()["choices"][0]["message"]["content"])


async def ask_json(system: str, prompt: str, session: str = "job", retries: int = 2,
                   prefer_local: bool = False):
    from services import gemini  # lazy: avoids circular import
    last_err = None

    # 0) local Qwen (Ollama) first, then free hosted HF Qwen, for small processing tasks
    if prefer_local:
        if ollama_url():
            try:
                return await _ollama_json(system, prompt)
            except Exception as e:
                last_err = e
                print(f"[llm] ollama chain failed: {str(e)[:120]}", flush=True)
        if hf_key():
            try:
                return await _hf_json(system, prompt)
            except Exception as e:
                last_err = e
                print(f"[llm] hf qwen chain failed: {str(e)[:120]}", flush=True)

    # 1) user's Gemini key first (cheapest)
    if gemini.gemini_key():
        for attempt in range(retries + 1):
            try:
                return await gemini.chat_json(system, prompt, session, retries=0)
            except Exception as e:
                last_err = e
        print(f"[llm] gemini chain failed: {str(last_err)[:120]}", flush=True)

    # 2) OpenAI (official SDK) with your own OPENAI_API_KEY
    keys = [k for k in (gemini.openai_key(),) if k]
    for attempt in range(retries + 1):
        for key in keys:
            try:
                msg_text = prompt if attempt == 0 else (
                    prompt + "\n\nCRITICAL: respond with ONLY a single valid JSON object/array. No markdown, no commentary."
                )
                txt = await _openai_text(key, system, msg_text)
                return parse_json(txt)
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if not any(w in msg for w in ("budget", "credit", "quota", "rate", "429", "401", "exceeded")):
                    break

    # 3) free hosted HF Qwen as the last resort
    if hf_key():
        try:
            return await _hf_json(system, prompt)
        except Exception as e:
            last_err = e
            print(f"[llm] hf qwen chain failed: {str(e)[:120]}", flush=True)

    from services.generation import redact
    raise ValueError(f"LLM call failed: {redact(last_err) or type(last_err).__name__}. Check provider diagnostics and configured text-model quota.")
