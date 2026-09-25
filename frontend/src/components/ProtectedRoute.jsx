import { useEffect, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { Flame, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

// Lets everyone through when the backend runs with AUTH_REQUIRED=false; otherwise needs a Google session.
export default function ProtectedRoute({ children }) {
  const location = useLocation();
  const [state, setState] = useState(location.state?.user ? "authed" : "checking");

  useEffect(() => {
    if (state !== "checking") return;
    api.get("/auth/config")
      .then((c) => (c.data.auth_required ? api.get("/auth/me") : null))
      .then(() => setState("authed"))
      .catch(() => setState("anon"));
  }, [state]);

  if (state === "checking") {
    return (
      <div className="grain flex min-h-screen items-center justify-center" style={{ background: "#090A0F" }}>
        <Flame className="h-8 w-8 animate-pulse text-amber-400" />
        <Loader2 className="ml-3 h-5 w-5 animate-spin text-slate-400" />
      </div>
    );
  }
  if (state === "anon") return <Navigate to="/login" replace />;
  return children;
}
