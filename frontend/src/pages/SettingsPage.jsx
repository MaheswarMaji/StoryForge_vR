import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Instagram, KeyRound, Loader2, Plug, RefreshCw, ShieldCheck, ShieldAlert, XCircle, Youtube } from "lucide-react";
import { api, usePoll } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import { EngineSettingsCard, GenerationDiagnostics } from '../components/MediaEngines';

const KEY_LABELS = {
  STUDIO_API_TOKEN: "Your Studio — only for a future HTTP API; not needed for the local worker on server27",
  OPENAI_API_KEY: "OpenAI (DALL·E / GPT Image 1 + LLM fallback)",
  GEMINI_API_KEY: "Google Gemini (Veo video, TTS, image, LLM)",
  HF_TOKEN: "Hugging Face (free hosted Qwen-72B text fallback; image API deprecated by HF)",
  FAL_KEY: "fal.ai (FLUX / SDXL / Juggernaut images, Wan / CogVideoX clips)",
  REPLICATE_API_TOKEN: "Replicate (FLUX images, Wan 2.1 clips)",
  STABILITY_API_KEY: "Stability AI (Stable Image Core / SD3.5 images)",
  PEXELS_API_KEY: "Pexels (free real photos & video clips — works without AI billing)",
  YOUTUBE_CLIENT_ID: "YouTube OAuth — Client ID",
  YOUTUBE_CLIENT_SECRET: "YouTube OAuth — Client Secret",
  YOUTUBE_REFRESH_TOKEN: "YouTube OAuth — Refresh Token (auto-filled after consent)",
  INSTAGRAM_ACCESS_TOKEN: "Instagram — Long-lived Access Token",
  INSTAGRAM_USER_ID: "Instagram — Professional User ID",
};

function ApiKeyRow({ name, info, values, setValue, clear }) {
  return (
    <div data-testid={`api-key-row-${name}`} className="flex flex-wrap items-center gap-2 rounded-xl bg-black/30 px-3 py-2">
      <div className="min-w-56 flex-1">
        <div className="flex items-center gap-2 text-xs font-semibold text-slate-200">
          {name.replace(/_/g, " ")}
          {info.set && <Badge className="border border-emerald-600/50 bg-emerald-950/60 text-[9px] text-emerald-300">active</Badge>}
        </div>
        <div className="text-[10px] text-slate-500">{KEY_LABELS[name] || name}{info.set && info.hint ? ` · ${info.hint}` : ""}</div>
      </div>
      <Input
        data-testid={`api-key-${name}-input`}
        type="password"
        value={values[name] || ""}
        onChange={(e) => setValue(name, e.target.value)}
        placeholder={info.set ? "•••• (leave blank to keep)" : "paste value…"}
        className="h-8 w-full max-w-72 border-white/10 bg-black/30 font-mono2 text-[11px] text-slate-200 sm:w-auto"
      />
      {info.set && (
        <button data-testid={`clear-api-key-${name}`} onClick={() => clear(name)} title="Clear this key" className="text-slate-500 hover:text-rose-300">
          <XCircle className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

function ApiKeysVault({ keys, refreshKeys }) {
  const [values, setValues] = useState({});
  const [savingKeys, setSavingKeys] = useState(false);

  const saveKeys = async () => {
    const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v && v.trim()));
    if (!Object.keys(filled).length) {
      toast.error("Nothing to save — paste at least one key");
      return;
    }
    setSavingKeys(true);
    try {
      const r = await api.put("/settings/api-keys", { values: filled });
      toast.success(`Saved: ${r.data.saved.join(", ")}`);
      setValues({});
      refreshKeys();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Save failed");
    } finally {
      setSavingKeys(false);
    }
  };

  const clearKey = async (name) => {
    try {
      await api.delete(`/settings/api-keys/${name}`);
      toast.success(`${name} cleared`);
      refreshKeys();
    } catch {
      toast.error("Clear failed");
    }
  };

  return (
    <div data-testid="api-keys-card" className="card-glow rounded-2xl border border-amber-500/15 bg-[#12141F] p-6">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <KeyRound className="h-5 w-5 text-amber-400" />
        <h3 className="font-display text-lg font-semibold text-amber-300">API Keys Vault</h3>
        <Button data-testid="save-api-keys-button" size="sm" onClick={saveKeys} disabled={savingKeys} className="ml-auto bg-amber-500 font-semibold text-[#090A0F] hover:bg-amber-400">
          {savingKeys ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" />} Save Keys
        </Button>
      </div>
      <div className="space-y-2">
        {Object.entries(keys || {}).map(([name, info]) => (
          <ApiKeyRow key={name} name={name} info={info} values={values}
            setValue={(k, v) => setValues({ ...values, [k]: v })} clear={clearKey} />
        ))}
      </div>
      <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
        Keys are saved server-side and override environment defaults, including after restart. Saving a key resets provider cooldowns. Choose your image and video engines above; a configured key alone does not confirm billing or generation quota.
      </p>
    </div>
  );
}

function InstagramCard({ ig, refreshIg }) {
  const [token, setToken] = useState("");
  const [uid, setUid] = useState("");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!token.trim() || !uid.trim()) {
      toast.error("Paste both the access token and the Instagram user ID");
      return;
    }
    setSaving(true);
    try {
      await api.put("/settings/instagram", { access_token: token.trim(), user_id: uid.trim() });
      toast.success("Instagram connected & verified");
      setToken("");
      setUid("");
      refreshIg();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const disconnect = async () => {
    try {
      await api.delete("/settings/instagram");
      toast.success("Instagram disconnected");
      refreshIg();
    } catch {
      toast.error("Disconnect failed");
    }
  };

  return (
    <div data-testid="instagram-settings-card" className={`card-glow rounded-2xl border p-6 ${ig?.connected && !ig?.error ? "border-fuchsia-500/30 bg-fuchsia-950/10" : "border-amber-500/20 bg-[#12141F]"}`}>
      <div className="flex items-center gap-2">
        <Instagram className="h-5 w-5 text-fuchsia-400" />
        <h3 className="font-display text-lg font-semibold text-slate-100">Instagram Reels</h3>
        {ig?.connected ? (
          ig?.error ? (
            <Badge data-testid="instagram-status-badge" className="border border-orange-500/50 bg-orange-950/50 text-[10px] text-orange-300">token check failed</Badge>
          ) : (
            <Badge data-testid="instagram-status-badge" className="border border-emerald-600/50 bg-emerald-950/60 text-[10px] text-emerald-300">connected{ig?.username ? ` · @${ig.username}` : ""}</Badge>
          )
        ) : (
          <Badge data-testid="instagram-status-badge" variant="outline" className="border-white/15 text-[10px] text-slate-400">not connected</Badge>
        )}
      </div>

      {ig?.connected ? (
        <div className="mt-4 space-y-3 text-xs text-slate-400">
          <div>User ID: <span className="font-mono2 text-slate-200">{ig.user_id}</span> <span className="text-slate-600">({ig.source})</span></div>
          <div>Token: <span className="font-mono2 text-slate-200">{ig.token_hint}</span></div>
          {ig.error && <div className="text-orange-300">{ig.error}</div>}
          <p>Reels publish to this account as soon as a story is approved and you hit “Upload to Instagram Reels”.</p>
          <Button data-testid="instagram-disconnect-button" variant="outline" size="sm" className="border-rose-500/40 text-rose-300 hover:bg-rose-500/10" onClick={disconnect}>
            <XCircle className="mr-1.5 h-3.5 w-3.5" /> Disconnect
          </Button>
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          <p className="text-xs leading-relaxed text-slate-400">
            From a Meta app with <span className="text-slate-200">instagram_business_basic</span> + <span className="text-slate-200">instagram_business_content_publish</span> permissions, generate a <span className="text-slate-200">long-lived access token</span> for your Instagram Professional account and paste it here with the account ID. (You can also use the API Keys vault above.)
          </p>
          <Input data-testid="instagram-token-input" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Long-lived access token" className="border-white/10 bg-black/30 font-mono2 text-xs text-slate-200" />
          <Input data-testid="instagram-userid-input" value={uid} onChange={(e) => setUid(e.target.value)} placeholder="Instagram Professional user ID (e.g. 1784xxxxxxxx)" className="border-white/10 bg-black/30 font-mono2 text-xs text-slate-200" />
          <Button data-testid="instagram-save-button" onClick={save} disabled={saving} className="bg-fuchsia-600 font-semibold text-white hover:bg-fuchsia-500">
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plug className="mr-2 h-4 w-4" />}
            Validate &amp; Connect
          </Button>
        </div>
      )}
    </div>
  );
}

function YouTubeCard({ creds }) {
  return (
    <div data-testid="youtube-settings-card" className="card-glow rounded-2xl border border-red-500/20 bg-[#12141F] p-6">
      <div className="flex items-center gap-2">
        <Youtube className="h-5 w-5 text-red-500" />
        <h3 className="font-display text-lg font-semibold text-slate-100">YouTube Shorts</h3>
        <Badge data-testid="youtube-status-badge" className={`border text-[10px] ${creds?.youtube ? "border-emerald-600/50 bg-emerald-950/60 text-emerald-300" : "border-amber-500/40 bg-amber-950/30 text-amber-300"}`}>
          {creds?.youtube ? "connected" : "needs OAuth consent"}
        </Badge>
      </div>
      <p className="mt-4 text-xs leading-relaxed text-slate-400">
        One-time OAuth (Data API v3): add the redirect URI in Google Cloud Console, authorize access, and the refresh token is stored automatically. Shorts upload then works from any approved story. Client ID/Secret can be pasted in the vault above.
      </p>
      <Link to="/social">
        <Button data-testid="youtube-manage-link" variant="outline" size="sm" className="mt-4 border-red-500/40 text-red-300 hover:bg-red-500/10">
          <ShieldCheck className="mr-1.5 h-3.5 w-3.5" /> Manage connection
        </Button>
      </Link>
    </div>
  );
}

function SchedulerCard({ sched, refreshSched }) {
  const [engagementHours, setEngagementHours] = useState("6");
  const [savingSched, setSavingSched] = useState(false);

  useEffect(() => {
    if (sched?.engagement_hours != null) setEngagementHours(String(sched.engagement_hours));
  }, [sched?.engagement_hours]);

  const saveScheduler = async () => {
    setSavingSched(true);
    try {
      const r = await api.put("/settings/scheduler", { engagement_hours: Number(engagementHours) });
      toast.success(`Engagement agent will run every ${r.data.engagement_hours}h`);
      refreshSched();
    } catch {
      toast.error("Save failed");
    } finally {
      setSavingSched(false);
    }
  };

  return (
    <div data-testid="scheduler-card" className="card-glow rounded-2xl border border-amber-500/10 bg-[#12141F] p-6">
      <div className="mb-4 flex items-center gap-2">
        <RefreshCw className="h-5 w-5 text-amber-400" />
        <h3 className="font-display text-lg font-semibold text-amber-300">Automation Schedule</h3>
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <div className="text-xs text-slate-300">
          Engagement agent runs every
          <input data-testid="engagement-hours-input" type="number" min="0.25" max="72" step="0.25"
            value={engagementHours}
            onChange={(e) => setEngagementHours(e.target.value)}
            className="mx-2 w-20 rounded-lg border border-white/15 bg-black/40 px-2 py-1 text-center font-mono2 text-xs text-slate-200" />
          hours
        </div>
        <Button data-testid="save-scheduler-button" size="sm" onClick={saveScheduler} disabled={savingSched} className="bg-amber-500 font-semibold text-[#090A0F] hover:bg-amber-400">
          {savingSched ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" />} Save Schedule
        </Button>
        <span className="text-[11px] text-slate-500">Default 6h — lower it during launches, raise it to save API quota.</span>
      </div>
    </div>
  );
}

function RouterHealthCard({ routerStatus, refresh }) {
  return (
    <div data-testid="router-health-card" className="card-glow rounded-2xl border border-amber-500/10 bg-[#12141F] p-6">
      <div className="mb-4 flex items-center gap-2">
        <ShieldCheck className="h-5 w-5 text-emerald-400" />
        <h3 className="font-display text-lg font-semibold text-amber-300">Model Router Health</h3>
        <Button data-testid="router-refresh-button" variant="outline" size="sm" className="ml-auto border-white/15 text-slate-300" onClick={refresh}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Refresh
        </Button>
      </div>
      <p className="mb-4 text-xs leading-relaxed text-slate-400">{routerStatus?.notes}</p>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(routerStatus?.providers || {}).map(([name, p]) => (
          <div key={name} data-testid={`router-provider-${name}`} className="flex items-center justify-between rounded-xl bg-black/30 px-3 py-2 text-xs">
            <span className="font-mono2 text-slate-300">{name}</span>
            <span className="flex items-center gap-1.5 text-[10px] text-slate-500">
              {p.key ? "key ✓" : "no key"}
              {p.capable === false && <span className="text-orange-400">needs GPU/RAM</span>}
              <span className={`h-2 w-2 rounded-full ${p.healthy ? "bg-emerald-400" : "bg-rose-500"}`} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [ig, refreshIg] = usePoll("/settings/instagram", 20000);
  const [keys, refreshKeys] = usePoll("/settings/api-keys", 30000);
  const [creds] = usePoll("/social/credentials", 30000);
  const [routerStatus] = usePoll("/router-status", 30000);
  const [sched, refreshSched] = usePoll("/settings/scheduler", 30000);

  return (
    <div className="mx-auto max-w-5xl space-y-8 pb-16">
      <div>
        <h1 className="font-display text-3xl font-extrabold tracking-tight text-slate-100 lg:text-4xl">Integrations &amp; API Keys</h1>
        <p className="mt-1 text-sm text-slate-400">Every key is configurable here — saved server-side and applied instantly. The model router picks the best provider that still has quota.</p>
      </div>

      <EngineSettingsCard />
      <GenerationDiagnostics />
      <ApiKeysVault keys={keys} refreshKeys={refreshKeys} />

      <div className="grid gap-5 lg:grid-cols-2">
        <InstagramCard ig={ig} refreshIg={refreshIg} />
        <YouTubeCard creds={creds} />
      </div>

      <SchedulerCard sched={sched} refreshSched={refreshSched} />

      <RouterHealthCard routerStatus={routerStatus} refresh={refreshIg} />
    </div>
  );
}
