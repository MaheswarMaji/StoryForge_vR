import { useState } from "react";
import { useParams } from "react-router-dom";
import { Play, RefreshCw, Download, CheckCircle2, XCircle, AlertTriangle, Loader2, Wand2, Sparkles, TrendingUp, Clapperboard, Square } from "lucide-react";
import { api, usePoll, useChannels, MEDIA } from "@/lib/api";
import { BEATS, beatMeta, PIPELINE_STEPS, statusMeta } from "@/lib/ui";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { toast } from "sonner";
import { StoryEngineControls, GenerationDiagnostics, StudioStoryPanel } from '../components/MediaEngines';

const CAMERAS = { zoom_in: "Zoom In", zoom_out: "Zoom Out", pan_left: "Pan Left", pan_right: "Pan Right", static: "Static" };

function StoryHeader({ story, channel, chunks, busy, genScript, produce }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge data-testid="story-detail-status" className={`${statusMeta(story.status).cls} border text-[11px]`}>{statusMeta(story.status).label}</Badge>
          {channel && <Badge variant="outline" className="border-amber-500/30 text-amber-300">{channel.name}</Badge>}
          {story.category && <Badge variant="outline" className="border-white/15 text-slate-400">{story.category} · {story.target_audience}</Badge>}
          {story.script?.viral_score?.total != null && (
            <Badge data-testid="viral-score-badge" variant="outline" className={`border text-[11px] font-semibold ${(story.script.viral_score.total >= 80 && "border-emerald-500/50 text-emerald-300") || (story.script.viral_score.total >= 60 && "border-amber-500/50 text-amber-300") || "border-rose-500/50 text-rose-300"}`}>
              Viral {story.script.viral_score.total}/100
            </Badge>
          )}
          {story.mode && (
            <Badge variant="outline" className="border-cyan-500/40 text-[11px] text-cyan-300">
              {story.mode === "clip" ? "Continuity-locked animated clips" : story.mode === "storyboard" ? "Unified storyboard" : story.mode === "stitch" ? "Your media, stitched" : "Slide-based"}
            </Badge>
          )}
          {!!story.target_seconds && <Badge variant="outline" className="border-white/15 font-mono2 text-slate-400">~{story.target_seconds}s</Badge>}
          {story.script?.editor?.factual_ok === false && (
            <Badge className="border border-orange-500/50 bg-orange-950/40 text-[11px] text-orange-300">Fact-check: corrections applied</Badge>
          )}
          <Badge variant="outline" className="border-white/15 font-mono2 text-slate-400">${(story.cost?.total || 0).toFixed(3)}</Badge>
        </div>
        <h1 data-testid="story-detail-title" className="mt-3 font-display text-3xl font-extrabold tracking-tight text-slate-100 lg:text-4xl">{story.title_hindi || story.title_english}</h1>
        <p className="mt-1 text-sm text-slate-400">{story.title_english} · {story.source} {story.page_start ? `· pp.${story.page_start}–${story.page_end}` : ""}</p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {story.status === "draft" && (
          <Button data-testid="generate-script-cta" onClick={genScript} disabled={busy} className="bg-amber-500 font-semibold text-[#090A0F] hover:bg-amber-400">
            <Wand2 className="mr-2 h-4 w-4" /> Generate 6-Beat Script
          </Button>
        )}
        {(story.status === "script_ready" || story.status === "edits_requested" || story.status === "rejected") && (
          <Button data-testid="produce-video-cta" onClick={produce} disabled={busy} className="bg-emerald-500 font-semibold text-[#090A0F] hover:bg-emerald-400">
            <Sparkles className="mr-2 h-4 w-4" /> Produce Video
          </Button>
        )}
        {story.status === "failed" && (
          <Button data-testid="retry-cta" onClick={chunks.length ? produce : genScript} disabled={busy} className="bg-amber-500 font-semibold text-[#090A0F] hover:bg-amber-400">
            <RefreshCw className="mr-2 h-4 w-4" /> Retry {chunks.length ? "Production" : "Script"}
          </Button>
        )}
      </div>
    </div>
  );
}

function PipelineTracker({ story, stageIdx, onStop }) {
  return (
    <div data-testid="pipeline-tracker" className="card-glow flex flex-wrap items-center gap-2 rounded-2xl border border-amber-500/10 bg-[#12141F] px-6 py-4">
      {PIPELINE_STEPS.map((s, i) => (
        <div key={s.key} className="flex items-center gap-2">
          <div className={`flex items-center gap-2 rounded-full px-3.5 py-1.5 text-xs font-medium ${i < stageIdx ? "bg-emerald-500/10 text-emerald-300" : i === stageIdx ? "bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/40" : "text-slate-500"}`}>
            {i < stageIdx ? <CheckCircle2 className="h-3.5 w-3.5" /> : i === stageIdx && story.status === "rendering" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" />}
            {s.label}
          </div>
          {i < PIPELINE_STEPS.length - 1 && <div className="h-px w-5 bg-white/10" />}
        </div>
      ))}
      {story.stage && <span className="ml-auto font-mono2 text-[11px] text-slate-500">{story.stage}</span>}
      {story.status === "rendering" && (
        <button data-testid="stop-render-button" onClick={onStop}
          className="ml-3 flex shrink-0 items-center gap-1.5 rounded-full border border-rose-500/40 bg-rose-500/10 px-3 py-1 text-[11px] font-semibold text-rose-300 transition-colors hover:bg-rose-500/20">
          <Square className="h-3 w-3" /> Stop
        </button>
      )}
    </div>
  );
}

function EditorLog({ script }) {
  const vs = script.viral_score || {};
  return (
    <div data-testid="editor-log-card" className="card-glow rounded-2xl border border-blue-500/20 bg-blue-950/10 p-6">
      <h3 className="mb-2 flex items-center gap-2 font-display text-lg font-semibold text-blue-300">
        Editor &amp; Fact-Check Log
        <Badge className={`border text-[10px] ${script.editor.factual_ok ? "border-emerald-600/50 text-emerald-300" : "border-orange-500/50 text-orange-300"}`}>
          {script.editor.factual_ok ? "factual ✓" : "corrections applied"}
        </Badge>
        {script?.editor?.corrections_applied > 0 && (
          <span className="text-xs text-slate-500">{script.editor.corrections_applied} edits applied</span>
        )}
      </h3>
      <ul className="space-y-1.5">
        {(script.editor.notes || []).map((n, i) => (
          <li key={`note-${i}-${n.slice(0, 12)}`} className="flex gap-2 text-xs text-slate-300"><span className="text-blue-400">•</span>{n}</li>
        ))}
      </ul>
      {vs.total != null && (
        <div className="mt-4 flex flex-wrap gap-2">
          {Object.entries(vs).filter(([k]) => !["total", "verdict", "notes"].includes(k)).map(([k, v]) => (
            <div key={k} className="rounded-lg bg-black/30 px-3 py-1.5 text-center">
              <div className="font-mono2 text-sm font-semibold text-amber-300">{v}</div>
              <div className="text-[9px] uppercase tracking-wide text-slate-500">{k}</div>
            </div>
          ))}
          <div className="rounded-lg bg-amber-500/15 px-3 py-1.5 text-center ring-1 ring-amber-500/40">
            <div className="font-mono2 text-sm font-bold text-amber-300">{vs.total}</div>
            <div className="text-[9px] uppercase tracking-wide text-amber-500">total</div>
          </div>
        </div>
      )}
    </div>
  );
}

function RenderConfig({ story, onSave }) {
  return (
    <div data-testid="render-config-card" className="card-glow flex flex-wrap items-center gap-x-8 gap-y-4 rounded-2xl border border-cyan-500/20 bg-cyan-950/10 p-5">
      <div className="flex items-center gap-2 text-xs text-slate-300">
        <Clapperboard className="h-4 w-4 text-cyan-300" /> Video style
        <select data-testid="mode-select" value={story.mode || "slide"} onChange={(e) => onSave({ mode: e.target.value })}
          className="rounded-lg border border-white/15 bg-black/40 px-2 py-1.5 text-xs text-slate-200">
          <option value="slide">Slide-based — image slides + infographics</option>
          <option value="storyboard">Unified storyboard — one consistent contact sheet, sliced</option>
          <option value="clip">Continuity-locked animated scene frames</option>
        </select>
      </div>
      <div className="flex items-center gap-2 text-xs text-slate-300">
        Length
        <select data-testid="length-select" value={story.target_seconds || 90} onChange={(e) => onSave({ target_seconds: Number(e.target.value) })}
          className="rounded-lg border border-white/15 bg-black/40 px-2 py-1.5 text-xs text-slate-200">
          <option value={30}>30 seconds</option>
          <option value={45}>45 seconds</option>
          <option value={60}>60 seconds</option>
          <option value={90}>90 seconds</option>
          <option value={120}>120 seconds</option>
          <option value={150}>150 seconds</option>
          <option value={180}>180 seconds</option>
          <option value={240}>240 seconds</option>
        </select>
      </div>
      <span className="text-[11px] text-slate-500">{story.script?.imported_verbatim ? "Imported dialogue and visuals stay unchanged when length changes." : "Changing the length rewrites the script — review it again before producing."}</span>
    </div>
  );
}

function ImprovementCoach({ story, busy, onImprove }) {
  const used = (story.improvements || []).length;
  return (
    <div data-testid="improvement-coach-card" className="card-glow rounded-2xl border border-emerald-500/20 bg-emerald-950/10 p-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h3 className="flex flex-wrap items-center gap-2 font-display text-lg font-semibold text-emerald-300">
            <TrendingUp className="h-5 w-5" /> Improvement Coach
            <Badge variant="outline" className="border-white/15 text-[10px] text-slate-400">{used}/2 rounds used</Badge>
          </h3>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-400">
            After assessment, the coach pinpoints the single weakest scope and applies minimal targeted edits — kept only if the overall viral score improves, otherwise auto-reverted. Max 2 rounds.
          </p>
        </div>
        <Button data-testid="improve-video-button" disabled={busy || used >= 2 || story.status === "rendering"} onClick={onImprove} className="bg-emerald-500 font-semibold text-[#090A0F] hover:bg-emerald-400">
          <Sparkles className="mr-2 h-4 w-4" /> {used >= 2 ? "Budget used (2/2)" : "Run Improvement Coach"}
        </Button>
      </div>
      {!!used && (
        <div className="mt-4 space-y-2">
          {story.improvements.map((it) => (
            <div key={`round-${it.round}`} data-testid={`improvement-round-${it.round - 1}`} className="flex flex-wrap items-center gap-3 rounded-xl bg-black/30 px-4 py-2.5 text-xs">
              <Badge className={`border text-[10px] ${it.accepted ? "border-emerald-600/50 bg-emerald-950 text-emerald-300" : "border-orange-600/50 bg-orange-950 text-orange-300"}`}>
                {it.accepted ? "score improved" : "reverted — no gain"}
              </Badge>
              <span className="text-slate-300">Round {it.round}: {it.scope}</span>
              <span className="ml-auto font-mono2 text-slate-400">{it.base_score} → {it.new_score ?? it.base_score}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CharacterPanel({ story, media, busy, onSave }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(story.character_sheet?.anchor || "");
  const beginEdit = () => {
    setDraft(story.character_sheet?.anchor || "");
    setEditing(true);
  };
  const save = async () => {
    await onSave(draft);
    setEditing(false);
  };
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="card-glow rounded-2xl border border-amber-500/10 bg-[#12141F] p-6 lg:col-span-2">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 data-testid="character-sheet-title" className="font-display text-lg font-semibold text-amber-300">Character &amp; Style Consistency Sheet</h3>
            <p data-testid="character-sheet-guidance" className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-500">
              Canonical continuity lock for every frame: faces, age, hair, skin tone, build, clothing, accessories, art medium, palette, lighting, architecture and recurring locations.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {story.character_sheet?.locked && <Badge data-testid="character-sheet-locked-badge" variant="outline" className="border-emerald-500/40 text-emerald-300">Locked · v{story.character_sheet?.version || 1}</Badge>}
            {!editing && story.status !== "rendering" && (
              <Button data-testid="edit-character-sheet-button" size="sm" variant="outline" className="border-amber-500/40 text-amber-300 hover:bg-amber-500/10" onClick={beginEdit}>
                <Wand2 className="mr-1.5 h-3.5 w-3.5" /> Edit sheet
              </Button>
            )}
          </div>
        </div>
        {editing ? (
          <div className="space-y-3">
            <Textarea data-testid="character-sheet-edit-input" value={draft} onChange={(e) => setDraft(e.target.value)} rows={12}
              placeholder="Define the immutable art style first, then every recurring character's exact face, age, skin tone, hair, build, clothing colors, accessories and aura; finish with recurring locations, architecture, props, palette and lighting."
              className="border-white/15 bg-black/40 text-sm leading-relaxed text-slate-100" />
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span data-testid="character-sheet-character-count" className={`text-xs ${draft.trim().length < 80 ? "text-rose-300" : "text-slate-500"}`}>{draft.trim().length} characters · minimum 80</span>
              <div className="flex gap-2">
                <Button data-testid="cancel-character-sheet-button" size="sm" variant="outline" className="border-white/20 text-slate-300" onClick={() => setEditing(false)}>Cancel</Button>
                <Button data-testid="save-character-sheet-button" size="sm" disabled={busy || draft.trim().length < 80} className="bg-amber-500 font-semibold text-[#090A0F] hover:bg-amber-400" onClick={save}>
                  <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" /> Save continuity lock
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <p data-testid="character-sheet-anchor" className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300">{story.character_sheet?.anchor || "No consistency sheet yet — add one before generating visuals."}</p>
        )}
        <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-400">
          {(story.characters || []).map((c, i) => (
            <span key={`char-${c.name || i}`} className="rounded-full bg-black/30 px-3 py-1">{c.name}: {c.description}</span>
          ))}
        </div>
      </div>
      {media.char_sheet && (
        <div className="card-glow overflow-hidden rounded-2xl border border-amber-500/10 bg-[#12141F]">
          <img data-testid="character-sheet-image" src={`${MEDIA}${media.char_sheet}?v=${story.character_sheet?.version || 1}`} alt="character sheet" className="h-64 w-full object-cover" />
          <p data-testid="character-sheet-image-caption" className="border-t border-white/5 px-4 py-3 text-[11px] leading-relaxed text-slate-500">Visual identity reference supplied to every image-capable generation path.</p>
        </div>
      )}
    </div>
  );
}

function SegmentRow({ chunk, i, media, editing, drafts, setDraft, regen, busy, storyId }) {
  const bm = beatMeta(chunk.beat);
  return (
    <div key={chunk.chunk_id ?? `seg-${i}`} data-testid={`story-beat-${chunk.beat}`} className={`rise rounded-2xl border p-5 ${bm.cls}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Badge className={`${bm.text} border ${bm.border} bg-black/30 text-[11px] font-semibold uppercase tracking-wider`}>{bm.label}</Badge>
        <span className="font-mono2 text-[11px] text-slate-400">segment {i + 1} · ~10s · {bm.hint}</span>
        <span className="ml-auto flex items-center gap-2">
          <span className="rounded-full bg-black/30 px-3 py-1 text-[11px] text-slate-300">{CAMERAS[chunk.camera] || chunk.camera}</span>
          <span className="rounded-full bg-black/30 px-3 py-1 text-[11px] text-slate-300">{chunk.emotion}</span>
        </span>
      </div>
      {Array.isArray(chunk.cast) && (
        <div data-testid={`segment-cast-${i}`} className="mb-1 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="text-slate-500">Visible cast:</span>
          {chunk.cast.length === 0 ? (
            <span className="rounded-full bg-black/30 px-2.5 py-0.5 text-slate-400">Environment only</span>
          ) : chunk.cast.map((c, k) => (
            <span key={`cast-${i}-${k}`} className="rounded-full border border-fuchsia-500/30 bg-fuchsia-500/10 px-2.5 py-0.5 text-fuchsia-200">{c}</span>
          ))}
        </div>
      )}
      <StoryEngineControls storyId={storyId} segment={i} disabled={busy} />
      <div className="mt-4 grid gap-5 lg:grid-cols-5">
        <div className="lg:col-span-3">
          {editing ? (
            <Textarea data-testid={`voiceover-edit-${i}`} rows={2}
              value={(drafts[i] || {}).voiceover ?? chunk.voiceover}
              onChange={(e) => setDraft(i, { voiceover: e.target.value })}
              className="font-deva border-white/15 bg-black/40 text-base text-slate-100" />
          ) : (
            <p data-testid={`segment-voiceover-${i}`} className="font-deva text-lg font-medium leading-relaxed text-slate-100">“{chunk.voiceover}”</p>
          )}
          {editing ? (
            <div className="mt-3 space-y-2">
              <Textarea data-testid={`visual-edit-${i}`} rows={2} placeholder="visual summary"
                value={(drafts[i] || {}).visual ?? chunk.visual}
                onChange={(e) => setDraft(i, { visual: e.target.value })}
                className="border-white/15 bg-black/40 font-mono2 text-[11px] text-slate-300" />
              <Textarea data-testid={`video-prompt-edit-${i}`} rows={3} placeholder="AI image/video prompt"
                value={(drafts[i] || {}).video_prompt ?? chunk.video_prompt}
                onChange={(e) => setDraft(i, { video_prompt: e.target.value })}
                className="border-white/15 bg-black/40 font-mono2 text-[11px] text-slate-400" />
            </div>
          ) : (
            <p className="mt-3 font-mono2 text-[11px] leading-relaxed text-slate-400">VISUAL: {chunk.video_prompt || chunk.visual}</p>
          )}
          <div className="mt-4 flex items-center gap-3">
            {media.audio?.[i] && (
              <div className="flex items-center gap-2 rounded-full bg-black/40 px-3 py-1.5">
                <button data-testid={`play-segment-audio-button-${i}`} onClick={() => document.getElementById(`seg-audio-${i}`)?.play()} className="text-amber-300 hover:text-amber-200">
                  <Play className="h-4 w-4" />
                </button>
                <audio id={`seg-audio-${i}`} src={`${MEDIA}${media.audio[i]}`} preload="none" controls className="h-8 max-w-52" />
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <Button data-testid={`regenerate-segment-script-button-${i}`} size="sm" variant="outline" className="border-amber-500/30 text-amber-300 hover:bg-amber-500/10" disabled={busy} onClick={() => regen(i, "script")}>
                <Wand2 className="mr-1.5 h-3.5 w-3.5" /> Script
              </Button>
              <Button data-testid={`regenerate-segment-voice-button-${i}`} size="sm" variant="outline" className="border-cyan-500/30 text-cyan-300 hover:bg-cyan-500/10" disabled={busy} onClick={() => regen(i, "voice")}>
                <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Voice
              </Button>
              <Button data-testid={`regenerate-segment-visual-button-${i}`} size="sm" variant="outline" className="border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/10" disabled={busy} onClick={() => regen(i, "visual")}>
                <Sparkles className="mr-1.5 h-3.5 w-3.5" /> Image / clip
              </Button>
            </div>
          </div>
        </div>
        {media.frames?.[i] && (
          <div className="overflow-hidden rounded-xl lg:col-span-2">
            <img data-testid={`segment-frame-image-${i}`} src={`${MEDIA}${media.frames[i]}`} alt={`frame ${i + 1}`} className="h-56 w-full object-cover ring-1 ring-white/10" />
          </div>
        )}
      </div>
    </div>
  );
}

function PublishPanel({ story, busy, onPublish }) {
  return (
    <div data-testid="publish-panel" className="card-glow flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-cyan-500/25 bg-cyan-950/15 p-6">
      <div>
        <h3 className="font-display text-lg font-semibold text-cyan-200">Approved — Ready to Publish</h3>
        <p className="text-xs text-slate-400">Upload the final 9:16 video straight to your connected channels</p>
        {story.publish?.youtube?.url && (
          <a data-testid="published-youtube-link" href={story.publish.youtube.url} target="_blank" rel="noreferrer" className="mt-2 block text-xs text-emerald-300 underline">Published on YouTube: {story.publish.youtube.url}</a>
        )}
        {story.publish?.instagram?.url && (
          <a data-testid="published-instagram-link" href={story.publish.instagram.url} target="_blank" rel="noreferrer" className="mt-1 block text-xs text-emerald-300 underline">Published on Instagram: {story.publish.instagram.url}</a>
        )}
      </div>
      <div className="flex flex-wrap gap-3">
        <Button data-testid="publish-youtube-button" disabled={busy} className="bg-red-600 font-semibold text-white hover:bg-red-500" onClick={() => onPublish("youtube")}>
          Upload to YouTube Shorts
        </Button>
        <Button data-testid="publish-instagram-button" disabled={busy} className="bg-fuchsia-600 font-semibold text-white hover:bg-fuchsia-500" onClick={() => onPublish("instagram")}>
          Upload to Instagram Reels
        </Button>
      </div>
    </div>
  );
}

function ResultsGrid({ story, media }) {
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="card-glow rounded-2xl border border-amber-500/10 bg-[#12141F] p-6">
        <h3 className="mb-4 font-display text-lg font-semibold text-amber-300">Final 9:16 Master</h3>
        <div className="phone-frame mx-auto w-72">
          <video data-testid="final-video-player" src={`${MEDIA}${media.final}`} controls className="h-[512px] w-full bg-black object-contain" />
        </div>
        <div className="mt-3 text-center text-xs text-slate-500">{media.duration_sec ? `${media.duration_sec}s · 1080×1920 · 30fps` : ""}</div>
        <a data-testid="export-shorts-mp4-button" href={`${MEDIA}${media.final}`} download={`${(story.title_english || "story").replace(/\s+/g, "-")}.mp4`}
          className="mt-3 flex items-center justify-center gap-2 rounded-xl bg-amber-500 px-4 py-2.5 text-sm font-semibold text-[#090A0F] hover:bg-amber-400">
          <Download className="h-4 w-4" /> Download MP4 (Shorts / Reels)
        </a>
      </div>

      <div className="card-glow rounded-2xl border border-amber-500/10 bg-[#12141F] p-6">
        <h3 data-testid="qa-report-accordion" className="mb-4 font-display text-lg font-semibold text-amber-300">Automated QA Report</h3>
        {story.qa?.score != null && (
          <div className="mb-4 flex items-center gap-3">
            <div className={`font-display text-4xl font-bold ${story.qa.passed ? "text-emerald-400" : "text-orange-400"}`}>{Number(story.qa.score).toFixed(1)}</div>
            <div className="text-xs text-slate-400">/ 10 overall<br />{story.qa.passed ? "All critical checks passed" : "Flagged — see issues"}</div>
          </div>
        )}
        <Accordion type="multiple" className="w-full">
          {(story.qa?.checks || []).map((c, i) => (
            <AccordionItem key={`qa-${c.name || i}`} value={`c${i}`} className="border-white/5">
              <AccordionTrigger className="py-2.5 text-left text-sm text-slate-200 hover:no-underline">
                <span className="flex items-center gap-2">
                  {c.passed ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : <XCircle className="h-4 w-4 text-red-400" />}
                  <span className="capitalize">{c.name?.replace(/_/g, " ")}</span>
                </span>
              </AccordionTrigger>
              <AccordionContent className="text-xs leading-relaxed text-slate-400">{c.detail}</AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>
        {!!(story.qa?.issues || []).length && (
          <div className="mt-4 space-y-1.5">
            {(story.qa.issues || []).map((x, i) => (
              <div key={`issue-${i}-${String(x).slice(0, 12)}`} className="flex gap-2 text-xs text-orange-300"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{x}</div>
            ))}
          </div>
        )}
        {!story.qa?.checks?.length && <div className="text-sm text-slate-500">QA runs automatically at the end of production</div>}
      </div>

      <div className="card-glow rounded-2xl border border-amber-500/10 bg-[#12141F] p-6">
        <h3 className="mb-4 font-display text-lg font-semibold text-amber-300">Publishing Metadata</h3>
        {media.thumbnail && <img data-testid="thumbnail-image" src={`${MEDIA}${media.thumbnail}`} alt="thumbnail" className="mb-4 w-full rounded-xl ring-1 ring-white/10" />}
        <div data-testid="meta-title" className="font-medium leading-snug text-slate-100">{story.metadata?.title}</div>
        <p data-testid="meta-description" className="mt-2 text-xs leading-relaxed text-slate-400">{story.metadata?.description}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {(story.metadata?.hashtags || []).map((h, i) => (
            <span key={`tag-${h}-${i}`} className="rounded-full bg-blue-950/60 px-2.5 py-0.5 text-[10px] text-blue-300">#{h}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

function ReviewBar({ story, busy, review }) {
  const [notes, setNotes] = useState("");
  const [open, setOpen] = useState(false);
  return (
    <div data-testid="review-bar" className="card-glow flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-purple-500/25 bg-purple-950/20 p-6">
      <div>
        <h3 className="font-display text-lg font-semibold text-purple-200">Human Review</h3>
        <p className="text-xs text-slate-400">Approve to mark ready for upload, or send back with notes</p>
        {story.review_notes && <p className="mt-2 max-w-xl text-xs italic text-yellow-300">“{story.review_notes}”</p>}
      </div>
      <div className="flex flex-wrap gap-3">
        <Button data-testid="review-approve-button" disabled={busy} className="bg-emerald-500 font-semibold text-[#090A0F] hover:bg-emerald-400" onClick={() => review("approve")}>
          <CheckCircle2 className="mr-2 h-4 w-4" /> Approve for Upload
        </Button>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button data-testid="review-request-edits-button" disabled={busy} variant="outline" className="border-amber-500/40 text-amber-300 hover:bg-amber-500/10">
              <Wand2 className="mr-2 h-4 w-4" /> Request Edits
            </Button>
          </DialogTrigger>
          <DialogContent className="border-amber-500/20 bg-[#12141F]">
            <DialogHeader><DialogTitle className="font-display text-amber-300">Request Edits</DialogTitle></DialogHeader>
            <Textarea data-testid="edit-notes-input" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="e.g. segment 3 visual shows the wrong character; slow down the climax narration…" className="border-white/10 bg-black/30 text-slate-200" rows={4} />
            <Button data-testid="submit-edit-request-button" disabled={!notes.trim()} onClick={async () => { await review("request_edits", notes.trim()); setNotes(""); setOpen(false); }} className="bg-amber-500 font-semibold text-[#090A0F] hover:bg-amber-400">Submit</Button>
          </DialogContent>
        </Dialog>
        <Button data-testid="review-reject-button" disabled={busy} variant="outline" className="border-rose-500/40 text-rose-300 hover:bg-rose-500/10" onClick={() => review("reject", "Rejected by reviewer")}>
          <XCircle className="mr-2 h-4 w-4" /> Reject
        </Button>
      </div>
    </div>
  );
}

export default function StoryDetailPage() {
  const { id } = useParams();
  const [story, refresh, err] = usePoll(`/stories/${id}`, 4000);
  const { channels } = useChannels();
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState({});

  if (err) return <div className="p-10 text-red-400">Story not found</div>;
  if (!story) return <div className="p-10 text-slate-500">Opening studio…</div>;

  const channel = channels?.find((c) => c.id === story.channel_id);
  const chunks = story.script?.chunks || [];
  const media = story.media || {};
  const hasVideo = !!media.final;

  const act = async (fn) => { setBusy(true); try { await fn(); refresh(); } catch (e) { toast.error(e?.response?.data?.detail || "Action failed"); } finally { setBusy(false); } };
  const genScript = () => act(() => api.post(`/stories/${id}/script`).then(() => toast.success("Script generation queued")));
  const produce = () => act(() => api.post(`/stories/${id}/produce`).then(() => toast.success("Production pipeline started — voices, frames, clips, stitch, QA")));
  const regen = (i, kind = "all") => act(() => api.post(`/stories/${id}/segments/${i}/regenerate`, { kind }).then(() => toast.success(`${kind === "visual" ? "Image / clip" : kind[0].toUpperCase() + kind.slice(1)} regeneration queued for segment ${i + 1}`)));
  const review = (action, note = "") => act(() => api.post(`/stories/${id}/review`, { action, notes: note }).then(() => toast.success(action === "request_edits" ? "Edit request queued — the studio will apply your notes" : `Review recorded: ${action.replace("_", " ")}`)));
  const improve = () => act(() => api.post(`/stories/${id}/improve`).then(() => toast.success("Improvement Coach queued — targeted edits, kept only if the score improves")));
  const stopRender = () => act(() => api.post(`/stories/${id}/stop`).then(() => toast.success("Render stopped — completed segments are kept")));
  const publish = (platform) => act(() => api.post(`/stories/${id}/publish`, { platform }).then(() => toast.success(platform === "youtube" ? "YouTube upload queued" : "Instagram Reels upload queued")));

  const saveScript = () => act(async () => {
    const chunkEdits = Object.entries(drafts).map(([i, d]) => ({ index: Number(i), ...d }));
    await api.patch(`/stories/${id}/script`, { chunks: chunkEdits });
    setEditing(false);
    setDrafts({});
    toast.success("Script updated — edited segments will re-render on next produce");
  });

  const saveCharacterSheet = (anchor) => act(async () => {
    await api.patch(`/stories/${id}/character-sheet`, { anchor });
    toast.success("Consistency sheet locked — all visuals and clips will regenerate on the next render");
  });

  const saveConfig = (patch) => act(async () => {
    await api.put(`/stories/${id}/config`, patch);
    if (patch.target_seconds != null) {
      if (story.script?.imported_verbatim) {
        toast.success("Length updated — supplied dialogue and visuals were preserved exactly");
      } else {
        await api.post(`/stories/${id}/script`);
        toast.success("Length updated — the script is being rewritten; review it before producing");
      }
    } else {
      toast.success("Video style updated");
    }
  });

  const setDraft = (i, patch) => setDrafts((s) => ({ ...s, [i]: { ...(s[i] || {}), ...patch } }));
  const startEditing = () => {
    setDrafts(Object.fromEntries(chunks.map((c, i) => [i, { voiceover: c.voiceover, visual: c.visual, video_prompt: c.video_prompt }])));
    setEditing(true);
  };

  const stageIdx = (() => {
    if (story.status === "published") return PIPELINE_STEPS.length;
    if (["approved", "edits_requested", "rejected"].includes(story.status)) return PIPELINE_STEPS.length - 1;
    const m = { "": 0, Scripting: 0, "Script ready": 0, "Character sheet": 1, "Queued": 1 };
    const s = story.stage || "";
    if (/Segment|Voices/i.test(s)) return 1;
    if (/Stitch/i.test(s)) return 4;
    if (/QA|Metadata/i.test(s)) return 5;
    if (/review/i.test(s)) return 6;
    return m[s] ?? (hasVideo ? 6 : chunks.length ? 1 : 0);
  })();

  return (
    <div className="mx-auto max-w-7xl space-y-8 pb-16">
      <StoryHeader story={story} channel={channel} chunks={chunks} busy={busy} genScript={genScript} produce={produce} />

      {story.error && <div data-testid="story-generation-error" className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-red-500/30 bg-red-950/30 px-4 py-3 text-sm text-red-300">{story.error}</div>}
      <StoryEngineControls storyId={id} disabled={busy || story.status === 'rendering'} />
      <GenerationDiagnostics storyId={id} />
      <StudioStoryPanel storyId={id} />

      <PipelineTracker story={story} stageIdx={stageIdx} onStop={stopRender} />

      {(story.script?.editor?.notes?.length || story.script?.editor?.factual_ok != null) && <EditorLog script={story.script} />}

      {chunks.length > 0 && !hasVideo && story.status !== "rendering" && <RenderConfig story={story} onSave={saveConfig} />}

      {chunks.length > 0 && <ImprovementCoach story={story} busy={busy} onImprove={improve} />}

      {chunks.length > 0 && (
        <>
          <CharacterPanel story={story} media={media} busy={busy} onSave={saveCharacterSheet} />
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h3 className="font-display text-xl font-semibold text-amber-300">Script Segments</h3>
              {story.status !== "rendering" && story.status !== "published" && (
                editing ? (
                  <div className="flex gap-2">
                    <Button data-testid="save-script-edits-button" size="sm" disabled={busy} className="bg-emerald-500 font-semibold text-[#090A0F] hover:bg-emerald-400" onClick={saveScript}>
                      <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" /> Save Changes
                    </Button>
                    <Button data-testid="cancel-script-edits-button" size="sm" variant="outline" className="border-white/20 text-slate-300" onClick={() => { setEditing(false); setDrafts({}); }}>
                      Cancel
                    </Button>
                  </div>
                ) : (
                  <Button data-testid="edit-script-button" size="sm" variant="outline" className="border-amber-500/40 text-amber-300 hover:bg-amber-500/10" onClick={startEditing}>
                    <Wand2 className="mr-1.5 h-3.5 w-3.5" /> Review &amp; Edit Script
                  </Button>
                )
              )}
            </div>
            {chunks.map((c, i) => (
              <SegmentRow key={c.chunk_id ?? `seg-${i}`} chunk={c} i={i} media={media}
                editing={editing} drafts={drafts} setDraft={setDraft} regen={regen} busy={busy || story.status === 'rendering'} storyId={id} />
            ))}
          </div>
        </>
      )}

      {hasVideo && story.status === "approved" && <PublishPanel story={story} busy={busy} onPublish={publish} />}
      {hasVideo && <ResultsGrid story={story} media={media} />}
      {hasVideo && <ReviewBar story={story} busy={busy} review={review} />}
    </div>
  );
}
