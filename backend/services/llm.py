"""LLM routing: local Ollama (strict primary) -> cloud APIs (only when Ollama not configured).

Task → Model assignment
-----------------------
fast=True      Light JSON, metadata, quick structured output  → llama3.1:8b
default        Script writing, story generation, creative     → qwen2.5:72b
reasoning=True QA, editor pass, fact-check, improvements      → deepseek-r1:32b
vision=True    Image-prompt enrichment, visual understanding  → qwen3-vl:32b-instruct

All four are overridable via env:
  OLLAMA_MODEL, OLLAMA_FAST_MODEL, OLLAMA_REASONING_MODEL, OLLAMA_VISION_MODEL

Local-only policy
-----------------
When OLLAMA_BASE_URL is set, cloud APIs (Gemini/OpenAI/HF) are NEVER called for
text generation.  If Ollama is unreachable the job fails with a clear error.
Cloud keys are still used for images, video and TTS.
"""
import asyncio
import json
import os
import re
from pathlib import Path

# Cloud fallback model (used only when OLLAMA_BASE_URL is empty)
_CLOUD_MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-5.4")
LLM_IN_PRICE  = 2.5e-6
LLM_OUT_PRICE = 1.0e-5


# ─────────────────────────────── Ollama model selectors ──────────────────────

def ollama_url() -> str:
    return (os.environ.get("OLLAMA_BASE_URL") or "").strip().rstrip("/")


def ollama_main_model() -> str:
    """Script writing, story generation — large creative model."""
    return (os.environ.get("OLLAMA_MODEL") or "qwen2.5:72b").strip()


def ollama_fast_model() -> str:
    """Metadata, light JSON parsing — small/fast model."""
    return (os.environ.get("OLLAMA_FAST_MODEL") or "llama3.1:8b").strip()


def ollama_reasoning_model() -> str:
    """QA, editor pass, fact-checking — reasoning model."""
    return (os.environ.get("OLLAMA_REASONING_MODEL") or "deepseek-r1:32b").strip()


def ollama_vision_model() -> str:
    """Image-prompt enrichment, visual understanding — VL model."""
    return (os.environ.get("OLLAMA_VISION_MODEL") or "qwen3-vl:32b-instruct").strip()


def is_local_only() -> bool:
    """True when OLLAMA_BASE_URL is configured — cloud LLM calls disabled."""
    return bool(ollama_url())


def _free_ram_gb() -> float:
    """Best-effort available system RAM in GiB (0.0 if it can't be read)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return 0.0


def pick_creative_model() -> str:
    """Pick the largest creative model that fits in free CPU RAM.

    qwen2.5:72b needs ~48 GiB resident on CPU; when RAM is tighter we drop to
    deepseek-r1:32b (~20 GiB).  Every text call runs on CPU (num_gpu=0) so the
    GPU stays free for the Studio image/video worker.
    """
    big   = ollama_main_model()       # qwen2.5:72b
    small = ollama_reasoning_model()  # deepseek-r1:32b
    return big if _free_ram_gb() >= 48.0 else small


def _select_model(fast: bool = False, reasoning: bool = False, vision: bool = False) -> str:
    if vision:
        return ollama_vision_model()
    if reasoning:
        return ollama_reasoning_model()
    if fast:
        return ollama_fast_model()
    return ollama_main_model()


# ─────────────────────────────── Ollama call ─────────────────────────────────

async def _ollama_json(system: str, prompt: str, model: str = "") -> dict:
    """One JSON chat completion via Ollama."""
    import httpx
    base = ollama_url()
    if not base:
        raise RuntimeError("OLLAMA_BASE_URL is not set")
    _model = (model or ollama_main_model()).strip()
    payload = {
        "model":   _model,
        "stream":  False,
        "format":  "json",
        "keep_alive": "10m",
        # CPU-only: the GPU is reserved for the Studio image/video worker.
        "options": {"num_gpu": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{base}/api/chat", json=payload)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        return parse_json(content)


# ──────────────────────────── Cloud helpers (fallback) ───────────────────────

def hf_key() -> str:
    from services.keys import get as _get
    return _get("HF_TOKEN")


def stability_key() -> str:
    from services.keys import get as _get
    return _get("STABILITY_API_KEY")


async def _stability_image(prompt: str, out_path, aspect: str = "9:16", model: str = "core") -> bool:
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
    raise response_failure(r, f"stability-{endpoint}")


async def _hf_json(system: str, prompt: str):
    import httpx
    key = hf_key()
    if not key:
        raise RuntimeError("HF_TOKEN not set")
    hf_model = os.environ.get("HF_TEXT_MODEL", "Qwen/Qwen2.5-72B-Instruct")
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://router.huggingface.co/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": hf_model,
                  "messages": [{"role": "system", "content": system},
                                {"role": "user",   "content": prompt}]})
        r.raise_for_status()
        return parse_json(r.json()["choices"][0]["message"]["content"])


async def _openai_text(key: str, system: str, prompt: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, timeout=180)
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    resp = await client.chat.completions.create(model=_CLOUD_MODEL, messages=messages)
    return resp.choices[0].message.content or ""


# ──────────────────────────── Shared utilities ───────────────────────────────

def estimate_llm_cost(prompt: str, response: str) -> float:
    return len(prompt) / 4 * LLM_IN_PRICE + len(response) / 4 * LLM_OUT_PRICE


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text).strip()
    # Strip deepseek-r1 <think>…</think> reasoning block
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    m = re.search(r"[\[{]", text)
    if not m:
        raise ValueError("no JSON found in LLM response")
    obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
    return obj


def candidate_keys():
    """OpenAI API keys to try, in priority order."""
    from services.keys import get as _get
    key = _get("OPENAI_API_KEY")
    return [key] if key else []


# ──────────────────────────── Public API ─────────────────────────────────────

async def ask_json(system: str, prompt: str, session: str = "job",
                   retries: int = 2, prefer_local: bool = False,
                   fast: bool = False, reasoning: bool = False,
                   vision: bool = False, model: str = "") -> dict:
    """Ask the LLM for a JSON response.

    Model selection (Ollama only when OLLAMA_BASE_URL is set):
      fast=True      → OLLAMA_FAST_MODEL   (llama3.1:8b)
      reasoning=True → OLLAMA_REASONING_MODEL (deepseek-r1:32b)
      vision=True    → OLLAMA_VISION_MODEL (qwen3-vl:32b-instruct)
      default        → OLLAMA_MODEL        (qwen2.5:72b)

    When OLLAMA_BASE_URL is set, cloud APIs are NEVER called for text.
    When not set, falls through to Gemini → OpenAI → HF.
    """
    last_err = None

    # ── 1. LOCAL OLLAMA ───────────────────────────────────────────────────────
    if ollama_url():
        _model = model or _select_model(fast=fast, reasoning=reasoning, vision=vision)
        for attempt in range(retries + 1):
            try:
                result = await _ollama_json(system, prompt, model=_model)
                return result
            except Exception as exc:
                last_err = exc
                print(f"[llm] ollama {_model} attempt {attempt + 1}/{retries + 1}: "
                      f"{str(exc)[:140]}", flush=True)
                if attempt < retries:
                    await asyncio.sleep(min(4.0, 1.5 ** attempt))
        # STRICT: Ollama configured → no silent cloud fallback
        from services.generation import redact
        raise ValueError(
            f"Ollama LLM failed after {retries + 1} attempts "
            f"(model={_model}, url={ollama_url()}): "
            f"{redact(last_err) or type(last_err).__name__}. "
            "Ensure Ollama is running and the model is pulled."
        )

    # ── 2. CLOUD CHAIN (only when Ollama is not configured) ──────────────────
    from services import gemini

    if gemini.gemini_key():
        for _ in range(retries + 1):
            try:
                return await gemini.chat_json(system, prompt, session, retries=0)
            except Exception as exc:
                last_err = exc
        print(f"[llm] gemini chain failed: {str(last_err)[:120]}", flush=True)

    keys = [k for k in (gemini.openai_key(),) if k]
    for attempt in range(retries + 1):
        for key in keys:
            try:
                msg_text = prompt if attempt == 0 else (
                    prompt + "\n\nCRITICAL: respond with ONLY a single valid JSON object/array."
                               " No markdown, no commentary.")
                txt = await _openai_text(key, system, msg_text)
                return parse_json(txt)
            except Exception as exc:
                last_err = exc
                if not any(w in str(exc).lower() for w in
                           ("budget", "credit", "quota", "rate", "429", "401", "exceeded")):
                    break

    if hf_key():
        try:
            return await _hf_json(system, prompt)
        except Exception as exc:
            last_err = exc

    from services.generation import redact
    raise ValueError(
        f"LLM call failed: {redact(last_err) or type(last_err).__name__}. "
        "Check provider diagnostics and configured text-model quota."
    )


async def ask_json_fast(system: str, prompt: str, session: str = "job", retries: int = 2) -> dict:
    """Metadata, light JSON — uses llama3.1:8b."""
    return await ask_json(system, prompt, session=session, retries=retries, fast=True)


async def ask_json_reasoning(system: str, prompt: str, session: str = "job", retries: int = 2) -> dict:
    """QA, editor, fact-check — uses deepseek-r1:32b."""
    return await ask_json(system, prompt, session=session, retries=retries, reasoning=True)
