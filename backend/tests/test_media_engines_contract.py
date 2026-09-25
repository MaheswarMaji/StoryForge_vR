"""Contract verification. Studio/paid providers are MOCKED; Mongo uses an isolated DB."""
import asyncio
import base64
import io
import json
import os
import uuid
from pathlib import Path
import httpx
import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image
from engine_models import EngineSettings
from services import generation, studio, gemini, router


@pytest_asyncio.fixture
async def sandbox(monkeypatch):
    client = AsyncIOMotorClient(os.environ['MONGO_URL'])
    database = client[f'storyforge_contract_{uuid.uuid4().hex}']
    monkeypatch.setattr(generation, 'db', database)
    monkeypatch.setattr(studio, 'db', database)
    token = generation.generation_context.set({'story_id': 'test-story', 'job_id': 'attempt-1'})
    monkeypatch.setenv('STUDIO_API_TOKEN', 'mock-studio-private-token')
    await database.stories.insert_one({'_id': 'test-story', 'character_sheet': {'anchor': 'CONTINUITY ' * 1500 + 'END_OF_FULL_BIBLE'}, 'script': {'chunks': [{'visual': 'Exact scene', 'voiceover': 'Exact narration'}]}})
    await database.story_engines.insert_one({'_id': 'test-story', 'studio': {'character': {'id': 'boy', 'version': 'v001'}, 'references': {'environment_asset_id': 'river'}, 'direction': {}, 'lora_ids': ['dhruv-v1']}})
    yield database
    generation.generation_context.reset(token)
    await client.drop_database(database.name)
    client.close()


def png():
    b = io.BytesIO()
    Image.new('RGB', (16, 16), 'red').save(b, format='PNG')
    return b.getvalue()


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: original(*args, **kwargs, transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_selected_gemini_never_falls_back_to_other_providers(sandbox, monkeypatch, tmp_path):
    await sandbox.settings.insert_one({'key': 'media_engines', 'values': {'image': 'gemini', 'video': 'kenburns'}})
    called = []
    async def direct(prompt, ref=None):
        called.append('gemini')
        raise generation.ProviderFailure('quota limit: 0', model='gemini-image', code='RESOURCE_EXHAUSTED', http_status=429)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Wrong provider called')
    monkeypatch.setattr(gemini, 'gen_image', direct)
    from services import imagegen
    monkeypatch.setattr(imagegen, '_gen_openai_image', forbidden)
    with pytest.raises(generation.ProviderFailure, match='RESOURCE_EXHAUSTED'):
        await router.image('prompt', tmp_path / 'out.png')
    assert called == ['gemini']
    event = await sandbox.generation_events.find_one({'provider': 'gemini'})
    assert event['http_status'] == 429 and event['model'] == 'gemini-image'
    assert not (tmp_path / 'out.png').exists()


@pytest.mark.asyncio
async def test_segment_inheritance(sandbox):
    await sandbox.settings.insert_one({'key': 'media_engines', 'values': {'image': 'gemini', 'video': 'kenburns'}})
    await sandbox.story_engines.update_one({'_id': 'test-story'}, {'$set': {'image': 'openai', 'segments': {'0': {'image': 'studio', 'video': 'gemini_veo'}}}})
    assert await generation.resolve('image') == 'openai'
    assert await generation.resolve('image', 0) == 'studio'
    assert await generation.resolve('video', 0) == 'gemini_veo'
    assert await generation.resolve('video', 1) == 'kenburns'


@pytest.mark.asyncio
async def test_quota_error_has_action_and_no_retry(sandbox, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'mock-google-secret-key')
    async def no_wait(): pass
    monkeypatch.setattr(gemini, '_respect_rate', no_wait)
    calls = []
    def handle(request):
        calls.append(request)
        assert 'key=' not in str(request.url)
        return httpx.Response(429, json={'error': {'status': 'RESOURCE_EXHAUSTED', 'message': 'free_tier_requests, limit: 0'}})
    mock_http(monkeypatch, handle)
    with pytest.raises(generation.ProviderFailure) as error:
        await gemini.gen_image('prompt')
    assert error.value.http_status == 429 and 'Enable billing' in error.value.action
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_image_safety_reason(sandbox, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'mock-key')
    async def no_wait(): pass
    monkeypatch.setattr(gemini, '_respect_rate', no_wait)
    mock_http(monkeypatch, lambda _: httpx.Response(200, json={'promptFeedback': {'blockReason': 'SAFETY'}}))
    with pytest.raises(generation.ProviderFailure, match='SAFETY'):
        await gemini.gen_image('prompt')


@pytest.mark.asyncio
async def test_studio_qc_gate_replay_full_payload_and_authenticated_artifact(sandbox, monkeypatch, tmp_path):
    state = {'status': 'review_required', 'production_pass': False}
    submissions, downloads = [], []
    def handle(request):
        assert request.headers['Authorization'] == 'Bearer mock-studio-private-token'
        path = request.url.path
        if path.endswith('/capabilities'):
            return httpx.Response(200, json={'operations': ['generate_image', 'generate_video']})
        if path.endswith('/jobs'):
            submissions.append((request.headers['Idempotency-Key'], json.loads(request.content)))
            return httpx.Response(200, json={'job_id': 'remote-job', 'status': 'queued'})
        if path.endswith('/jobs/remote-job'):
            return httpx.Response(200, json={**state, 'job_id': 'remote-job', 'qc': {'visual': 'unassessed'}, 'artifacts': [{'artifact_id': 'approved', 'role': 'output', 'media_type': 'image/png', 'download_url': 'https://evil.invalid/steal-token'}]})
        if path.endswith('/artifacts/approved/content'):
            downloads.append(str(request.url))
            return httpx.Response(200, content=png(), headers={'Content-Type': 'image/png'})
        raise AssertionError(path)
    mock_http(monkeypatch, handle)
    cfg = EngineSettings(studio_base_url='http://studio.test/api/v1')
    path = tmp_path / 'approved.png'
    with pytest.raises(generation.ProviderFailure, match='Output excluded'):
        await studio.generate('image', 'prompt without truncation', path, cfg, segment=0)
    assert not path.exists() and not downloads
    body = submissions[0][1]
    assert body['consistency_sheet']['anchor'].endswith('END_OF_FULL_BIBLE')
    assert len(body['consistency_sheet']['anchor']) > 12000
    assert body['scene']['voiceover'] == 'Exact narration'
    assert body['character']['version'] == 'v001' and body['references']['environment_asset_id'] == 'river'
    assert body['lora_ids'] == ['dhruv-v1']
    state.update(status='succeeded', production_pass=False)
    with pytest.raises(generation.ProviderFailure):
        await studio.generate('image', 'prompt without truncation', path, cfg, segment=0)
    state['production_pass'] = True
    result = await studio.generate('image', 'prompt without truncation', path, cfg, segment=0)
    assert result['production_pass'] is True and path.exists()
    assert len(submissions) == 1 and downloads == ['http://studio.test/api/v1/artifacts/approved/content']


@pytest.mark.asyncio
async def test_studio_submission_timeout_reuses_idempotency_key(sandbox, monkeypatch, tmp_path):
    ids = []
    def handle(request):
        if request.url.path.endswith('/capabilities'):
            return httpx.Response(200, json={'operations': ['generate_image']})
        if request.url.path.endswith('/jobs'):
            ids.append(request.headers['Idempotency-Key'])
            raise httpx.ReadTimeout('unknown completion', request=request)
        raise AssertionError(str(request.url))
    mock_http(monkeypatch, handle)
    cfg = EngineSettings(studio_base_url='http://studio.test/api/v1')
    for _ in range(2):
        with pytest.raises(httpx.ReadTimeout):
            await studio.generate('image', 'same', tmp_path / 'out.png', cfg, segment=0)
    assert len(ids) == 2 and ids[0] == ids[1]
    assert await sandbox.studio_jobs.count_documents({}) == 1


@pytest.mark.asyncio
async def test_vault_wins_after_restart_and_clear(sandbox, monkeypatch):
    import server
    monkeypatch.setattr(server, 'db', sandbox)
    monkeypatch.setenv('GEMINI_API_KEY', 'stale-env-key')
    await sandbox.settings.insert_one({'key': 'api_keys', 'values': {'GEMINI_API_KEY': 'new-vault-key'}})
    await server._load_key_vault()
    assert gemini.gemini_key() == 'new-vault-key'
    await sandbox.settings.update_one({'key': 'api_keys'}, {'$set': {'values.GEMINI_API_KEY': ''}})
    await server._load_key_vault()
    assert gemini.gemini_key() == ''


def test_redacts_credentials(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'my-actual-test-key')
    safe = generation.redact('failed key=my-actual-test-key Bearer another-token ' + 'a' * 400)
    assert 'my-actual-test-key' not in safe and 'another-token' not in safe and 'a' * 400 not in safe