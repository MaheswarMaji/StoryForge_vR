import asyncio
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).parent / ".env")

from db import db
from routes import router
from auth import auth_router, verify_auth
from admin import admin_router
from services.ocr import MEDIA_ROOT
import job_queue
import tasks as tasks_mod

app = FastAPI(title="StoryForge API", version="1.0", dependencies=[Depends(verify_auth)])

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix='/api')
from routers.engines import router as engine_router
api_router.include_router(engine_router)
app.include_router(auth_router)
app.include_router(admin_router)
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/api/media", StaticFiles(directory=str(MEDIA_ROOT)), name="media")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("storyforge")

CHANNEL_SEEDS = [
    {
        "key": "mythology", "name": "Mythic Shorts — Dramatic",
        "description": "Puranas, Bhagwat, Ramayana & Mahabharata retellings. Warm authoritative male narration, devotional-to-epic music.",
        "language": "hi", "tone": "Warm, authoritative, dramatic storytelling with dramatic pauses",
        "voice": "kokoro:hm_omega", "voice_speed": 0.95, "music_mood": "devotional", "music_volume": 0.16,
        "safety_level": "general", "is_kids": False,
        "style_prefix": "Indian miniature painting style with gold leaf details, deep indigo and saffron palette, cinematic 4K, dramatic temple lighting",
        "cta_text": "Follow for more legendary tales from the Puranas",
    },
    {
        "key": "folk_ghost", "name": "Folk Tales — Whimsical",
        "description": "Thakurmar Jhuli & Bengali/Indian folk and ghost lore for kids. Playful narrator, gentle spooks, kind morals.",
        "language": "bn", "tone": "Friendly, playful, animated grandmother-style narration for kids",
        "voice": "local:auto", "voice_speed": 1.0, "music_mood": "moral", "music_volume": 0.14,
        "safety_level": "strict_kids", "is_kids": True,
        "style_prefix": "Whimsical storybook illustration, soft pastel palette with glowing lanterns, rounded friendly shapes, gentle magical night atmosphere",
        "cta_text": "Subscribe for more bedtime folk tales",
    },
    {
        "key": "public_interest", "name": "Public Interest — News Explained",
        "description": "Daily factual Shorts on climate, disasters, science & tech, institutional reports, economy and public health. Every video passes an independent policy agent + human review before publishing.",
        "language": "en", "tone": "Crisp, factual, engaging news-explainer — neutral and authoritative",
        "voice": "kokoro:am_adam", "voice_speed": 1.05, "music_mood": "suspense", "music_volume": 0.10,
        "safety_level": "news", "is_kids": False,
        "style_prefix": "Clean broadcast news style, modern flat infographic aesthetic, deep navy and white with a single accent color, professional studio lighting",
        "cta_text": "Follow for daily public-interest briefings",
    },
]


async def seed_channels():
    from bson import ObjectId
    for seed in CHANNEL_SEEDS:
        existing = await db.channels.find_one({"key": seed["key"]})
        if not existing:
            await db.channels.insert_one({"_id": str(ObjectId()), **seed})
        else:
            if isinstance(existing["_id"], ObjectId):
                doc = dict(existing)
                doc["_id"] = str(existing["_id"])
                await db.channels.delete_one({"key": seed["key"]})
                await db.channels.insert_one(doc)


@app.on_event("startup")
async def startup():
    await seed_channels()
    await _load_social_settings()
    await _load_key_vault()
    await _seed_video_type_channels()
    from services.tts_defaults import migrate_channel_defaults
    await migrate_channel_defaults(db)
    tasks_mod.register_all()
    asyncio.create_task(_delayed_workers())


async def _load_key_vault():
    doc = await db.settings.find_one({"key": "queue"})
    if doc and doc.get("paused"):
        job_queue.QUEUE_PAUSED["paused"] = True
        print("[startup] queue was paused before restart — staying paused", flush=True)
    doc = await db.settings.find_one({"key": "api_keys"})
    n = 0
    for k, v in (doc or {}).get("values", {}).items():
        from routes import ALLOWED_VAULT_KEYS
        if k in ALLOWED_VAULT_KEYS:
            # Explicit vault values (including cleared keys) override stale environment defaults.
            os.environ[k] = str(v).strip()
            n += 1
    if n:
        print(f"[startup] loaded {n} API keys from settings vault", flush=True)


async def _seed_video_type_channels():
    from services.video_types import VIDEO_TYPES
    for vtype, cfg in VIDEO_TYPES.items():
        key = f"vt-{vtype}"
        if not await db.channels.find_one({"key": key}):
            from models import Channel
            doc = Channel(key=key, name=cfg["name"],
                          description=f"{cfg['name']} ({cfg['audience']}) — voice & music auto-selected",
                          language=cfg["language"], tone=cfg["tone"], voice=cfg["voice"],
                          music_mood=cfg["music_mood"], music_volume=cfg["music_volume"],
                          safety_level=cfg["safety_level"], is_kids=cfg["is_kids"],
                          style_prefix=cfg["style_prefix"], cta_text=cfg["cta_text"],
                          mode="slide", video_type=vtype)
            await db.channels.insert_one(doc.to_mongo())
            print(f"[startup] seeded channel {key}", flush=True)


async def _load_social_settings():
    from services import social
    doc = await db.settings.find_one({"key": "instagram"})
    if doc and doc.get("access_token") and doc.get("user_id"):
        social.set_ig_creds(doc["access_token"], doc["user_id"], "settings")
        print("[social] Instagram credentials loaded from settings", flush=True)


async def _delayed_workers():
    await job_queue.start_workers(2)
    log.info("job queue workers started")
    # engagement defaults to every 6 hours — adjustable live in Settings (db.settings.scheduler)
    asyncio.create_task(_periodic("engagement", 6.0, _engagement_ready))
    asyncio.create_task(_periodic("news", 6 * 3600 / 3600, lambda: True))


async def _scheduler_interval(job_type: str, default: float) -> float:
    doc = await db.settings.find_one({"key": "scheduler"})
    if job_type == "engagement":
        hours = (doc or {}).get("engagement_hours", default)
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            hours = default
        return max(0.25, min(72.0, hours)) * 3600
    return default * 3600


async def _engagement_ready():
    from services import social
    return any(social.cred_status().values())


async def _periodic(job_type: str, default_hours: float, ready):
    from job_queue import enqueue
    await asyncio.sleep(30)
    while True:
        try:
            interval = await _scheduler_interval(job_type, default_hours)
            if await ready() if asyncio.iscoroutinefunction(ready) else ready():
                await enqueue(job_type, "system", f"Scheduled {job_type}")
            await asyncio.sleep(interval)
        except Exception as e:
            log.warning("periodic %s failed: %s", job_type, e)
            await asyncio.sleep(300)


@app.on_event("shutdown")
async def shutdown():
    db.client.close()


# The imported router already carries /api. Fold it in before the final include.
for route in router.routes:
    api_router.routes.append(route)
app.include_router(api_router)
