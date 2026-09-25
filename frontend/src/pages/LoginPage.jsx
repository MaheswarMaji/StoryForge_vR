import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Flame, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

const GSI_SRC = "https://accounts.google.com/gsi/client";

function loadGoogleScript() {
  return new Promise((resolve, reject) => {
    if (window.google?.accounts?.id) return resolve();
    const existing = document.querySelector(`script[src="${GSI_SRC}"]`);
    const script = existing || document.createElement("script");
    script.addEventListener("load", () => resolve());
    script.addEventListener("error", () => reject(new Error("Could not load Google Sign-In")));
    if (!existing) {
      script.src = GSI_SRC;
      script.async = true;
      document.head.appendChild(script);
    }
  });
}

export default function LoginPage() {
  const navigate = useNavigate();
  const buttonRef = useRef(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.get("/auth/config");
        if (!data.google_client_id) {
          setError("Google sign-in is not configured. Set GOOGLE_CLIENT_ID in backend/.env.");
          return;
        }
        await loadGoogleScript();
        if (cancelled || !buttonRef.current) return;
        window.google.accounts.id.initialize({
          client_id: data.google_client_id,
          callback: async ({ credential }) => {
            setBusy(true);
            setError("");
            try {
              const r = await api.post("/auth/session", { credential });
              navigate("/dashboard", { replace: true, state: { user: r.data.user } });
            } catch (e) {
              setError(e.response?.data?.detail || "Sign-in failed. Try again.");
              setBusy(false);
            }
          },
        });
        window.google.accounts.id.renderButton(buttonRef.current, {
          theme: "outline", size: "large", text: "signin_with", shape: "rectangular", width: 320,
        });
      } catch (e) {
        if (!cancelled) setError(e.message || "Could not start Google sign-in.");
      }
    })();
    return () => { cancelled = true; };
  }, [navigate]);

  return (
    <div className="grain flex min-h-screen items-center justify-center px-6" style={{ background: "#090A0F" }}>
      <div className="rise w-full max-w-md rounded-3xl border border-amber-500/15 bg-[#0B0D16]/85 p-10 text-center shadow-[0_30px_80px_rgba(0,0,0,0.6)] backdrop-blur-xl">
        <div className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-500/15 ring-1 ring-amber-500/40">
          <Flame className="h-8 w-8 text-amber-400" />
        </div>
        <h1 data-testid="login-heading" className="font-display text-3xl font-extrabold tracking-tight text-amber-100">StoryForge</h1>
        <p className="mt-2 text-sm text-slate-400">Mythology, folk tales &amp; public-interest news — your AI video factory.</p>
        <div className="mt-8 flex min-h-[44px] items-center justify-center">
          <div ref={buttonRef} data-testid="google-login-button" />
          {busy && <Loader2 className="ml-3 h-4 w-4 animate-spin text-amber-400" />}
        </div>
        {error && <p data-testid="login-error" className="mt-4 text-xs text-red-400">{error}</p>}
        <p className="mt-6 text-[11px] leading-relaxed text-slate-600">
          Sign in with your Google account. Your session stays valid for 7 days.
        </p>
      </div>
    </div>
  );
}
