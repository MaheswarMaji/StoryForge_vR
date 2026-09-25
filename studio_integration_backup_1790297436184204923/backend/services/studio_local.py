"""Adapter for the local Studio still-image worker (studio_story_batch batch.py) on server27.

The worker has no HTTP API. StoryForge launches it as a subprocess (an argument array, never a shell
string), one process at a time, and reads <output>/batch_r001/response.json while it runs. Images
appear individually. COMPLETE only means "generated": acceptance stays with StoryForge's own review.
"""
import asyncio
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

from db import db
from services.generation import ProviderFailure, generation_context, redact

MAX_SCENES = 24
RELAUNCH_AFTER = 120  # seconds; a failed run is not relaunched by every waiting segment at once

_GLOBAL = asyncio.Lock()  # the worker rejects concurrent runs, so studio jobs are serialised here
_LAUNCHES: dict = {}


# ---------------------------------------------------------------- configuration
def paths():
    root = Path(os.environ.get('STUDIO_ROOT', '/home/siddhesh/studio'))
    python = Path(os.environ.get('STUDIO_PYTHON', str(root / 'runtime/envs/studio-v007/bin/python')))
    script = Path(os.environ.get('STUDIO_BATCH_SCRIPT', str(root / 'tools/studio_story_batch_v011/batch.py')))
    return root, python, script


def check():
    """(ok, message) — is the local worker installed and runnable by this account?"""
    root, python, script = paths()
    problems = []
    if not root.is_dir():
        problems.append(f'studio root not found: {root}')
    if not (python.is_file() and os.access(python, os.X_OK)):
        problems.append(f'worker Python not executable: {python}')
    if not script.is_file():
        problems.append(f'worker script not found: {script}')
    if not problems and not os.access(root / 'projects', os.W_OK) and (root / 'projects').exists():
        problems.append(f'no write access to {root / "projects"}')
    return (not problems), ('; '.join(problems) or f'Worker found at {script}')


def is_configured():
    return check()[0]


# ---------------------------------------------------------------- request building
def _slug(name, taken):
    base = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:24] or f'char{len(taken) + 1}'
    key, n = base, 2
    while key in taken:
        key, n = f'{base}_{n}', n + 1
    return key


_LINE = re.compile(r'^\s*(?:[-•*▪●]|\d+[.)])?\s*\**\s*([^:：\n]{1,40}?)\s*\**\s*(?::|：|\s[—–-]\s)\s*(.{15,})$')


def characters_from(story):
    """[(key, name, description)] from the story's characters, else from the consistency bible."""
    rows = []
    for c in story.get('characters') or []:
        if isinstance(c, dict) and (c.get('name') or '').strip() and (c.get('description') or '').strip():
            rows.append((c['name'].strip(), c['description'].strip()))
    if not rows:
        anchor = ((story.get('character_sheet') or {}).get('anchor') or '').strip()
        for line in anchor.splitlines():
            m = _LINE.match(line)
            if m:
                rows.append((m.group(1).strip(' *'), m.group(2).strip()))
        if not rows and anchor:
            rows = [('cast', anchor)]
    out, taken = [], set()
    for name, desc in rows[:6]:
        key = _slug(name, taken)
        taken.add(key)
        out.append((key, name, desc[:1500]))
    return out


def _mentions(text, name):
    text = text.lower()
    parts = {name.lower(), name.split()[0].lower()} if name.split() else {name.lower()}
    return any(p and p in text for p in parts)


def build_request(story, channel, project_id, prefs_refs=None):
    style = (channel.get('style_prefix') or story.get('visual_style') or 'Indian miniature painting style').strip()
    if '9:16' not in style:
        style += ', vertical 9:16'
    chars = characters_from(story)
    chunks = ((story.get('script') or {}).get('chunks') or [])[:MAX_SCENES]
    scenes = []
    for i, chunk in enumerate(chunks):
        text = ' '.join(str(chunk.get(k) or '') for k in ('visual', 'video_prompt', 'voiceover', 'source_label'))
        cast = [k for k, name, _ in chars if _mentions(text, name)] or [k for k, _, _ in chars]
        prompt = (chunk.get('video_prompt') or chunk.get('visual') or chunk.get('voiceover') or '').strip()
        scenes.append({'id': f'S{i + 1:03d}', 'cast': cast, 'image_prompt': prompt[:3000]})
    request = {'project_id': project_id, 'style': style[:600],
               'characters': {k: d for k, _, d in chars}, 'scenes': scenes}
    refs = _safe_references(prefs_refs or {}, {k for k, _, _ in chars})
    if refs:
        request['character_reference_paths'] = refs
    return request


def _safe_references(refs, keys):
    """Only image files inside the studio root or StoryForge's own media/storage may be handed to the worker."""
    root, _, _ = paths()
    from services import storage
    media_root = Path(__file__).resolve().parent.parent / 'media'  # backend/media, same as services.ocr.MEDIA_ROOT
    allowed = [root.resolve(), storage.STORAGE_ROOT.resolve(), media_root.resolve()]
    out = {}
    for key, value in refs.items():
        try:
            p = Path(str(value)).resolve()
        except Exception:
            continue
        if key in keys and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp') and p.is_file() \
                and any(a == p or a in p.parents for a in allowed):
            out[key] = str(p)
    return out


def _signature(request):
    body = json.dumps({k: request[k] for k in ('style', 'characters', 'scenes', 'character_reference_paths')
                       if k in request}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:10]


def _safe_id(value):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', str(value))[:60] or 'story'


# ---------------------------------------------------------------- process handling
class Launch:
    def __init__(self, argv, cwd, log_path):
        self.argv, self.cwd, self.log_path = argv, cwd, log_path
        self.proc = None
        self.returncode = None
        self.error = ''
        self.started_at = 0.0
        self.finished_at = 0.0
        self.started = asyncio.Event()
        self.done = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            async with _GLOBAL:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, 'ab') as log:
                    self.proc = await asyncio.create_subprocess_exec(
                        *self.argv, cwd=str(self.cwd), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log)
                    self.started_at = time.time()
                    self.started.set()
                    self.returncode = await self.proc.wait()
        except Exception as error:  # launching itself failed
            self.error = str(error)
        finally:
            self.finished_at = time.time()
            self.started.set()
            self.done.set()

    def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    def log_tail(self, lines=12):
        try:
            return '\n'.join(self.log_path.read_text(errors='replace').splitlines()[-lines:])
        except OSError:
            return ''


def _launch(key, argv, cwd, log_path):
    old = _LAUNCHES.get(key)
    if old and not old.done.is_set():
        return old
    if old and time.time() - old.finished_at < RELAUNCH_AFTER:
        return old  # just finished (or failed): every waiter sees the same result
    launch = Launch(argv, cwd, log_path)
    _LAUNCHES[key] = launch
    return launch


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _find_output(data, scene_id):
    for out in (data or {}).get('outputs') or []:
        if out.get('scene_id') == scene_id and out.get('status') == 'COMPLETE' and out.get('path') \
                and Path(out['path']).is_file():
            return out
    return None


async def _cancelled(ctx):
    job_id = ctx.get('job_id')
    if not job_id:
        return False
    from job_queue import is_cancelled
    local = await db.jobs.find_one({'_id': job_id}, {'status': 1})
    return bool(is_cancelled(job_id) or (local or {}).get('status') == 'cancelled')


async def _wait_for(launch, response_path, scene_id, config, ctx):
    poll, stall = max(1, config.studio_poll_seconds), config.studio_timeout_seconds
    seen, last_progress = -1, time.monotonic()
    while True:
        if await _cancelled(ctx):
            from job_queue import JobCancelled
            launch.stop()
            raise JobCancelled()
        data = _read_json(response_path)
        found = _find_output(data, scene_id)
        if found:
            return found
        fresh = bool(data) and _mtime(response_path) >= launch.started_at - 2
        if launch.done.is_set() or (fresh and (data or {}).get('status') == 'FAILED'):
            data = _read_json(response_path)
            found = _find_output(data, scene_id)
            if found:
                return found
            errors = '; '.join(str(e)[:300] for e in ((data or {}).get('errors') or [])[:3])
            raise ProviderFailure(
                f'Studio did not produce scene {scene_id}. {errors or launch.error or launch.log_tail() or "No details in response.json."}',
                code='STUDIO_FAILED', action=f'Check the worker log: {launch.log_path}')
        progress = len((data or {}).get('outputs') or [])
        if progress != seen or not launch.started.is_set():
            seen, last_progress = progress, time.monotonic()
        if time.monotonic() - last_progress > stall:
            launch.stop()
            raise ProviderFailure(f'No new Studio output for {stall}s (scene {scene_id}). Retrying resumes the recorded work.',
                                  code='POLL_TIMEOUT')
        await asyncio.sleep(poll)


# ---------------------------------------------------------------- delivery + records
async def _deliver(output, out_path):
    root, _, _ = paths()
    src = Path(output['path']).resolve()
    if root.resolve() not in src.parents:
        raise ProviderFailure('Studio returned an image path outside the studio root; refusing to read it.', code='INVALID_ARTIFACT')
    data = src.read_bytes()
    if output.get('sha256') and hashlib.sha256(data).hexdigest() != output['sha256']:
        raise ProviderFailure('Studio image checksum does not match response.json.', code='INVALID_ARTIFACT')
    from PIL import Image
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception as error:
        raise ProviderFailure('Studio returned an invalid PNG.', code='INVALID_ARTIFACT') from error
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')
    tmp.write_bytes(data)
    tmp.replace(dest)


async def _record(story_id, sig, scene_id, segment, status, run, revision, error='', qc=None):
    _id = f'{sig}:{scene_id}:{revision}'
    await db.studio_jobs.update_one({'_id': _id}, {'$set': {
        'id': _id, 'job_id': f'{run}/{revision}', 'story_id': story_id, 'segment': segment,
        'operation': 'generate_image', 'status': status, 'production_pass': status == 'succeeded',
        'qc': qc or {}, 'error': redact(error)[:1500], 'base_url': 'local-worker', 'created_at': time.time()}}, upsert=True)


# ---------------------------------------------------------------- public entry point
async def generate_image(prompt, out_path, config, segment=None):
    ok, message = check()
    if not ok:
        raise ProviderFailure(f'Studio worker is not available: {message}', code='NOT_CONFIGURED',
                              action='Check STUDIO_ROOT / STUDIO_PYTHON / STUDIO_BATCH_SCRIPT in backend/.env.')
    root, python, script = paths()
    ctx = generation_context.get()
    story_id = ctx.get('story_id', '')
    story = await db.stories.find_one({'_id': story_id}) or {}
    channel = await db.channels.find_one({'_id': story.get('channel_id')}) or {}
    prefs_doc = await db.story_engines.find_one({'_id': story_id}) or {}
    refs = ((prefs_doc.get('studio') or {}).get('references')) or {}
    project = f'storyforge_{_safe_id(story_id)}'
    base = root / 'projects' / project / 'stills_v011'
    request = build_request(story, channel, project, refs)
    if not request['scenes']:
        raise ProviderFailure('The story has no scenes to send to Studio.', code='NO_SCENES')

    if segment is None:
        # Character sheet / thumbnail: one extra image, using the prompt StoryForge built for it.
        key = hashlib.sha256((prompt + json.dumps(request['characters'], sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:10]
        request = {**request, 'scenes': [{'id': 'S000', 'cast': [k for k in request['characters']],
                                           'image_prompt': prompt[:3000]}]}
        sig, scene_id, run_dir = f'single_{key}', 'S000', base / f'single_{key}'
    else:
        if segment >= len(request['scenes']):
            raise ProviderFailure('Segment is outside the first 24 scenes Studio accepts.', code='NO_SCENES')
        sig = _signature(request)
        scene_id, run_dir = f'S{segment + 1:03d}', base / f'run_{sig}'

    request_path = base / f'request_{sig}.json'
    request_path.parent.mkdir(parents=True, exist_ok=True)
    if not request_path.exists():  # never change the story JSON inside a run
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')

    state_id = f'{story_id}:{sig}'
    state = await db.studio_runs.find_one({'_id': state_id}) or {}
    revision = (state.get('scenes') or {}).get(scene_id, 'r001')
    regenerate = segment is not None and ctx.get('regenerate') == segment
    batch_response = run_dir / 'batch_r001' / 'response.json'
    batch_argv = [str(python), str(script), '--story', str(request_path), '--output', str(run_dir)]
    current = batch_response if revision == 'r001' else run_dir / f'{scene_id}_{revision}' / 'response.json'
    if regenerate and not _find_output(_read_json(current), scene_id):
        regenerate = False  # nothing from Studio to revise yet (e.g. earlier frames came from another engine)
    if regenerate:
        parent, revision = revision, f'r{int(revision[1:]) + 1:03d}'
        feedback = (ctx.get('feedback') or '').strip() or \
            'Regenerate this scene with a fresh composition. Keep the same characters, costumes, art style and setting.'
        feedback_path = run_dir / f'feedback_{scene_id}_{revision}.txt'
        run_dir.mkdir(parents=True, exist_ok=True)
        feedback_path.write_text(feedback, encoding='utf-8')
        argv = [str(python), str(script), '--story', str(request_path), '--output', str(run_dir), '--scene', scene_id,
                '--revision', revision, '--parent-revision', parent, '--feedback-file', str(feedback_path)]
        response_path = run_dir / f'{scene_id}_{revision}' / 'response.json'
        launch_key = (story_id, sig, scene_id, revision)
    else:
        argv, response_path, launch_key = batch_argv, batch_response, (story_id, sig)
        if revision != 'r001':  # a regenerated scene: its latest revision is the current image
            latest = run_dir / f'{scene_id}_{revision}' / 'response.json'
            if _find_output(_read_json(latest), scene_id):
                response_path = latest
            else:
                revision = 'r001'

    output = _find_output(_read_json(response_path), scene_id)  # already generated: no new worker run
    try:
        if output is None:
            await _record(story_id, sig, scene_id, segment, 'running', run_dir.name, revision)
            launch = _launch(launch_key, argv, root, run_dir / 'storyforge_worker.log')
            output = await _wait_for(launch, response_path, scene_id, config, ctx)
        await _deliver(output, out_path)
    except Exception as error:
        await _record(story_id, sig, scene_id, segment, 'failed', run_dir.name, revision, error=str(error))
        raise
    await db.studio_runs.update_one({'_id': state_id}, {'$set': {f'scenes.{scene_id}': revision, 'story_id': story_id}}, upsert=True)
    await _record(story_id, sig, scene_id, segment, 'succeeded', run_dir.name, revision,
                  qc={'qc_status': output.get('qc_status', ''), 'sha256': output.get('sha256', ''),
                      'size': f"{output.get('width', '?')}x{output.get('height', '?')}"})
    return {'provider': 'studio', 'job_id': f'{run_dir.name}/{scene_id}/{revision}', 'production_pass': True}


async def self_test(timeout=60):
    """Run `batch.py --help` to prove the interpreter and script start (no GPU work)."""
    root, python, script = paths()
    proc = await asyncio.create_subprocess_exec(
        str(python), str(script), '--help', cwd=str(root), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProviderFailure(f'batch.py --help did not finish within {timeout}s', code='POLL_TIMEOUT')
    text = out.decode(errors='replace')
    if proc.returncode != 0:
        raise ProviderFailure(f'batch.py --help exited with {proc.returncode}: {text[-400:]}', code='WORKER_ERROR')
    return text[:1500]
