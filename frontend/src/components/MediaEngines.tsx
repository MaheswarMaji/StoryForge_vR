import { useState, type ComponentType, type ButtonHTMLAttributes } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Cpu, Image, Video, Server, ShieldCheck, Activity, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { Button as BaseButton } from './ui/button';
import { apiGet, apiPost, apiPut } from '../lib/api';
import type { EngineChoice, EngineOption, EngineSettings, EngineSettingsView, GenerationEvent, StoryEngines, StoryEnginesView, ConnectionResult, StudioJobView, StudioBinding } from '../lib/engine-types';

const Button = BaseButton as ComponentType<ButtonHTMLAttributes<HTMLButtonElement> & { variant?: string; size?: string }>;
const card = 'rounded-2xl border border-cyan-500/20 bg-[#101720] p-5 sm:p-6';
const input = 'w-full rounded-lg border border-white/15 bg-[#090d14] px-3 py-2.5 text-xs text-slate-200 outline-none transition-colors focus:border-cyan-400 focus:ring-1 focus:ring-cyan-400 disabled:opacity-50';
const action = 'border border-white/15 bg-white/5 text-slate-200 transition-colors hover:bg-white/10';
const primary = 'bg-cyan-300 font-semibold text-slate-950 transition-colors hover:bg-cyan-200';
const errorToast = (e: Error) => toast.error(e.message);
const emptyBinding: StudioBinding = { character: {}, references: {}, direction: {}, lora_ids: [] };

function EngineSelect({ kind, options, value, onChange, prefix, inherited, disabled }: {
  kind: 'image' | 'video'; options: EngineOption[]; value: string | null; onChange: (value: string | null) => void;
  prefix: string; inherited?: string; disabled?: boolean;
}) {
  const selected = options.find(o => o.id === (value || inherited));
  return <div className="min-w-0 flex-1">
    <label data-testid={`${prefix}-${kind}-label`} htmlFor={`${prefix}-${kind}`} className="mb-2 flex items-center gap-2 text-xs font-semibold text-slate-300">
      {kind === 'image' ? <Image size={14} className="text-cyan-300" /> : <Video size={14} className="text-amber-300" />}
      {kind === 'image' ? 'Image engine' : 'Video clip engine'}
    </label>
    <select id={`${prefix}-${kind}`} data-testid={`${prefix}-${kind}-engine`} className={input} value={value || ''} disabled={disabled} onChange={e => onChange(e.target.value || null)}>
      {inherited && <option value="" data-testid={`${prefix}-${kind}-inherit-option`} children={`Inherit · ${options.find(o => o.id === inherited)?.label || inherited}`} />}
      {options.map(o => <option key={o.id} value={o.id} data-testid={`${prefix}-${kind}-${o.id.replaceAll('_', '-')}-option`} children={`${o.label}${!o.configured ? ' · not configured' : ''}`} />)}
    </select>
    {selected && <p data-testid={`${prefix}-${kind}-note`} className={`mt-2 text-[11px] leading-relaxed ${!selected.configured || !selected.reference_aware ? 'text-amber-300' : 'text-slate-500'}`}>
      {!selected.configured ? 'Not configured. ' : ''}{selected.note}
    </p>}
  </div>;
}

export function EngineSettingsCard() {
  const query = useQuery({ queryKey: ['media-engines'], queryFn: () => apiGet<EngineSettingsView>('/api/settings/media-engines') });
  if (query.error) return <p data-testid="engine-settings-error" className="text-rose-300">{query.error.message}</p>;
  if (!query.data) return <p data-testid="engine-settings-loading" className="text-slate-500">Loading media engines…</p>;
  return <SettingsForm key={JSON.stringify(query.data)} initial={query.data} />;
}

function SettingsForm({ initial }: { initial: EngineSettingsView }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<EngineSettings>(initial);
  const [connection, setConnection] = useState<ConnectionResult | null>(null);
  const save = useMutation({ mutationFn: () => apiPut<EngineSettingsView>('/api/settings/media-engines', draft), onSuccess: async () => {
    await client.invalidateQueries({ queryKey: ['media-engines'] });
    await client.invalidateQueries({ queryKey: ['story-engines'] });
    toast.success('Default media engines saved');
  }, onError: errorToast });
  const test = useMutation({ mutationFn: () => apiPost<ConnectionResult>('/api/settings/studio/test'), onSuccess: setConnection, onError: errorToast });
  const diagnostic = useMutation({ mutationFn: (generate: boolean) => apiPost<GenerationEvent>('/api/settings/media-engines/test-gemini', { generate_sample: generate }), onSuccess: async result => {
    await client.invalidateQueries({ queryKey: ['generation-events'] });
    if (result.status === 'failed') toast.error('Gemini check failed — see the exact reason in Diagnostics below');
    else toast.success(result.message);
  }, onError: errorToast });
  return <section data-testid="media-engine-settings-card" className={card}>
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><h2 data-testid="media-engine-settings-title" className="flex items-center gap-2 font-display text-xl font-semibold text-cyan-100"><Cpu size={19} className="text-cyan-300" /> Media Engines</h2>
        <p data-testid="media-engine-settings-description" className="mt-1 text-xs leading-relaxed text-slate-400">Your images. Your motion. Choose which engine does each job.</p></div>
      <Button data-testid="save-media-engine-defaults" onClick={() => save.mutate()} disabled={save.isPending} className={primary}>{save.isPending ? 'Saving…' : 'Save defaults'}</Button>
    </div>
    <div className="mt-6 grid gap-5 sm:grid-cols-2">
      <EngineSelect prefix="default" kind="image" options={initial.image_options} value={draft.image} onChange={v => setDraft({ ...draft, image: v as EngineSettings['image'] })} />
      <EngineSelect prefix="default" kind="video" options={initial.video_options} value={draft.video} onChange={v => setDraft({ ...draft, video: v as EngineSettings['video'] })} />
    </div>
    <div data-testid="engine-routing-policy" className="mt-5 flex items-start gap-2 rounded-lg border border-emerald-500/15 bg-emerald-500/5 p-3 text-xs leading-relaxed text-emerald-200/80"><ShieldCheck size={16} className="mt-0.5 shrink-0" />A selected engine never silently switches providers. Auto may fall back and records every attempt. Story and segment choices override these defaults; cached media stays until regenerated.</div>
    <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-white/10 pt-5">
      <Button data-testid="check-gemini-key" variant="outline" className={action} disabled={diagnostic.isPending} onClick={() => diagnostic.mutate(false)}>Check Gemini key</Button>
      <Button data-testid="test-gemini-image" variant="outline" className={action} disabled={diagnostic.isPending} onClick={() => diagnostic.mutate(true)}>{diagnostic.isPending ? 'Checking…' : 'Test Gemini image'}</Button>
      <p data-testid="gemini-test-cost-note" className="text-[11px] text-slate-500">Key check is free. Image test makes one small generation request and may incur provider charges.</p>
    </div>
    <div className="mt-6 border-t border-white/10 pt-5">
      <div className="flex flex-wrap items-center gap-3"><h3 data-testid="studio-connection-title" className="flex items-center gap-2 text-sm font-semibold text-slate-200"><Server size={16} className="text-cyan-300" /> Your Studio · server27</h3>
        <span data-testid="studio-connection-status" className="rounded-full border border-amber-500/25 bg-amber-500/5 px-2.5 py-1 text-[10px] text-amber-300">{connection?.status === 'connected' ? 'Worker ready' : initial.image_options.find(o => o.id === 'studio')?.configured ? 'Worker found · not verified' : 'Not configured'}</span></div>
      <p data-testid="studio-connection-help" className="mt-3 text-xs leading-relaxed text-slate-400">Studio runs as a local worker on server27 (no HTTP API): StoryForge starts it for each story, one job at a time, and collects the finished images. It makes still images only (576×1024). The paths come from STUDIO_ROOT, STUDIO_PYTHON and STUDIO_BATCH_SCRIPT in the backend .env.</p>
      <div className="mt-3 flex flex-wrap gap-4">
        <label data-testid="studio-poll-label" className="text-xs text-slate-400">Poll interval (seconds)<input data-testid="studio-poll-seconds" type="number" min={1} max={60} value={draft.studio_poll_seconds} onChange={e => setDraft({ ...draft, studio_poll_seconds: Number(e.target.value) })} className={`${input} mt-1 max-w-36`} /></label>
        <label data-testid="studio-timeout-label" className="text-xs text-slate-400">Stall timeout (seconds without a new image)<input data-testid="studio-timeout-seconds" type="number" min={30} max={7200} value={draft.studio_timeout_seconds} onChange={e => setDraft({ ...draft, studio_timeout_seconds: Number(e.target.value) })} className={`${input} mt-1 max-w-36`} /></label>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3"><Button data-testid="test-studio-connection" variant="outline" className={action} disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending ? 'Testing…' : 'Test worker'}</Button><p data-testid="studio-qc-policy" className="text-[11px] text-slate-500">Generated ≠ approved: review every scene in StoryForge before publishing.</p></div>
      {connection && <p data-testid="studio-connection-result" className={`mt-3 whitespace-pre-wrap text-xs ${connection.status === 'connected' ? 'text-emerald-300' : 'text-amber-300'}`}>{connection.message}</p>}
    </div>
  </section>;
}

export function StoryEngineControls({ storyId, segment, disabled = false }: { storyId: string; segment?: number; disabled?: boolean }) {
  const defaults = useQuery({ queryKey: ['media-engines'], queryFn: () => apiGet<EngineSettingsView>('/api/settings/media-engines') });
  const story = useQuery({ queryKey: ['story-engines', storyId], queryFn: () => apiGet<StoryEnginesView>(`/api/stories/${storyId}/media-engines`) });
  if (!defaults.data || !story.data) return null;
  return <StoryEngineForm key={JSON.stringify(story.data)} storyId={storyId} segment={segment} disabled={disabled} defaults={defaults.data} initial={story.data} />;
}

function StoryEngineForm({ storyId, segment, disabled, defaults, initial }: { storyId: string; segment?: number; disabled: boolean; defaults: EngineSettingsView; initial: StoryEnginesView }) {
  const client = useQueryClient();
  const prefix = segment === undefined ? 'story' : `segment-${segment}`;
  const [choice, setChoice] = useState<EngineChoice>(segment === undefined ? { image: initial.image, video: initial.video } : initial.segments[String(segment)] || { image: null, video: null });
  const save = useMutation({ mutationFn: () => {
    const body: StoryEngines = segment === undefined ? { ...initial, ...choice } : { ...initial, segments: { ...initial.segments, [String(segment)]: choice } };
    return apiPut<StoryEnginesView>(`/api/stories/${storyId}/media-engines`, body);
  }, onSuccess: async () => { await client.invalidateQueries({ queryKey: ['story-engines', storyId] }); toast.success('Engine choice saved — used on the next generation'); }, onError: errorToast });
  return <section data-testid={`${prefix}-engines-card`} className={segment === undefined ? card : 'mt-4 rounded-xl border border-white/10 bg-black/20 p-4'}>
    {segment === undefined && <h3 data-testid="story-engines-title" className="mb-4 flex items-center gap-2 font-display text-lg font-semibold text-cyan-100"><Cpu size={17} /> Engines for this story</h3>}
    <div className="flex flex-wrap items-start gap-4">
      <EngineSelect prefix={prefix} kind="image" options={defaults.image_options} value={choice.image} inherited={segment === undefined ? defaults.image : initial.effective.image || defaults.image} disabled={disabled} onChange={v => setChoice({ ...choice, image: v as EngineChoice['image'] })} />
      <EngineSelect prefix={prefix} kind="video" options={defaults.video_options} value={choice.video} inherited={segment === undefined ? defaults.video : initial.effective.video || defaults.video} disabled={disabled} onChange={v => setChoice({ ...choice, video: v as EngineChoice['video'] })} />
      <Button data-testid={`${prefix}-save-engines`} variant="outline" className={`${action} mt-6`} disabled={disabled || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save choice'}</Button>
    </div>
    {segment === undefined && <p data-testid="story-engine-help" className="mt-3 text-[11px] leading-relaxed text-slate-500">Video engines apply in clip mode. Storyboard uses one story-level image engine for the full contact sheet; segment choices apply to individual regeneration. Changing engines does not discard already-approved media.</p>}
  </section>;
}

export function GenerationDiagnostics({ storyId = '' }: { storyId?: string }) {
  const prefix = storyId ? 'story-diagnostics' : 'settings-diagnostics';
  const query = useQuery({ queryKey: ['generation-events', storyId], queryFn: () => apiGet<GenerationEvent[]>(`/api/generation-events${storyId ? `?story_id=${encodeURIComponent(storyId)}` : ''}`), refetchInterval: 5000 });
  return <section data-testid={prefix} className="rounded-2xl border border-white/10 bg-[#10131b] p-5">
    <div className="flex items-center gap-2"><Activity size={17} className="text-amber-300" /><h3 data-testid={`${prefix}-title`} className="font-display text-lg font-semibold text-slate-200">Generation Diagnostics</h3><Button data-testid={`${prefix}-refresh`} className={`${action} ml-auto`} variant="outline" size="sm" onClick={() => { void query.refetch(); }}><RefreshCw size={13} /> Refresh</Button></div>
    <p data-testid={`${prefix}-help`} className="mt-2 text-xs text-slate-500">Provider responses, error codes and next steps. Secrets are redacted. Historical generic failures cannot be reconstructed; retry or run a diagnostic to capture details.</p>
    {query.error && <p data-testid={`${prefix}-error`} className="mt-3 text-xs text-rose-300">{query.error.message}</p>}
    {!query.data?.length && <p data-testid={`${prefix}-empty`} className="mt-4 text-xs text-slate-400">No generation attempts recorded yet.</p>}
    <div className="mt-3 max-h-96 space-y-2 overflow-y-auto">
      {query.data?.map(event => <details key={event.id} data-testid={`${prefix}-event-${event.id}`} className="rounded-lg border border-white/5 bg-black/20 px-3 py-2" open={event.status === 'failed' && query.data?.[0]?.id === event.id}>
        <summary data-testid={`${prefix}-event-toggle-${event.id}`} className="cursor-pointer text-xs text-slate-300 transition-colors hover:text-white"><span className={event.status === 'failed' ? 'text-rose-300' : 'text-emerald-300'}>{event.status.toUpperCase()}</span> · {event.provider} · {event.kind}{event.segment != null ? ` · segment ${event.segment + 1}` : ''}{event.http_status ? ` · HTTP ${event.http_status}` : ''}<span className="ml-2 text-slate-600">{event.created_at.replace('T', ' ').slice(0, 19)} UTC</span></summary>
        <div data-testid={`${prefix}-event-content-${event.id}`} className="mt-3 space-y-2 text-xs"><p className="font-mono text-slate-500">{event.model} {event.code} {event.request_id && `· request ${event.request_id}`}</p><p className="whitespace-pre-wrap break-words leading-relaxed text-slate-300">{event.message}</p>{event.action && <p className="leading-relaxed text-amber-200">Next step: {event.action}</p>}</div>
      </details>)}
    </div>
  </section>;
}

export function StudioStoryPanel({ storyId }: { storyId: string }) {
  const client = useQueryClient();
  const prefs = useQuery({ queryKey: ['story-engines', storyId], queryFn: () => apiGet<StoryEnginesView>(`/api/stories/${storyId}/media-engines`) });
  const jobs = useQuery({ queryKey: ['studio-jobs', storyId], queryFn: () => apiGet<StudioJobView[]>(`/api/stories/${storyId}/studio-jobs`), refetchInterval: 5000 });
  const [draft, setDraft] = useState<string | null>(null);
  const jobAction = useMutation({ mutationFn: ({ id, action: operation }: { id: string; action: string }) => apiPost<StudioJobView>(`/api/stories/${storyId}/studio-jobs/${id}/${operation}`), onSuccess: async () => { await client.invalidateQueries({ queryKey: ['studio-jobs', storyId] }); }, onError: errorToast });
  const save = useMutation({ mutationFn: async () => {
    const parsed = JSON.parse(draft || '{}') as { studio: StudioBinding; studio_segments: Record<string, StudioBinding> };
    if (!parsed.studio || typeof parsed.studio !== 'object') throw new Error('Include a studio object with character, references, direction and lora_ids');
    return apiPut<StoryEnginesView>(`/api/stories/${storyId}/media-engines`, { ...prefs.data, ...parsed });
  }, onSuccess: async () => { await client.invalidateQueries({ queryKey: ['story-engines', storyId] }); setDraft(null); toast.success('Studio reference mapping saved'); }, onError: errorToast });
  return <details data-testid="studio-story-panel" className="rounded-2xl border border-white/10 bg-[#10131b] p-5">
    <summary data-testid="studio-story-toggle" className="cursor-pointer text-sm font-semibold text-cyan-100">Your Studio · character references &amp; job QC</summary>
    <p data-testid="studio-mapping-help" className="mt-4 text-xs leading-relaxed text-slate-400">Characters and scene prompts are sent to Studio automatically from the story. Optionally set studio.references to a JSON object such as {'{ "character_key": "/absolute/path/on/server27/reference.png" }'} to give a character one fixed reference image (one image per character; the path must be inside the studio folder or StoryForge's media folder). Without it, Studio generates its own references.</p>
    <textarea data-testid="studio-mapping-json" aria-label="Studio reference mapping JSON" className={`${input} mt-4 font-mono leading-relaxed`} rows={10} value={draft ?? JSON.stringify({ studio: prefs.data?.studio || emptyBinding, studio_segments: prefs.data?.studio_segments || {} }, null, 2)} onChange={e => setDraft(e.target.value)} />
    <Button data-testid="save-studio-mapping" className={`${action} mt-3`} disabled={draft === null || save.isPending} onClick={() => save.mutate()}>Save reference mapping</Button>
    <div className="mt-4 space-y-2">{!jobs.data?.length && <p data-testid="studio-jobs-empty" className="text-xs text-slate-500">No Studio images yet. Choose “Your Studio” as the image engine and produce the story.</p>}
      {jobs.data?.map(job => <div data-testid={`studio-job-${job.id}`} key={job.id} className="rounded-lg bg-black/30 p-3 text-xs"><p className="text-slate-300">{job.operation} · {job.segment == null ? 'character reference' : `segment ${job.segment + 1}`} · {job.status}</p><p className={job.production_pass && job.status === 'succeeded' ? 'mt-1 text-emerald-300' : 'mt-1 text-amber-300'}>{job.production_pass && job.status === 'succeeded' ? 'Accepted' : (job.status === 'succeeded' ? 'Generated · review in StoryForge' : 'Generation pending or failed')}</p><pre className="mt-2 overflow-auto text-[11px] text-slate-500">{JSON.stringify({ job_id: job.job_id, qc: job.qc, error: job.error }, null, 2)}</pre><div className="mt-2 flex gap-2"><Button data-testid={`studio-job-refresh-${job.id}`} className={action} disabled={jobAction.isPending || !job.job_id} onClick={() => jobAction.mutate({ id: job.id, action: 'refresh' })}>Poll Studio</Button>{!['failed', 'cancelled', 'succeeded'].includes(job.status) && <Button data-testid={`studio-job-cancel-${job.id}`} className={action} disabled={jobAction.isPending || !job.job_id} onClick={() => jobAction.mutate({ id: job.id, action: 'cancel' })}>Request cancellation</Button>}</div></div>)}
    </div>
  </details>;
}