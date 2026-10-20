import { createContext, FormEvent, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { Banner, Loading, useFetch } from "./ui";
import MarketBrief from "./views/MarketBrief";
import Demand from "./views/Demand";
import Offers from "./views/Offers";
import Activity from "./views/Activity";
import Conditions from "./views/Conditions";
import Analysis from "./views/Analysis";
import Admin from "./views/Admin";

export type Geo = "AU" | "VIC" | "NSW" | "QLD" | "SA";
export type User = { email: string; name?: string; role: "viewer" | "admin"; login_required?: boolean; admin_gate?: boolean };
type Ctx = { geo: Geo; setGeo: (g: Geo) => void; meta: any; user: User; navigate: (p: string) => void };
export const AppCtx = createContext<Ctx>(null as any);
export const useApp = () => useContext(AppCtx);

const VIEWS = [
  { path: "/", label: "Market brief" },
  { path: "/demand", label: "Demand trends" },
  { path: "/offers", label: "Offers in market" },
  { path: "/activity", label: "Competitor activity" },
  { path: "/conditions", label: "Market conditions" },
  { path: "/analysis", label: "AI analysis" },
];
const GEOS: { code: Geo; label: string; title: string }[] = [
  { code: "AU", label: "Australia", title: "National context" },
  { code: "VIC", label: "VIC", title: "Victoria (Melbourne primary city)" },
  { code: "NSW", label: "NSW", title: "New South Wales (Sydney)" },
  { code: "QLD", label: "QLD", title: "Queensland (Brisbane)" },
  { code: "SA", label: "SA", title: "South Australia (Adelaide)" },
];

function usePath() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => { const h = () => setPath(window.location.pathname); window.addEventListener("popstate", h); return () => window.removeEventListener("popstate", h); }, []);
  return path;
}

function Login({ onDone }: { onDone: (u: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { onDone(await api.post("/api/auth/login", { email, password })); } catch (x) { setErr((x as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="login"><form className="card stack" onSubmit={submit}>
      <div className="brand-mark"><span className="dot">M</span>Market Intelligence</div>
      <p className="muted small" style={{ margin: 0 }}>Private tool for marketing and sales. Ask an administrator for an account.</p>
      <label className="field">Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
      <label className="field">Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
      {err && <Banner tone="bad">{err}</Banner>}
      <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
    </form></div>
  );
}

function AdminUnlock({ onDone }: { onDone: (u: User) => void }) {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api.post("/api/auth/admin-unlock", { password }); onDone(await api.get<User>("/api/auth/me")); } catch (x) { setErr((x as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="login"><form className="card stack" onSubmit={submit}>
      <h2 style={{ margin: 0 }}>Admin</h2>
      <p className="muted small" style={{ margin: 0 }}>Enter the admin password to manage sources, jobs, limits and settings. Everything else in the dashboard stays open.</p>
      <label className="field">Password<input type="password" autoFocus autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
      {err && <Banner tone="bad">{err}</Banner>}
      <button className="btn primary" disabled={busy}>{busy ? "Checking…" : "Unlock admin"}</button>
    </form></div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const path = usePath();
  const [geo, setGeoState] = useState<Geo>(() => (new URLSearchParams(window.location.search).get("geo") as Geo) || (localStorage.getItem("geo") as Geo) || "AU");

  useEffect(() => { api.get<User>("/api/auth/me").then(setUser).catch((e: ApiError) => setUser(e.status === 401 ? null : null)); }, []);
  const navigate = useCallback((p: string) => { window.history.pushState({}, "", p); window.dispatchEvent(new PopStateEvent("popstate")); window.scrollTo(0, 0); }, []);
  const setGeo = useCallback((g: Geo) => {
    setGeoState(g);
    try { localStorage.setItem("geo", g); } catch { /* storage can be blocked */ }
    const u = new URL(window.location.href); u.searchParams.set("geo", g); window.history.replaceState({}, "", u);
  }, []);
  const meta = useFetch(user ? "/api/meta" : null);

  if (user === undefined) return <Loading />;
  if (user === null) return <Login onDone={setUser} />;
  if (!meta.data) return <Loading />;
  const isAdmin = user.role === "admin";
  const onAdmin = path.startsWith("/admin");
  const logout = async () => { await api.post("/api/auth/logout"); setUser(null); };
  const links = [...VIEWS, ...(isAdmin || user.admin_gate ? [{ path: "/admin", label: "Admin" }] : [])];
  const lock = async () => { await api.post("/api/auth/admin-lock"); setUser(await api.get<User>("/api/auth/me")); navigate("/"); };
  const active = links.find((v) => (v.path === "/" ? path === "/" : path.startsWith(v.path)))?.path ?? "/";

  return (
    <AppCtx.Provider value={{ geo, setGeo, meta: meta.data, user, navigate }}>
      <header className="topbar"><div className="topbar-in">
        <div className="brand-mark"><span className="dot" aria-hidden>M</span>Market Intelligence</div>
        <nav className="nav" aria-label="Main">{links.map((v) => <a key={v.path} href={v.path + `?geo=${geo}`} className={active === v.path ? "on" : ""} onClick={(e) => { e.preventDefault(); navigate(v.path + `?geo=${geo}`); }}>{v.label}</a>)}</nav>
        <div className="spacer" />
        {!onAdmin && <div className="geo" role="group" aria-label="Geography">{GEOS.map((g) => <button key={g.code} title={g.title} className={geo === g.code ? "on" : ""} onClick={() => setGeo(g.code)}>{g.label}</button>)}</div>}
        {user.admin_gate && isAdmin && <div className="userbox"><button className="btn sm" onClick={lock}>Lock admin</button></div>}
        {user.login_required && <div className="userbox"><span className="small muted">{user.email}</span><button className="btn sm" onClick={logout}>Sign out</button></div>}
      </div></header>
      <main className="page">
        {path === "/" && <MarketBrief />}
        {path.startsWith("/demand") && <Demand />}
        {path.startsWith("/offers") && <Offers />}
        {path.startsWith("/activity") && <Activity />}
        {path.startsWith("/conditions") && <Conditions />}
        {path.startsWith("/analysis") && <Analysis />}
        {path.startsWith("/admin") && (isAdmin ? <Admin /> : user.admin_gate ? <AdminUnlock onDone={setUser} /> : <Banner tone="bad">Administrator access required.</Banner>)}
      </main>
    </AppCtx.Provider>
  );
}
