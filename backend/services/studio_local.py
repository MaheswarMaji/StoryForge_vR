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
import fcntl
import sqlite3
from contextlib import contextmanager
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
    script = Path(os.environ.get('STUDIO_BATCH_SCRIPT', str(root / 'tools/studio_story_batch_v0112/batch.py')))
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
            raise ProviderFailure('Supply separate named character definitions; a combined cast cannot be one identity.', code='INVALID_REQUEST')
    out, taken = [], set()
    if len(rows)>12:raise ProviderFailure('Studio supports at most 12 character definitions.', code='INVALID_REQUEST')
    for name, desc in rows:
        key = _slug(name, taken)
        taken.add(key)
        out.append((key, name, desc[:1500]))
    return out


def _mentions(text, name):
    text = text.lower()
    parts = {name.lower(), name.split()[0].lower()} if name.split() else {name.lower()}
    return any(p and re.search(r'(?<!\w)'+re.escape(p)+r'(?!\w)',text) for p in parts)


def build_request(story, channel, project_id, prefs_refs=None, allow_unresolved=False):
    style = (channel.get('style_prefix') or story.get('visual_style') or 'Indian miniature painting style').strip()
    if '9:16' not in style:
        style += ', vertical 9:16'
    chars = characters_from(story)
    chunks = ((story.get('script') or {}).get('chunks') or [])
    if len(chunks)>MAX_SCENES:raise ProviderFailure('At most 24 scenes supported by this adapter.', code='INVALID_REQUEST')
    if not chars:raise ProviderFailure('Named character descriptions are required.', code='INVALID_REQUEST')
    scenes = []
    for i, chunk in enumerate(chunks):
        text = ' '.join(str(chunk.get(k) or '') for k in ('image_prompt', 'visual', 'video_prompt', 'voiceover', 'source_label'))
        declared=chunk.get('cast') or chunk.get('character_ids')
        if declared is not None and (not isinstance(declared,list) or any(not isinstance(x,str) for x in declared)):
            raise ProviderFailure('Scene cast must be a list of names or IDs.', code='INVALID_REQUEST')
        def aliases(key,name):
            found=[name,key]
            for item in story.get('characters') or []:
                if isinstance(item,dict) and item.get('name')==name:
                    extra=item.get('aliases',[])
                    if isinstance(extra,list):found += [x for x in extra if isinstance(x,str)]
            bilingual={'ध्रुव':['Dhruv','Dhruva'],'सुरुचि':['Suruchi'],'सुनीति':['Suniti','Sunithi'],'नारद':['Narada','Narad'],'विष्णु':['Vishnu'],'उत्तानपाद':['Uttanapada']}
            found+=bilingual.get(name,[])
            return found
        if declared:
            cast=[]
            for item in declared:
                matched=[k for k,name,_ in chars if any(item.casefold()==a.casefold() for a in aliases(k,name))]
                if len(matched)!=1:raise ProviderFailure('Unknown or ambiguous scene character: '+item,code='INVALID_REQUEST')
                if matched[0] not in cast:cast.append(matched[0])
        else:
            cast=[k for k,name,_ in chars if any(_mentions(text,a) for a in aliases(k,name))]
        if not 1<=len(cast)<=4 and (declared or not allow_unresolved):
            raise ProviderFailure(f'Scene {i+1} needs an explicit cast of 1..4 characters; no entire-cast fallback is used.',code='INVALID_REQUEST')
        prompt = (chunk.get('image_prompt') or chunk.get('visual') or '').strip()
        if not prompt:raise ProviderFailure('Scene needs an image_prompt or visual description.',code='INVALID_REQUEST')
        scenes.append({'id': f'S{i + 1:03d}', 'cast': cast, 'image_prompt': prompt[:3000]})
    request = {'project_id': project_id, 'style': style[:600],
               'characters': {k: d for k, _, d in chars}, 'scenes': scenes}
    refs = _safe_references(prefs_refs or {}, {k for k, _, _ in chars})
    if refs:
        request['character_reference_paths'] = refs
    return request



# One cached, CPU-only director call resolves all unmatched scenes in a story.
def validate_cast_resolution(answer,needed,known):
    if not isinstance(answer,dict) or not isinstance(answer.get('casts'),dict) or set(answer['casts'])!=set(needed):
        raise ProviderFailure('Director did not resolve exactly the requested scenes.',code='CAST_RESOLUTION_FAILED')
    for scene,cast in answer['casts'].items():
        if not isinstance(cast,list) or not 1<=len(cast)<=4 or any(not isinstance(k,str) or k not in known for k in cast) or len(set(cast))!=len(cast):
            raise ProviderFailure('Director could not map scene '+scene+' to 1..4 existing characters. A missing character or a character-free scene needs an explicit supported request.',code='CAST_RESOLUTION_FAILED')
    return answer['casts']

def _cast_api(path,payload=None):
    import urllib.request
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request('http://127.0.0.1:11434'+path,data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=240) as response:return json.load(response)

def _cast_cpu_check():
    data=_cast_api('/api/ps')
    models=data.get('models')
    if not isinstance(models,list):raise ProviderFailure('Cannot verify Ollama CPU placement.',code='DIRECTOR_UNAVAILABLE')
    for model in models:
        if type(model.get('size_vram')) not in (int,float) or model['size_vram']!=0:
            raise ProviderFailure('Ollama has GPU or unknown placement; keep studio LLMs on CPU.',code='DIRECTOR_UNAVAILABLE')

def _resolve_cast_cached(payload,cache_dir):
    model=os.environ.get('STUDIO_DIRECTOR_MODEL','deepseek-r1:32b')
    if 'cloud' in model.lower():raise ProviderFailure('Local director model required.',code='DIRECTOR_UNAVAILABLE')
    key=hashlib.sha256(json.dumps({'version':113,'model':model,'input':payload},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    cache_dir.mkdir(parents=True,exist_ok=True);path=cache_dir/(key+'.json')
    with locked_file(cache_dir/(key+'.lock')):
        if path.exists():
            saved=json.loads(path.read_text())
            if saved.get('status')=='COMPLETE':return validate_cast_resolution(saved['answer'],payload['scenes'],payload['characters'])
            raise ProviderFailure('Previous cast resolution failed or was interrupted; see '+str(path),code='CAST_RESOLUTION_FAILED')
        record={'status':'RUNNING','model':model,'input':payload}
        def save():
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(record,ensure_ascii=False,indent=2));os.replace(temp,path)
        save()
        try:
            _cast_cpu_check()
            prompt=('Resolve the visible cast of each story illustration, including pronouns, titles and Hindi/English name variants. '
                'Return only JSON {"casts":{"SCENE_ID":["character_id"]}}. Use ONLY character IDs supplied below. '
                'Select only people physically depicted by the image description, not everyone mentioned in dialogue. '
                'Do not change the story, add identities, or include the entire cast as fallback. '
                'If no listed character is depicted or resolution is impossible return an empty list for that scene. '
                'There are at most four people per supported scene. Data: '+json.dumps(payload,ensure_ascii=False))
            result=_cast_api('/api/chat',{'model':model,'stream':False,'format':'json','keep_alive':'10m',
                'messages':[{'role':'user','content':prompt}],
                'options':{'num_gpu':0,'num_ctx':8192,'num_predict':1600,'temperature':0}})
            _cast_cpu_check()
            if result.get('done') is not True or result.get('done_reason')=='length':raise ValueError('Incomplete director response')
            answer=json.loads(result.get('message',{}).get('content',''))
            casts=validate_cast_resolution(answer,payload['scenes'],payload['characters'])
            record.update(status='COMPLETE',answer=answer);save();return casts
        except Exception as e:
            record.update(status='FAILED',error=str(e));save()
            raise ProviderFailure('CPU director cast resolution failed: '+str(e),code='CAST_RESOLUTION_FAILED') from e

async def resolved_request(story,channel,project,refs,base):
    request=build_request(story,channel,project,refs,allow_unresolved=True)
    needed={s['id']:s['image_prompt'] for s in request['scenes'] if not 1<=len(s['cast'])<=4}
    if not needed:return request
    payload={'characters':{key:{'name':name,'description':description} for key,name,description in characters_from(story)},'scenes':needed}
    casts=await asyncio.to_thread(_resolve_cast_cached,payload,base/'cast_resolution')
    for scene in request['scenes']:
        if scene['id'] in casts:scene['cast']=casts[scene['id']]
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
    data={k:request[k] for k in ('style','characters','scenes','character_reference_paths') if k in request}
    data['reference_hashes']={k:hashlib.sha256(Path(v).read_bytes()).hexdigest() for k,v in request.get('character_reference_paths',{}).items()}
    data['worker_path']=str(paths()[2])
    body=json.dumps(data,ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:10]


def _safe_id(value):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', str(value))[:40] or 'story'


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
                        *self.argv, cwd=str(self.cwd), env=dict(os.environ, CUDA_VISIBLE_DEVICES="0"), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
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
    if old and old.returncode not in (0,None):
        return old  # failed launch retained; explicit recovery required, never timed replay
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
            # Cancel this consumer, not a shared batch or an already-submitted GPU job.
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
        progress = (len((data or {}).get('outputs') or []), _mtime(Path(response_path).parent/'progress.json'), _mtime(launch.log_path), _mtime(Path(response_path).parent.parent/'queue_progress.json'))
        if progress != seen or not launch.started.is_set():
            seen, last_progress = progress, time.monotonic()
        if time.monotonic() - last_progress > stall:
            raise ProviderFailure(f'No Studio progress for {stall}s (scene {scene_id}). Worker left running; retry will reattach to recorded work.',
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
    _id = f'{story_id}:{sig}:{scene_id}:{revision}'
    await db.studio_jobs.update_one({'_id': _id}, {'$set': {
        'id': _id, 'job_id': f'{run}/{revision}', 'story_id': story_id, 'segment': segment,
        'operation': 'generate_image', 'status': status, 'production_pass': False,
        'qc': qc or {}, 'error': redact(error)[:1500], 'base_url': 'local-worker', 'created_at': time.time()}}, upsert=True)


@contextmanager
def locked_file(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        yield

def reserve_revision(base,state_id,scene_id,parent,request_id,feedback):
    if not request_id:raise ProviderFailure('Regeneration requires a persistent job ID.',code='INVALID_REQUEST')
    con=sqlite3.connect(str(base/'revision_reservations.sqlite'),timeout=30)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS reservations (story TEXT, scene TEXT, request TEXT, revision TEXT, parent TEXT, feedback TEXT, PRIMARY KEY(story,scene,request), UNIQUE(story,scene,revision))')
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT revision,parent,feedback FROM reservations WHERE story=? AND scene=? AND request=?',(state_id,scene_id,request_id)).fetchone()
        if row:
            if row[2]!=feedback:raise ProviderFailure('Same regeneration job has changed comments.',code='INVALID_REQUEST')
            con.commit();return row[1],row[0]
        nums=[int(x[0][1:]) for x in con.execute('SELECT revision FROM reservations WHERE story=? AND scene=?',(state_id,scene_id))]
        number=max([int(parent[1:])]+nums)+1
        if number>999:raise ProviderFailure('Revision limit reached; create a new run.',code='INVALID_REQUEST')
        revision='r%03d'%number
        con.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?)',(state_id,scene_id,request_id,revision,parent,feedback));con.commit()
        return parent,revision
    finally:con.close()


def _character_sheet_artifact(run_dir,request):
    from PIL import Image,ImageOps,ImageDraw
    entries=[]
    for key in request['characters']:
        p=run_dir/'characters'/key/'candidate.png'
        record=_read_json(p.parent/'asset.json') or {}
        if not p.is_file() or record.get('sha256')!=hashlib.sha256(p.read_bytes()).hexdigest():
            raise ProviderFailure('Character reference unavailable or changed: '+key,code='INVALID_ARTIFACT')
        entries.append((key,p))
    columns=min(3,len(entries));rows=(len(entries)+columns-1)//columns
    canvas=Image.new('RGB',(columns*384,rows*704),(236,228,207));draw=ImageDraw.Draw(canvas)
    for index,(key,p) in enumerate(entries):
        with Image.open(p) as im:pic=ImageOps.contain(im.convert('RGB'),(374,660))
        x=index%columns*384;y=index//columns*704
        canvas.paste(pic,(x+(384-pic.width)//2,y+30));draw.text((x+8,y+8),key,fill='black')
    dest=run_dir/'storyforge_character_sheet.png'
    with locked_file(dest.with_suffix('.lock')):
        temp=dest.with_suffix('.tmp.png');canvas.save(temp);os.replace(temp,dest)
    return {'path':str(dest),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'width':canvas.width,'height':canvas.height,'qc_status':'NOT_RUN'}

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
    request = await resolved_request(story, channel, project, refs, base)
    if not request['scenes']:
        raise ProviderFailure('The story has no scenes to send to Studio.', code='NO_SCENES')

    auxiliary=None
    if segment is None:
        # Pipeline passes character sheets under media/char and thumbnails under media/tmp.
        # Reuse the canonical story batch instead of bootstrapping a second cast/run.
        if Path(out_path).parent.name=='char':auxiliary='character_sheet'
        elif 'thumb' in Path(out_path).name.lower():auxiliary='thumbnail'
        else:raise ProviderFailure('Unrecognized auxiliary image request; expected character sheet or thumbnail.',code='INVALID_REQUEST')
        scene_id=request['scenes'][0]['id']
    else:
        if type(segment) is not int or segment<0 or segment>=len(request['scenes']):
            raise ProviderFailure('Segment index outside the story.',code='NO_SCENES')
        scene_id=f'S{segment+1:03d}'
    sig=_signature(request)
    run_dir=base/f'run_{sig}'
    record_scene_id='CHARACTERS' if auxiliary=='character_sheet' else 'THUMBNAIL' if auxiliary else scene_id

    request_path = base / f'request_{sig}.json'
    request_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(request_path.with_suffix('.lock')):
        if request_path.exists() and json.loads(request_path.read_text())!=request:raise ProviderFailure('Request content changed inside an existing run.',code='INVALID_REQUEST')
        if not request_path.exists():request_path.write_text(json.dumps(request,ensure_ascii=False,indent=2),encoding='utf-8')

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
        feedback = (ctx.get('feedback') or '').strip() or \
            'Regenerate this scene with a fresh composition. Keep the same characters, costumes, art style and setting.'
        parent,revision=reserve_revision(base,state_id,scene_id,revision,str(ctx.get('job_id') or ''),feedback)
        feedback_path = run_dir / f'feedback_{scene_id}_{revision}.txt'
        run_dir.mkdir(parents=True, exist_ok=True)
        with locked_file(feedback_path.with_suffix('.lock')):
            if feedback_path.exists() and feedback_path.read_text()!=feedback:raise ProviderFailure('Reserved revision feedback changed.',code='INVALID_REQUEST')
            if not feedback_path.exists():feedback_path.write_text(feedback,encoding='utf-8')
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
            await _record(story_id, sig, record_scene_id, segment, 'running', run_dir.name, revision)
            launch = _launch(launch_key, argv, root, run_dir / 'storyforge_worker.log')
            output = await _wait_for(launch, response_path, scene_id, config, ctx)
        if auxiliary=='character_sheet':output=_character_sheet_artifact(run_dir,request)
        await _deliver(output, out_path)
    except Exception as error:
        await _record(story_id, sig, record_scene_id, segment, 'failed', run_dir.name, revision, error=str(error))
        raise
    await db.studio_runs.update_one({'_id': state_id}, {'$max': {f'scenes.{scene_id}': revision}, '$set': {'story_id': story_id}}, upsert=True)
    await _record(story_id, sig, record_scene_id, segment, 'succeeded', run_dir.name, revision,
                  qc={'qc_status': output.get('qc_status', ''), 'sha256': output.get('sha256', ''),
                      'size': f"{output.get('width', '?')}x{output.get('height', '?')}"})
    return {'provider': 'studio', 'job_id': f'{run_dir.name}/{scene_id}/{revision}', 'production_pass': False, 'generation_complete': True, 'acceptance_owner':'storyforge'}


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
