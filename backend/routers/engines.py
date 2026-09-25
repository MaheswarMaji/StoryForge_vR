import os
from fastapi import APIRouter, HTTPException, UploadFile, File
import httpx
from db import db
from engine_models import (EngineSettings, EngineSettingsView, StoryEngines, StoryEnginesView,
    GenerationEvent, DiagnosticRequest, ConnectionResult, StudioJobView,
    StudioAssetView, StudioCharacterRequest, StudioCharacterView)
from services import generation, studio_local

router = APIRouter()


async def view_settings():
    s = await generation.settings()
    options = [
        ('auto', 'Automatic · reference-safe fallback', '', True, 'Only Auto may switch providers.'),
        ('gemini', 'Google Gemini · your key', 'GEMINI_API_KEY', True, 'Image generation requires project billing/quota.'),
        ('openai', 'OpenAI GPT Image', 'OPENAI_API_KEY', False, 'This adapter cannot edit reference images; blocked for continuity-locked frames.'),
        ('stability', 'Stability AI', 'STABILITY_API_KEY', False, 'Text-to-image only; blocked for continuity-locked frames.'),
        ('fal_flux', 'fal.ai FLUX', 'FAL_KEY', False, 'Text-to-image only; blocked for continuity-locked frames.'),
        ('replicate_flux', 'Replicate FLUX', 'REPLICATE_API_TOKEN', False, 'Text-to-image only; blocked for continuity-locked frames.'),
        ('hf_flux', 'Hugging Face FLUX', 'HF_TOKEN', False, 'Hosted model availability varies; no reference support.'),
        ('studio', 'Your Studio · server27', '', True, 'Local worker on server27: still images only (576×1024). StoryForge review decides acceptance.'),
    ]
    def option(row):
        key, label, env, reference, note = row
        configured = (not env or bool(os.getenv(env, '').strip()))
        if key == 'studio':
            configured = bool(s.studio_base_url) or studio_local.is_configured()
        return dict(id=key, label=label, configured=configured, reference_aware=reference, note=note)
    studio_video = ('studio', 'Your Studio · server27', '', True, 'The worker does not expose video; not available.')
    videos = [options[0], ('kenburns', 'Local motion · Ken Burns (not generative AI)', '', True, 'Animates the approved image locally; no API cost.'),
              ('gemini_veo', 'Google Veo · image-to-video', 'GEMINI_API_KEY', True, 'Uses the approved frame; paid video quota required.'), studio_video]
    video_options = [option(o) for o in videos]
    for o in video_options:
        if o['id'] == 'studio':
            o['configured'] = bool(s.studio_base_url and os.getenv('STUDIO_API_TOKEN', '').strip())
    return EngineSettingsView(**s.model_dump(), studio_token_set=bool(os.getenv('STUDIO_API_TOKEN', '').strip()),
                              image_options=[option(o) for o in options], video_options=video_options)


@router.get('/settings/media-engines', response_model=EngineSettingsView)
async def get_engines():
    return await view_settings()


@router.put('/settings/media-engines', response_model=EngineSettingsView)
async def save_engines(body: EngineSettings):
    await db.settings.update_one({'key': 'media_engines'}, {'$set': {'values': body.model_dump()}}, upsert=True)
    return await view_settings()


async def require_story(story_id):
    story = await db.stories.find_one({'_id': story_id})
    if not story:
        raise HTTPException(404, 'Story not found')
    return story


@router.get('/stories/{story_id}/media-engines', response_model=StoryEnginesView)
async def get_story_engines(story_id: str):
    await require_story(story_id)
    prefs = await generation.preferences(story_id)
    defaults = await generation.settings()
    return StoryEnginesView(**prefs.model_dump(), effective={'image': prefs.image or defaults.image, 'video': prefs.video or defaults.video})


@router.put('/stories/{story_id}/media-engines', response_model=StoryEnginesView)
async def save_story_engines(story_id: str, body: StoryEngines):
    story = await require_story(story_id)
    if story.get('status') in ('rendering', 'scripting') or await db.jobs.count_documents({'ref_id': story_id, 'status': {'$in': ['queued', 'running']}}):
        raise HTTPException(409, 'Wait for the active job to finish before switching engines')
    n = len((story.get('script') or {}).get('chunks') or [])
    if any(not k.isdigit() or str(int(k)) != k or not 0 <= int(k) < n for k in set(body.segments) | set(body.studio_segments)):
        raise HTTPException(422, 'Invalid segment index')
    await db.story_engines.replace_one({'_id': story_id}, {'_id': story_id, **body.model_dump()}, upsert=True)
    return await get_story_engines(story_id)


@router.get('/generation-events', response_model=list[GenerationEvent])
async def events(story_id: str = ''):
    query = {'story_id': story_id} if story_id else {}
    return await db.generation_events.find(query, {'_id': 0}).sort('created_at', -1).limit(40).to_list(40)


@router.post('/settings/media-engines/test-gemini', response_model=GenerationEvent)
async def test_gemini(body: DiagnosticRequest):
    from services import gemini
    try:
        if not gemini.gemini_key():
            raise generation.ProviderFailure('GEMINI_API_KEY is not configured', code='NOT_CONFIGURED', action='Save your key in Integrations & API Keys.')
        if body.generate_sample:
            await gemini.gen_image('A single red apple on a plain white background. No text.')
            message = 'Sample image generated successfully. Image generation quota is available.'
        else:
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get(f'{gemini.BASE}/models/{gemini.IMAGE_MODELS[0]}', headers={'x-goog-api-key': gemini.gemini_key()})
                if r.status_code != 200:
                    raise generation.response_failure(r, gemini.IMAGE_MODELS[0])
            message = 'Key and model access verified. This free check does NOT verify paid image-generation quota; use Test image to check generation.'
        return await generation.record_event('diagnostic', 'gemini', 'succeeded', message, model=gemini.IMAGE_MODELS[0])
    except Exception as error:
        return await generation.record_event('diagnostic', 'gemini', 'failed', error=error)


@router.post('/settings/studio/test', response_model=ConnectionResult)
async def studio_test():
    from services.studio import StudioClient
    try:
        config = await generation.settings()
        if not config.studio_base_url:
            ok, message = studio_local.check()
            if not ok:
                raise generation.ProviderFailure(message, code='NOT_CONFIGURED', action='Check STUDIO_ROOT / STUDIO_PYTHON / STUDIO_BATCH_SCRIPT in backend/.env.')
            help_text = await studio_local.self_test()
            root, python, script = studio_local.paths()
            return ConnectionResult(status='connected', message='Local Studio worker starts correctly. Images: PNG 576×1024, one job at a time.',
                                    capabilities={'transport': 'local subprocess', 'worker': str(script), 'python': str(python), 'help': help_text[:600]})
        async with StudioClient(config) as studio:
            capabilities = await studio.json('GET', 'capabilities')
        return ConnectionResult(status='connected', message='Studio API reachable. Generation and QC still depend on installed workers.', capabilities=capabilities)
    except Exception as error:
        event = await generation.record_event('connection', 'studio', 'failed', error=error)
        return ConnectionResult(status='not_connected', message=event['message'])


@router.get('/stories/{story_id}/studio-jobs', response_model=list[StudioJobView])
async def studio_jobs(story_id: str):
    await require_story(story_id)
    return await db.studio_jobs.find({'story_id': story_id}, {'_id': 0, 'request': 0, 'base_url': 0}).sort('created_at', -1).limit(30).to_list(30)


@router.post('/stories/{story_id}/studio-assets', response_model=StudioAssetView)
async def upload_reference(story_id: str, file: UploadFile = File(...)):
    await require_story(story_id)
    if file.content_type not in ('image/png', 'image/jpeg', 'image/webp', 'video/mp4', 'audio/wav', 'audio/mpeg'):
        raise HTTPException(415, 'Unsupported reference format')
    data = await file.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, 'Reference must be under 50 MB')
    from services.studio import StudioClient
    try:
        async with StudioClient(await generation.settings()) as studio:
            out = await studio.upload(data, file.filename or 'reference', file.content_type)
        await db.studio_assets.insert_one({'story_id': story_id, **out})
        return StudioAssetView(**out)
    except Exception as error:
        raise HTTPException(502, generation.redact(error)) from error


@router.post('/stories/{story_id}/studio-jobs/{local_id}/{action}', response_model=StudioJobView)
async def studio_job_action(story_id: str, local_id: str, action: str):
    from services.studio import StudioClient, STATES
    from urllib.parse import quote
    await require_story(story_id)
    job = await db.studio_jobs.find_one({'_id': local_id, 'story_id': story_id})
    if not job or not job.get('job_id'):
        raise HTTPException(404, 'Studio job not found or submission not yet confirmed')
    if action not in ('refresh', 'cancel'):
        raise HTTPException(422, 'Use refresh or cancel')
    config = await generation.settings()
    if config.studio_base_url != job.get('base_url'):
        raise HTTPException(409, 'Restore this job’s original Studio service URL before polling it')
    try:
        async with StudioClient(config) as studio:
            path = f'jobs/{quote(job["job_id"], safe="")}'
            if action == 'cancel':
                await studio.json('POST', f'{path}/cancel')
            result = await studio.json('GET', path)
        if result.get('status') not in STATES:
            raise ValueError('Studio returned an unknown job state')
        values = {'status': result['status'], 'production_pass': result.get('production_pass') is True,
                  'qc': result.get('qc') or {}, 'error': generation.redact(result.get('error') or '')}
        await db.studio_jobs.update_one({'_id': local_id}, {'$set': values})
        return StudioJobView(**{**job, **values})
    except Exception as error:
        raise HTTPException(502, generation.redact(error)) from error


@router.post('/stories/{story_id}/studio-characters', response_model=StudioCharacterView)
async def register_character(story_id: str, body: StudioCharacterRequest):
    story = await require_story(story_id)
    from services.studio import StudioClient
    try:
        definition = {**body.definition, 'consistency_sheet': story.get('character_sheet') or {}}
        async with StudioClient(await generation.settings()) as studio:
            result = await studio.json('POST', 'characters', json=definition)
        await db.studio_characters.insert_one({'story_id': story_id, 'definition': result})
        return StudioCharacterView(definition=result)
    except Exception as error:
        raise HTTPException(502, generation.redact(error)) from error