"""Studio adapters. Default: the local batch.py worker (services/studio_local.py).
The HTTP client below is only used if a Studio base URL is saved (legacy/proposed contract)."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import quote
import httpx
from db import db
from services.generation import ProviderFailure, generation_context, preferences, response_failure, redact

STATES = {'queued', 'running', 'succeeded', 'review_required', 'blocked', 'failed', 'cancelled'}


class StudioClient:
    def __init__(self, config):
        self.config = config
        self.base = config.studio_base_url.rstrip('/')
        token = os.environ.get('STUDIO_API_TOKEN', '').strip()
        if not self.base or not token:
            raise ProviderFailure('Studio is not configured. Install its HTTP API, save the reachable base URL and add STUDIO_API_TOKEN in the vault.', code='NOT_CONFIGURED')
        self.client = httpx.AsyncClient(headers={'Authorization': f'Bearer {token}'},
            timeout=httpx.Timeout(connect=10, read=60, write=120, pool=10), follow_redirects=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method, path, **kw):
        # Never trust status_url/download_url supplied by the remote service with our bearer token.
        r = await self.client.request(method, f'{self.base}/{path.lstrip("/")}', **kw)
        if not 200 <= r.status_code < 300:
            raise response_failure(r, 'studio')
        return r

    async def json(self, method, path, **kw):
        r = await self.request(method, path, **kw)
        try:
            out = r.json()
            if not isinstance(out, dict):
                raise ValueError()
            return out
        except ValueError as error:
            raise ProviderFailure('Studio returned invalid JSON', code='INVALID_RESPONSE') from error

    async def upload(self, data, filename, mime):
        out = await self.json('POST', 'assets', files={'file': (filename, data, mime)})
        if not out.get('asset_id') or not out.get('status') or not out.get('media_type'):
            raise ProviderFailure('Studio asset response must contain asset_id, media_type and status', code='INVALID_RESPONSE')
        return out


async def _reference(studio, ref_image):
    if not ref_image or not Path(ref_image).exists():
        return ''
    data = Path(ref_image).read_bytes()
    identity = hashlib.sha256((studio.base + ':').encode() + data).hexdigest()
    old = await db.studio_assets.find_one({'_id': identity})
    if old:
        return old['asset_id']
    out = await studio.upload(data, 'approved-reference.png', 'image/png')
    if out['status'] != 'ready':
        raise ProviderFailure('Studio reference asset is not ready; asset preparation/QC is required.', code='ASSET_NOT_READY')
    await db.studio_assets.update_one({'_id': identity}, {'$setOnInsert': out}, upsert=True)
    return out['asset_id']


async def generate(kind, prompt, out_path, config, ref_image=None, segment=None, duration=6):
    if not config.studio_base_url:
        if kind != 'image':
            raise ProviderFailure('The Studio worker only generates still images; video is not exposed. Use Ken Burns or another video engine.',
                                  code='UNSUPPORTED_OPERATION')
        from services import studio_local
        return await studio_local.generate_image(prompt, out_path, config, segment)
    ctx = generation_context.get()
    story_id = ctx.get('story_id', '')
    story = await db.stories.find_one({'_id': story_id}) or {}
    prefs = await preferences(story_id)
    binding = prefs.studio.model_dump()
    shot = prefs.studio_segments.get(str(segment))
    if shot:
        for field, val in shot.model_dump().items():
            if isinstance(val, dict):
                binding[field] = {**binding[field], **val}
            elif val:
                binding[field] = val
    character = binding['character']
    if not character.get('id') or not character.get('version'):
        raise ProviderFailure('Studio needs a registered, versioned character in the story’s Studio mapping. An uploaded sheet alone is not a production-ready character.', code='CHARACTER_NOT_REGISTERED')
    async with StudioClient(config) as studio:
        caps = await studio.json('GET', 'capabilities')
        # operations is a provisional capability-schema field, documented in memory/STUDIO_API.md.
        operation = f'generate_{kind}'
        operations = caps.get('operations', caps.get('supported_operations'))
        if not isinstance(operations, list) or operation not in operations:
            raise ProviderFailure(f'Studio must advertise {operation} in capabilities.operations before use.', code='UNSUPPORTED_OPERATION')
        references = dict(binding['references'])
        ref_id = await _reference(studio, ref_image)
        if ref_id:
            references['master_frame_asset_id' if kind == 'video' else 'appearance_asset_id'] = ref_id
        chunks = (story.get('script') or {}).get('chunks') or []
        payload = {
            'operation': operation, 'project_id': story_id, 'shot_id': f'S{segment + 1:03d}' if segment is not None else 'character-reference',
            'character': character, 'references': references,
            'direction': {**binding['direction'], 'action': prompt, 'preserve_environment': True},
            'output': {'width': 480, 'height': 832, **({'fps': 16, 'frame_count': max(1, round(duration * 16))} if kind == 'video' else {})},
            'budget': {'max_generation_attempts': 1},
            # Explicit proposed extensions. Studio must preserve these unabridged.
            'consistency_sheet': story.get('character_sheet') or {},
            'scene': chunks[segment] if segment is not None and 0 <= segment < len(chunks) else {},
            'lora_ids': binding['lora_ids'],
        }
        fingerprint = hashlib.sha256((studio.base + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
        existing = await db.studio_jobs.find_one({'fingerprint': fingerprint, '$or': [
            {'status': {'$in': ['submitting', 'queued', 'running', 'review_required', 'blocked']}},
            {'status': 'succeeded', 'production_pass': False},
        ]})
        idem = existing['id'] if existing else hashlib.sha256(f'{fingerprint}:{ctx.get("job_id", "manual")}'.encode()).hexdigest()
        await db.studio_jobs.update_one({'_id': idem}, {'$setOnInsert': {
            'id': idem, 'job_id': '', 'story_id': story_id, 'segment': segment, 'operation': operation,
            'status': 'submitting', 'production_pass': False, 'qc': {}, 'error': '',
            'request': payload, 'fingerprint': fingerprint, 'base_url': studio.base, 'created_at': time.time(),
        }}, upsert=True)
        saved = await db.studio_jobs.find_one({'_id': idem})
        job_id = saved.get('job_id', '')
        if not job_id:
            submission = await studio.json('POST', 'jobs', json=saved['request'], headers={'Idempotency-Key': idem})
            job_id = submission.get('job_id')
            if not isinstance(job_id, str) or not job_id:
                raise ProviderFailure('Studio did not return a job_id; retry will reuse the same idempotency key.', code='INVALID_RESPONSE')
            await db.studio_jobs.update_one({'_id': idem}, {'$set': {'job_id': job_id, 'status': submission.get('status', 'queued')}})
        deadline = time.monotonic() + config.studio_timeout_seconds
        while time.monotonic() < deadline:
            if ctx.get('job_id'):
                from job_queue import is_cancelled, JobCancelled
                local = await db.jobs.find_one({'_id': ctx['job_id']}, {'status': 1})
                if is_cancelled(ctx['job_id']) or (local or {}).get('status') == 'cancelled':
                    await studio.json('POST', f'jobs/{quote(job_id, safe="")}/cancel')
                    await db.studio_jobs.update_one({'_id': idem}, {'$set': {'status': 'cancelled'}})
                    raise JobCancelled()
            result = await studio.json('GET', f'jobs/{quote(job_id, safe="")}')
            state = result.get('status')
            if state not in STATES:
                raise ProviderFailure(f'Unknown Studio job status: {state}', code='INVALID_RESPONSE')
            production_pass = result.get('production_pass') is True
            err = redact(result.get('error') or result.get('message') or '')
            await db.studio_jobs.update_one({'_id': idem}, {'$set': {
                'status': state, 'production_pass': production_pass,
                'qc': result.get('qc') if isinstance(result.get('qc'), dict) else {}, 'error': err,
            }})
            if state in ('queued', 'running'):
                await asyncio.sleep(config.studio_poll_seconds)
                continue
            if state != 'succeeded' or not production_pass:
                raise ProviderFailure(f'Studio job {job_id}: {state}. Production QC pass: {production_pass}. {err} Output excluded from final video.',
                    code='QC_NOT_PASSED' if state in ('succeeded', 'review_required', 'blocked') else state.upper(),
                    action='Resolve QC in Studio, then retry to poll the retained job; do not auto-approve a preview.')
            artifacts = result.get('artifacts') or []
            artifact = next((a for a in artifacts if a.get('role') in ('output', 'final', 'production') and str(a.get('media_type', '')).startswith(kind + '/')), None)
            if not artifact or not artifact.get('artifact_id'):
                raise ProviderFailure('QC-passed Studio job has no production artifact (preview-only outputs are never used).', code='MISSING_ARTIFACT')
            response = await studio.request('GET', f'artifacts/{quote(artifact["artifact_id"], safe="")}/content')
            data = response.content
            if kind == 'image':
                import io
                from PIL import Image
                try:
                    Image.open(io.BytesIO(data)).verify()
                except Exception as error:
                    raise ProviderFailure('Studio returned invalid image bytes', code='INVALID_ARTIFACT') from error
            elif len(data) < 12 or data[4:8] != b'ftyp':
                raise ProviderFailure('Studio must return an MP4 production artifact', code='INVALID_ARTIFACT')
            path = Path(out_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + '.part')
            tmp.write_bytes(data)
            tmp.replace(path)
            return {'provider': 'studio', 'job_id': job_id, 'production_pass': True}
        raise ProviderFailure(f'Studio job {job_id} exceeded the polling deadline. Its ID is retained; retry resumes polling, not a new GPU job.', code='POLL_TIMEOUT')