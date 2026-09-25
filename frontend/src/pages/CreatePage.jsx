import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowDown, ArrowUp, Clapperboard, Film, Images, LayoutGrid, Loader2,
  Music, Plus, Sparkles, Upload, Wand2, X,
} from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";

const fmtSize = (b) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round((b || 0) / 1e3)} KB`);

function TypeGrid({ types, vtype, setVtype }) {
  return (
    <div>
      <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">Video type — sets voice, music &amp; captions automatically</label>
      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
        {(types || []).map((t) => (
          <button
            key={t.key}
            data-testid={`video-type-${t.key}`}
            onClick={() => setVtype(t.key)}
            className={`rounded-xl border p-3.5 text-left transition-colors ${vtype === t.key ? "border-amber-500/60 bg-amber-500/10" : "border-white/10 bg-black/30 hover:border-amber-500/30"}`}
          >
            <div className={`text-xs font-bold ${vtype === t.key ? "text-amber-300" : "text-slate-200"}`}>{t.name}</div>
            <div className="mt-1 text-[10px] leading-relaxed text-slate-500">{t.audience} · {t.language.toUpperCase()} · {t.music_mood} music</div>
          </button>
        ))}
      </div>
    </div>
  );
}

function ScriptCreator({ title, types, vtype, setVtype, busy, setBusy }) {
  const [source, setSource] = useState("");
  const [mode, setMode] = useState("storyboard");
  const [length, setLength] = useState(90);
  const navigate = useNavigate();

  const submit = async () => {
    if (!source.trim()) {
      toast.error("Paste a script or prompt first");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/stories/create", {
        title, source_text: source, video_type: vtype, length_seconds: length, mode,
      });
      toast.success(data.imported
        ? `${data.imported_segments} supplied scenes loaded exactly — no dialogue or visual regeneration`
        : "No structured scenes found, so the script is being written from your prompt");
      navigate(`/stories/${data.story_id}`);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Create failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div>
        <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-slate-400">Script, dialogue &amp; visuals — or a prompt *</label>
        <Textarea data-testid="create-script-textarea" value={source} onChange={(e) => setSource(e.target.value)} rows={8}
          placeholder={"Paste a scene-by-scene script with labels such as:\nसीन 1 | 0:00–0:08 (Hook)\nविज़ुअल: …\nनैरेशन (वॉयसओवर): \"…\"\n\nSupplied scenes are loaded verbatim. Plain prompts still use AI script generation."}
          className="border-white/10 bg-black/30 text-sm leading-relaxed text-slate-200" />
      </div>

      <TypeGrid types={types} vtype={vtype} setVtype={setVtype} />

      <div className="grid gap-5 sm:grid-cols-2">
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">Production style</label>
          <div className="grid grid-cols-3 gap-2.5">
            <button data-testid="mode-slide-radio" onClick={() => setMode("slide")}
              className={`flex flex-col items-center gap-1.5 rounded-xl border p-4 transition-colors ${mode === "slide" ? "border-cyan-500/60 bg-cyan-500/10 text-cyan-200" : "border-white/10 bg-black/30 text-slate-400"}`}>
              <Images className="h-5 w-5" />
              <span className="text-xs font-semibold">Slide-based</span>
              <span className="text-[10px] leading-snug text-slate-500">AI image slides &amp; infographics + narration</span>
            </button>
            <button data-testid="mode-clip-radio" onClick={() => setMode("clip")}
              className={`flex flex-col items-center gap-1.5 rounded-xl border p-4 transition-colors ${mode === "clip" ? "border-fuchsia-500/60 bg-fuchsia-500/10 text-fuchsia-200" : "border-white/10 bg-black/30 text-slate-400"}`}>
              <Film className="h-5 w-5" />
              <span className="text-xs font-semibold">AI video clips</span>
              <span className="text-[10px] leading-snug text-slate-500">Short AI video clips per scene, stitched</span>
            </button>
            <button data-testid="mode-storyboard-radio" onClick={() => setMode("storyboard")}
              className={`flex flex-col items-center gap-1.5 rounded-xl border p-4 transition-colors ${mode === "storyboard" ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-200" : "border-white/10 bg-black/30 text-slate-400"}`}>
              <LayoutGrid className="h-5 w-5" />
              <span className="text-xs font-semibold">Instant storyboard</span>
              <span className="text-[10px] leading-snug text-slate-500">All slides in ONE image, sliced locally — fastest &amp; cheapest</span>
            </button>
          </div>
        </div>
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">Target length: <span className="text-amber-300">{length}s</span></label>
          <input data-testid="length-slider" type="range" min="30" max="240" step="15" value={length}
            onChange={(e) => setLength(Number(e.target.value))}
            className="mt-3 w-full accent-amber-500" />
          <div className="mt-1 flex justify-between text-[10px] text-slate-500"><span>30s</span><span>2 min</span><span>4 min</span></div>
        </div>
      </div>

      <Button data-testid="create-video-button" onClick={submit} disabled={busy} className="w-full bg-amber-500 py-3 font-semibold text-[#090A0F] hover:bg-amber-400">
        {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wand2 className="mr-2 h-4 w-4" />}
        Load Script &amp; Open Story Studio
      </Button>
      <p className="text-[11px] leading-relaxed text-slate-500">
        Numbered scenes with Visual + Narration/Dialogue labels bypass AI writing and load directly into matching segments. Unstructured prompts still use AI generation.
      </p>
    </>
  );
}

function ScriptSegmenter({ title, types, vtype, setVtype, busy, setBusy }) {
  const [source, setSource] = useState("");
  const [mode, setMode] = useState("storyboard");
  const [length, setLength] = useState(90);
  const navigate = useNavigate();

  const submit = async () => {
    if (!source.trim()) {
      toast.error("Paste your full script first");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/stories/create", {
        title, source_text: source, video_type: vtype, length_seconds: length, mode,
        create_mode: "segment",
      });
      toast.success("Segmenting your script on the local CPU model — character sheet, cast, narration & visuals are being built");
      navigate(`/stories/${data.story_id}`);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Segmentation failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div>
        <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-slate-400">Paste your full script — narration &amp; dialogue *</label>
        <Textarea data-testid="segment-script-textarea" value={source} onChange={(e) => setSource(e.target.value)} rows={9}
          placeholder={"Paste a complete story or screenplay in any format.\nThe local LLM (qwen2.5:72b / deepseek-r1:32b — auto-picked to fit CPU RAM) keeps your wording and builds:\n• one character-consistency sheet\n• per-scene visible cast\n• ~10s scene narration\n• detailed 9:16 visual prompts"}
          className="border-white/10 bg-black/30 text-sm leading-relaxed text-slate-200" />
        <p className="mt-1.5 text-[11px] leading-relaxed text-emerald-300/80">
          Your narration &amp; dialogue are preserved — the model only structures the script, it does not rewrite the story.
        </p>
      </div>

      <TypeGrid types={types} vtype={vtype} setVtype={setVtype} />

      <div className="grid gap-5 sm:grid-cols-2">
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">Production style</label>
          <div className="grid grid-cols-3 gap-2.5">
            <button data-testid="seg-mode-slide-radio" onClick={() => setMode("slide")}
              className={`flex flex-col items-center gap-1.5 rounded-xl border p-4 transition-colors ${mode === "slide" ? "border-cyan-500/60 bg-cyan-500/10 text-cyan-200" : "border-white/10 bg-black/30 text-slate-400"}`}>
              <Images className="h-5 w-5" />
              <span className="text-xs font-semibold">Slide-based</span>
            </button>
            <button data-testid="seg-mode-clip-radio" onClick={() => setMode("clip")}
              className={`flex flex-col items-center gap-1.5 rounded-xl border p-4 transition-colors ${mode === "clip" ? "border-fuchsia-500/60 bg-fuchsia-500/10 text-fuchsia-200" : "border-white/10 bg-black/30 text-slate-400"}`}>
              <Film className="h-5 w-5" />
              <span className="text-xs font-semibold">AI video clips</span>
            </button>
            <button data-testid="seg-mode-storyboard-radio" onClick={() => setMode("storyboard")}
              className={`flex flex-col items-center gap-1.5 rounded-xl border p-4 transition-colors ${mode === "storyboard" ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-200" : "border-white/10 bg-black/30 text-slate-400"}`}>
              <LayoutGrid className="h-5 w-5" />
              <span className="text-xs font-semibold">Storyboard</span>
            </button>
          </div>
        </div>
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">Target length: <span className="text-amber-300">{length}s</span></label>
          <input data-testid="seg-length-slider" type="range" min="30" max="240" step="15" value={length}
            onChange={(e) => setLength(Number(e.target.value))}
            className="mt-3 w-full accent-amber-500" />
          <div className="mt-1 flex justify-between text-[10px] text-slate-500"><span>30s</span><span>2 min</span><span>4 min</span></div>
        </div>
      </div>

      <Button data-testid="segment-script-button" onClick={submit} disabled={busy} className="w-full bg-emerald-500 py-3 font-semibold text-[#090A0F] hover:bg-emerald-400">
        {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wand2 className="mr-2 h-4 w-4" />}
        Segment Script &amp; Open Story Studio
      </Button>
      <p className="text-[11px] leading-relaxed text-slate-500">
        Runs entirely on your local Ollama CPU model. Once segmented, review the scenes and cast in Story Studio, then produce.
      </p>
    </>
  );
}

function MediaStitcher({ title, types, vtype, setVtype, busy, setBusy }) {
  const [items, setItems] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [music, setMusic] = useState(true);
  const [endcard, setEndcard] = useState(true);
  const [beatSync, setBeatSync] = useState(true);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef(null);
  const navigate = useNavigate();

  const onFiles = async (fileList) => {
    const files = [...fileList];
    if (!files.length) return;
    setUploading(true);
    for (const f of files) {
      const fd = new FormData();
      fd.append("file", f);
      try {
        const { data } = await api.post("/stitch/upload", fd);
        setItems((s) => [...s, { ...data, file: f.name, preview: URL.createObjectURL(f), narration: "", dur: data.kind === "image" ? 4 : null }]);
      } catch (err) {
        toast.error(`${f.name}: ${err?.response?.data?.detail || "upload failed"}`);
      }
    }
    setUploading(false);
  };

  const setItem = (i, patch) => setItems((s) => s.map((it, k) => (k === i ? { ...it, ...patch } : it)));
  const moveItem = (i, dir) => setItems((s) => {
    const j = i + dir;
    if (j < 0 || j >= s.length) return s;
    const next = [...s];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });

  const submit = async () => {
    if (!items.length) {
      toast.error("Upload at least one image or video clip");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/stitch", {
        title, video_type: vtype, music, endcard, beat_sync: beatSync,
        items: items.map((i) => ({ media_id: i.media_id, narration: i.narration, duration: i.dur })),
      });
      toast.success("Stitching queued — your clips, images, narration and music are being assembled");
      navigate(`/stories/${data.story_id}`);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Stitch failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div>
        <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">Your images &amp; video clips</label>
        <div
          data-testid="stitch-upload-zone"
          onClick={() => fileRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); onFiles(e.dataTransfer.files); }}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed px-6 py-10 text-center transition-colors ${dragOver ? "border-cyan-400 bg-cyan-500/10" : "border-white/15 bg-black/20 hover:border-cyan-500/50"}`}
        >
          {uploading ? <Loader2 className="h-7 w-7 animate-spin text-cyan-300" /> : <Plus className="h-7 w-7 text-cyan-300" />}
          <div className="text-sm font-semibold text-slate-200">{uploading ? "Uploading…" : "Click or drop images / video clips here"}</div>
          <div className="text-[11px] text-slate-500">JPG · PNG · WebP · MP4 · MOV · WebM — up to 30 items, mixed freely, in any order</div>
          <input data-testid="stitch-file-input" ref={fileRef} type="file" multiple accept="image/jpeg,image/png,image/webp,video/mp4,video/quicktime,video/webm,.avi,.m4v"
            className="hidden" onChange={(e) => { onFiles(e.target.files); e.target.value = ""; }} />
        </div>
      </div>

      {items.length > 0 && (
        <div className="space-y-3">
          {items.map((it, i) => (
            <div key={it.media_id} data-testid="stitch-item-row" className="flex flex-wrap items-start gap-3 rounded-xl border border-white/10 bg-black/20 p-3">
              {it.kind === "video" ? (
                <video src={it.preview} muted className="h-20 w-12 rounded-lg bg-black object-cover" />
              ) : (
                <img src={it.preview} alt="" className="h-20 w-12 rounded-lg bg-black object-cover" />
              )}
              <div className="min-w-[180px] flex-1 space-y-2">
                <div className="flex items-center gap-2 text-xs text-slate-400">
                  <span className="font-semibold text-slate-200">#{i + 1}</span>
                  <span className="capitalize">{it.kind}</span>
                  <span>· {fmtSize(it.size)}</span>
                  {it.kind === "video" && it.duration ? <span>· {it.duration}s</span> : null}
                </div>
                <Textarea data-testid={`stitch-item-narration-${i}`} value={it.narration}
                  onChange={(e) => setItem(i, { narration: e.target.value })} rows={2}
                  placeholder="Optional narration for this part — leave empty for silent/music-only playback"
                  className="border-white/10 bg-black/30 text-xs text-slate-200" />
                {it.kind === "image" && (
                  <div className="flex items-center gap-2 text-[11px] text-slate-400">
                    On screen for
                    <input data-testid={`stitch-item-duration-${i}`} type="number" min="1" max="20" value={it.dur ?? 4}
                      onChange={(e) => setItem(i, { dur: Number(e.target.value) })}
                      className="w-16 rounded-md border border-white/10 bg-black/30 px-2 py-1 text-xs text-slate-200" />
                    seconds
                  </div>
                )}
              </div>
              <div className="flex flex-col gap-1.5">
                <button data-testid={`stitch-item-up-${i}`} onClick={() => moveItem(i, -1)} className="rounded-md border border-white/10 bg-black/30 p-1.5 text-slate-400 hover:text-amber-300"><ArrowUp className="h-3.5 w-3.5" /></button>
                <button data-testid={`stitch-item-down-${i}`} onClick={() => moveItem(i, 1)} className="rounded-md border border-white/10 bg-black/30 p-1.5 text-slate-400 hover:text-amber-300"><ArrowDown className="h-3.5 w-3.5" /></button>
                <button data-testid={`stitch-item-remove-${i}`} onClick={() => setItems((s) => s.filter((_, k) => k !== i))} className="rounded-md border border-white/10 bg-black/30 p-1.5 text-slate-400 hover:text-red-400"><X className="h-3.5 w-3.5" /></button>
              </div>
            </div>
          ))}
        </div>
      )}

      <TypeGrid types={types} vtype={vtype} setVtype={setVtype} />

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex items-center gap-3 rounded-xl border border-white/10 bg-black/30 px-4 py-2.5">
          <Switch data-testid="stitch-music-switch" checked={music} onCheckedChange={setMusic} />
          <span className="flex items-center gap-2 text-sm text-slate-300"><Music className="h-4 w-4 text-amber-400" /> Background music (mood of the video type)</span>
        </div>
        <div className="flex items-center gap-3 rounded-xl border border-white/10 bg-black/30 px-4 py-2.5">
          <Switch data-testid="stitch-endcard-switch" checked={endcard} onCheckedChange={setEndcard} />
          <span className="text-sm text-slate-300">Subscribe end-card</span>
        </div>
        <div className="flex items-center gap-3 rounded-xl border border-white/10 bg-black/30 px-4 py-2.5 sm:col-span-2">
          <Switch data-testid="stitch-beatsync-switch" checked={beatSync} onCheckedChange={setBeatSync} />
          <span className="text-sm text-slate-300">Cut silent slides on the music's beat — tighter, rhythm-locked reels</span>
        </div>
      </div>

      <Button data-testid="stitch-submit-button" onClick={submit} disabled={busy || uploading} className="w-full bg-cyan-500 py-3 font-semibold text-[#090A0F] hover:bg-cyan-400">
        {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
        Stitch Into One 9:16 Video
      </Button>
      <p className="text-[11px] leading-relaxed text-slate-500">
        Everything is normalized to 1080×1920, reordered exactly as arranged, narrations are voiced (with expressive delivery), music mixed under, captions burned in — then exported ready for Shorts/Reels.
      </p>
    </>
  );
}

export default function CreatePage() {
  const [types, setTypes] = useState([]);
  const [title, setTitle] = useState("");
  const [vtype, setVtype] = useState("mythology_moral");
  const [tab, setTab] = useState("ai");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.get("/video-types").then((r) => { if (live) setTypes(r.data); }).catch(() => {});
    return () => { live = false; };
  }, []);

  return (
    <div className="mx-auto max-w-5xl space-y-8 pb-16">
      <div>
        <h1 className="flex items-center gap-3 font-display text-3xl font-extrabold tracking-tight text-slate-100 lg:text-4xl">
          <Clapperboard className="h-8 w-8 text-amber-400" /> Create a Video
        </h1>
        <p className="mt-1 text-sm text-slate-400">Let the AI write and produce from a prompt — or bring your own footage and let the studio stitch it into a polished Short.</p>
      </div>

      <div className="grid grid-cols-3 gap-2.5 sm:max-w-2xl">
        <button data-testid="create-tab-ai" onClick={() => setTab("ai")}
          className={`flex items-center justify-center gap-2 rounded-xl border px-3 py-3 text-xs font-semibold transition-colors sm:text-sm ${tab === "ai" ? "border-amber-500/60 bg-amber-500/10 text-amber-300" : "border-white/10 bg-black/30 text-slate-400 hover:border-amber-500/30"}`}>
          <Wand2 className="h-4 w-4" /> Script or prompt
        </button>
        <button data-testid="create-tab-segment" onClick={() => setTab("segment")}
          className={`flex items-center justify-center gap-2 rounded-xl border px-3 py-3 text-xs font-semibold transition-colors sm:text-sm ${tab === "segment" ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-300" : "border-white/10 bg-black/30 text-slate-400 hover:border-emerald-500/30"}`}>
          <Sparkles className="h-4 w-4" /> Segment my script
        </button>
        <button data-testid="create-tab-media" onClick={() => setTab("media")}
          className={`flex items-center justify-center gap-2 rounded-xl border px-3 py-3 text-xs font-semibold transition-colors sm:text-sm ${tab === "media" ? "border-cyan-500/60 bg-cyan-500/10 text-cyan-300" : "border-white/10 bg-black/30 text-slate-400 hover:border-cyan-500/30"}`}>
          <Upload className="h-4 w-4" /> Stitch my own media
        </button>
      </div>

      <div className="card-glow space-y-5 rounded-2xl border border-amber-500/10 bg-[#12141F] p-6">
        <div>
          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-slate-400">Video title (optional)</label>
          <Input data-testid="create-title-input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. The Boy Who Shared His Last Roti" className="border-white/10 bg-black/30 text-slate-200" />
        </div>

        {tab === "ai" && <ScriptCreator title={title} types={types} vtype={vtype} setVtype={setVtype} busy={busy} setBusy={setBusy} />}
        {tab === "segment" && <ScriptSegmenter title={title} types={types} vtype={vtype} setVtype={setVtype} busy={busy} setBusy={setBusy} />}
        {tab === "media" && <MediaStitcher title={title} types={types} vtype={vtype} setVtype={setVtype} busy={busy} setBusy={setBusy} />}
      </div>
    </div>
  );
}
