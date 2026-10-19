import { FormEvent, useEffect, useState } from "react";
import { api, fmtDate, fmtDateTime } from "../api";
import { Banner, Chip, Empty, ErrorBox, Loading, Tabs, useFetch } from "../ui";

type Tab = "jobs" | "sources" | "roster" | "search" | "themes" | "trends" | "limits" | "users" | "audit";
const TABS: { id: Tab; label: string }[] = [
  { id: "jobs", label: "Jobs" }, { id: "sources", label: "Sources" }, { id: "roster", label: "Brands & regions" }, { id: "search", label: "Search data" },
  { id: "themes", label: "Themes & keywords" }, { id: "trends", label: "Trends & discovery" }, { id: "limits", label: "Limits & spend" }, { id: "users", label: "Users" }, { id: "audit", label: "Audit log" },
];

export default function Admin() {
  const [tab, setTab] = useState<Tab>((new URLSearchParams(window.location.search).get("tab") as Tab) || "jobs");
  return (
    <div>
      <div className="page-head"><div><h1>Administration</h1><p>Configuration, evidence review and job controls. Reader-facing screens never show this vocabulary.</p></div></div>
      <Tabs value={tab} onChange={setTab} items={TABS} />
      {tab === "jobs" && <Jobs />}{tab === "sources" && <Sources />}{tab === "roster" && <Roster />}{tab === "search" && <Search />}
      {tab === "themes" && <Themes />}{tab === "trends" && <Trends />}{tab === "limits" && <Limits />}{tab === "users" && <Users />}{tab === "audit" && <Audit />}
    </div>
  );
}

const statusTone = (s: string) => (s === "succeeded" ? "good" : s === "failed" ? "bad" : s === "partial" || s === "skipped" ? "warn" : undefined);

function Jobs() {
  const d = useFetch(`/api/admin/jobs`);
  const diag = useFetch(`/api/admin/diagnostics`);
  const [msg, setMsg] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [test, setTest] = useState<any>(null);
  const [testing, setTesting] = useState(false);
  const runTest = async () => { setTesting(true); try { setTest(await api.post("/api/admin/test-connections")); } catch (e) { setTest({ error: (e as Error).message }); } finally { setTesting(false); } };
  const run = async (name: string, params: any = {}) => { try { const r = await api.post("/api/admin/jobs/run", { name, params }); setMsg(`Started job ${r.job_id} (${name.replace(/_/g, " ")}). It runs in the background; this table refreshes by itself.`); d.reload(); } catch (e) { setMsg((e as Error).message); } };
  const active = (d.data?.runs ?? []).filter((r: any) => r.status === "queued" || r.status === "running");
  const anyActive = active.length > 0;
  useEffect(() => {
    if (!anyActive && !msg.startsWith("Started")) return;
    const t = setInterval(d.reload, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyActive, msg]);
  const elapsed = (r: any) => { const t0 = new Date(r.started_at ?? r.queued_at).getTime(); const sec = Math.max(0, Math.round((Date.now() - t0) / 1000)); return sec < 90 ? `${sec}s` : `${Math.floor(sec / 60)}m ${sec % 60}s`; };
  if (!d.data) return d.error ? <ErrorBox message={d.error} /> : <Loading />;
  return (
    <div className="stack">
      {diag.data && <div className="grid g3">
        <div className="tile"><div className="label">AI model (Claude)</div><div className="value" style={{ fontSize: 17 }}>{diag.data.ai.connected ? "Connected" : "Not connected"}</div><div className="sub">{diag.data.ai.model ?? diag.data.ai.reason}</div></div>
        <div className="tile"><div className="label">Google Trends (DataForSEO)</div><div className="value" style={{ fontSize: 17 }}>{diag.data.dataforseo.connected ? "Connected" : "Not connected"}</div><div className="sub">{diag.data.trend_capabilities.filter((c: any) => c.supported).length} of {diag.data.trend_capabilities.length} geographies verified</div></div>
        <div className="tile"><div className="label">Bot-protected sources</div><div className="value" style={{ fontSize: 17 }}>{diag.data.sources.filter((s: any) => s.last_outcome === "blocked").length}</div><div className="sub">Blocked by a bot challenge (never bypassed). Allowlisting header configured: {diag.data.extra_header_allowlist_configured ? "yes" : "no"}</div></div>
      </div>}
      <section className="card">
        <div className="row between"><h3>Connections</h3><button className="btn sm primary" onClick={runTest} disabled={testing}>{testing ? "Testing…" : "Test Anthropic and DataForSEO now"}</button></div>
        <p className="small muted" style={{ margin: "6px 0" }}>Makes one tiny real call to each provider, so a wrong key, wrong model id or missing variable shows up here with the provider’s own message.</p>
        {test?.error && <Banner tone="bad">{test.error}</Banner>}
        {test?.anthropic && <div className="stack small">
          <div><Chip tone={test.anthropic.ok ? "good" : "bad"}>Anthropic: {test.anthropic.ok ? "working" : "not working"}</Chip> {test.anthropic.detail}</div>
          <div><Chip tone={test.dataforseo.ok ? "good" : "bad"}>DataForSEO: {test.dataforseo.ok ? "working" : "not working"}</Chip> {test.dataforseo.detail}</div>
          <div className="muted">Variables set on the web service: {Object.entries(test.environment_seen_by_web_service).map(([k, v]) => `${k} ${v ? "✓" : "✗ missing"}`).join(" · ")}</div>
          <div className="muted">{test.note}</div></div>}
        {(() => { const last = d.data.runs.find((r: any) => r.trigger === "schedule" && r.result?.connections); return last ? <p className="small" style={{ margin: "8px 0 0" }}>The last <b>scheduled</b> run (cron service, {last.name}, {fmtDateTime(last.finished_at)}) saw: AI <Chip tone={last.result.connections.ai ? "good" : "bad"}>{last.result.connections.ai ? "connected" : "not connected"}</Chip> Trends <Chip tone={last.result.connections.trends ? "good" : "bad"}>{last.result.connections.trends ? "connected" : "not connected"}</Chip></p> : null; })()}
      </section>
      {anyActive && <Banner tone="info"><strong>Running now:</strong> {active.map((r: any) => `${r.name.replace(/_/g, " ")} (${r.status}, ${elapsed(r)})`).join(" · ")}. Updates every few seconds; you can leave this page and come back. Calls to DataForSEO take about 30 seconds each, so the capability test needs a few minutes.</Banner>}
      {!anyActive && msg && <Banner tone="info">{msg}</Banner>}
      <section className="card">
        <div className="row between"><h3>Run a job now</h3></div>
        <div className="row" style={{ marginTop: 8 }}>{d.data.available.filter((n: string) => n !== "import_gsc").map((n: string) => <button key={n} className="btn sm" onClick={() => run(n)}>{n.replace(/_/g, " ")}</button>)}</div>
        <p className="tiny faint">Scheduled (Australia/Sydney time, checked hourly): {d.data.schedule.map((s: any) => `${s.job} ${s.cadence === "weekly" ? "Mon" : "daily"} ${String(s.hour_sydney).padStart(2, "0")}:00`).join(" · ")}. Due now: {d.data.due.join(", ") || "none"}.</p>
      </section>
      <div className="tablewrap"><table><thead><tr><th>#</th><th>Job</th><th>Status</th><th>Trigger</th><th>Started</th><th>Finished</th><th>Result</th><th /></tr></thead><tbody>
        {d.data.runs.map((r: any) => (<><tr key={r.id}><td>{r.id}</td><td>{r.name}</td><td><Chip tone={statusTone(r.status) as any}>{r.status === "running" ? `running ${elapsed(r)}` : r.status}</Chip></td><td>{r.trigger}</td><td className="small">{fmtDateTime(r.started_at)}</td><td className="small">{fmtDateTime(r.finished_at)}</td>
          <td className="small" style={{ maxWidth: 380 }}>{r.result?.not_connected ? <span className="muted">Not connected: {r.result.not_connected}</span> : Object.entries(r.result ?? {}).filter(([k]) => k !== "errors").slice(0, 4).map(([k, v]) => `${k}: ${typeof v === "object" ? "…" : v}`).join(" · ")}</td>
          <td><button className="btn sm" onClick={() => setOpen(open === r.id ? null : r.id)}>Details</button> {(r.status === "failed" || r.status === "partial") && <button className="btn sm" onClick={async () => { await api.post(`/api/admin/jobs/${r.id}/retry`); d.reload(); }}>Retry</button>}</td></tr>
          {open === r.id && <tr key={r.id + "d"}><td colSpan={8}><pre className="small" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{r.error || JSON.stringify(r.result, null, 2)}</pre></td></tr>}</>))}</tbody></table></div>
    </div>
  );
}

function Feeds() {
  const d = useFetch(`/api/admin/feeds`);
  const [nu, setNu] = useState({ url: "", label: "" });
  const [msg, setMsg] = useState("");
  const add = async (e: FormEvent) => { e.preventDefault(); setMsg("Checking the feed…"); try { const r = await api.post("/api/admin/feeds", { url: nu.url, label: nu.label || null }); setNu({ url: "", label: "" }); d.reload(); setMsg(`Added – found ${r.items_found} items, e.g. “${r.sample[0] ?? ""}”. It is read with the daily news run.`); } catch (x) { setMsg((x as Error).message); } };
  return (
    <section className="card stack">
      <div><h3 style={{ margin: 0 }}>News feeds (RSS / Atom)</h3><p className="small muted" style={{ margin: "2px 0 0" }}>Add a feed that covers the brands – for example a Google Alerts feed set to “deliver to RSS feed”. Items that name a monitored builder show up in Competitor activity; relevant housing news shows in Market conditions. The feed is tested when you add it.</p></div>
      <form className="row" onSubmit={add} style={{ alignItems: "flex-end" }}>
        <label className="field" style={{ flex: 1, minWidth: 280 }}>Feed URL<input type="text" value={nu.url} onChange={(e) => setNu({ ...nu, url: e.target.value })} required placeholder="https://www.google.com.au/alerts/feeds/…" /></label>
        <label className="field">Label<input type="text" value={nu.label} onChange={(e) => setNu({ ...nu, label: e.target.value })} placeholder="All-brands alert" /></label>
        <button className="btn primary">Add feed</button><span className="small muted">{msg}</span>
      </form>
      {d.data?.length > 0 && <div className="tablewrap"><table><thead><tr><th>Feed</th><th>Last read</th><th className="r">Items</th><th>Status</th><th /></tr></thead><tbody>
        {d.data.map((f: any) => <tr key={f.id} style={{ opacity: f.active ? 1 : .5 }}><td><b>{f.label ?? "Feed"}</b><div className="tiny faint" style={{ wordBreak: "break-all", maxWidth: 420 }}>{f.url}</div></td><td className="small">{fmtDateTime(f.last_fetched_at)}</td><td className="r num">{f.items_last_run ?? "–"}</td>
          <td>{f.last_error ? <Chip tone="bad">{f.last_error.slice(0, 60)}</Chip> : f.last_fetched_at ? <Chip tone="good">ok</Chip> : <Chip>not read yet</Chip>}</td>
          <td className="row"><button className="btn sm" onClick={async () => { await api.patch(`/api/admin/feeds/${f.id}`, { active: !f.active }); d.reload(); }}>{f.active ? "Disable" : "Enable"}</button><button className="btn sm" onClick={async () => { await api.del(`/api/admin/feeds/${f.id}`); d.reload(); }}>Remove</button></td></tr>)}</tbody></table></div>}
    </section>
  );
}

function Sources() {
  const d = useFetch(`/api/admin/sources`);
  const brands = useFetch(`/api/admin/brands`);
  const [pasteFor, setPasteFor] = useState<any>(null);
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");
  const [nu, setNu] = useState({ brand_slug: "", kind: "offer_listing", url: "", state: "" });
  const [ns, setNs] = useState({ brand_slug: "", kind: "offers", query: "" });
  const addSearch = async (e: FormEvent) => { e.preventDefault(); try { await api.post("/api/admin/searches", ns); setNs({ ...ns, query: "" }); d.reload(); setMsg("Search added – it runs with the next daily collection, or press Fetch on its row"); } catch (x) { setMsg((x as Error).message); } };
  const add = async (e: FormEvent) => { e.preventDefault(); try { await api.post("/api/admin/sources", { ...nu, state: nu.state || null }); setNu({ ...nu, url: "" }); d.reload(); setMsg("Added"); } catch (x) { setMsg((x as Error).message); } };
  if (!d.data) return d.error ? <ErrorBox message={d.error} /> : <Loading />;
  return (
    <div className="stack">
      <form className="card row" onSubmit={add} style={{ alignItems: "flex-end" }}>
        <label className="field">Brand<select value={nu.brand_slug} onChange={(e) => setNu({ ...nu, brand_slug: e.target.value })} required><option value="">Choose…</option>{brands.data?.map((b: any) => <option key={b.slug} value={b.slug}>{b.name}</option>)}</select></label>
        <label className="field">Kind<select value={nu.kind} onChange={(e) => setNu({ ...nu, kind: e.target.value })}>{["offer_listing", "offer_detail", "terms", "pdf", "competitor_page", "discovery_seed"].map((k) => <option key={k}>{k}</option>)}</select></label>
        <label className="field">State<select value={nu.state} onChange={(e) => setNu({ ...nu, state: e.target.value })}><option value="">Not state-specific</option>{["VIC", "NSW", "QLD", "SA"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label className="field" style={{ flex: 1, minWidth: 260 }}>URL<input type="text" value={nu.url} onChange={(e) => setNu({ ...nu, url: e.target.value })} required /></label>
        <button className="btn primary">Add source</button><span className="small muted">{msg}</span>
      </form>
      <form className="card row" onSubmit={addSearch} style={{ alignItems: "flex-end" }}>
        <label className="field">Brand<select value={ns.brand_slug} onChange={(e) => setNs({ ...ns, brand_slug: e.target.value })} required><option value="">Choose…</option>{brands.data?.map((b: any) => <option key={b.slug} value={b.slug}>{b.name}</option>)}</select></label>
        <label className="field">Google search for<select value={ns.kind} onChange={(e) => setNs({ ...ns, kind: e.target.value })}><option value="offers">Offers (own-site results)</option><option value="news">News (activity)</option></select></label>
        <label className="field" style={{ flex: 1, minWidth: 260 }}>Search text<input type="text" value={ns.query} maxLength={120} placeholder="e.g. Metricon first home buyer offer" onChange={(e) => setNs({ ...ns, query: e.target.value })} required /></label>
        <button className="btn primary">Add search</button><span className="small faint">About $0.002 per search per day. No <code>site:</code> operators.</span>
      </form>
      <div className="tablewrap"><table><thead><tr><th>Brand</th><th>Kind</th><th>URL / search</th><th>State</th><th>Last outcome</th><th>Last success</th><th>Health</th><th /></tr></thead><tbody>
        {d.data.map((s: any) => (<tr key={s.id} style={{ opacity: s.active ? 1 : .5 }}><td>{s.brand}</td><td className="small">{s.kind}</td><td className="small" style={{ maxWidth: 360, wordBreak: "break-all" }}>{s.query ? <><b>“{s.query}”</b> <a className="tiny" href={s.url} target="_blank" rel="noreferrer noopener">view on Google</a></> : <a href={s.url} target="_blank" rel="noreferrer noopener">{s.url}</a>}{s.notes && <div className="tiny faint">{s.notes}</div>}</td><td>{s.state ?? "–"}</td>
          <td><Chip tone={s.last_outcome === "ok" ? "good" : s.last_outcome ? "bad" : undefined}>{s.last_outcome ?? "never"}</Chip>{s.last_error && <div className="tiny faint" style={{ maxWidth: 220 }}>{s.last_error}</div>}</td><td className="small">{fmtDateTime(s.last_success_at)}</td>
          <td><Chip tone={s.freshness === "fresh" ? "good" : "warn"}>{s.freshness.replace("_", " ")}</Chip></td>
          <td className="row"><button className="btn sm" onClick={async () => { await api.post(`/api/admin/sources/${s.id}/fetch`); setMsg("Fetch queued"); }}>Fetch</button>{!s.query && <button className="btn sm" onClick={() => setPasteFor(s)}>Paste page</button>}
            <button className="btn sm" onClick={async () => { await api.patch(`/api/admin/sources/${s.id}`, { active: !s.active }); d.reload(); }}>{s.active ? "Disable" : "Enable"}</button></td></tr>))}</tbody></table></div>
      <Feeds />
      {pasteFor && <section className="card"><h3>Add page content as evidence – {pasteFor.url}</h3>
        <p className="small muted">For pages a crawler can’t reach (for example bot-protected pages): open the page in a browser, copy the visible text (or view-source HTML) and paste it here. It is stored as a manual snapshot and goes through the same extraction and review.</p>
        <textarea value={text} onChange={(e) => setText(e.target.value)} style={{ minHeight: 200 }} />
        <div className="row" style={{ marginTop: 8 }}><button className="btn primary" disabled={!text.trim()} onClick={async () => { try { await api.post(`/api/admin/sources/${pasteFor.id}/manual-snapshot`, { text, note: "pasted by administrator" }); setMsg("Stored; extraction queued"); setPasteFor(null); setText(""); d.reload(); } catch (e) { setMsg((e as Error).message); } }}>Store snapshot and extract</button><button className="btn" onClick={() => setPasteFor(null)}>Cancel</button></div></section>}
    </div>
  );
}

function Roster() {
  const d = useFetch(`/api/admin/brands`);
  const [nu, setNu] = useState({ name: "", domains: "", states: [] as string[] });
  const [msg, setMsg] = useState("");
  if (!d.data) return d.error ? <ErrorBox message={d.error} /> : <Loading />;
  const toggleState = async (b: any, st: string) => { const states = b.states.includes(st) ? b.states.filter((x: string) => x !== st) : [...b.states, st]; await api.patch(`/api/admin/brands/${b.slug}`, { states }); d.reload(); };
  return (
    <div className="stack">
      <Banner tone="info">{d.data.length} brands, {d.data.reduce((n: number, b: any) => n + b.states.length, 0)} brand/state assignments. Changes apply on the next collection run – no code change needed.</Banner>
      <div className="tablewrap"><table><thead><tr><th>Brand</th><th>Official domains</th><th>States</th><th>Search aliases</th><th>Active</th></tr></thead><tbody>
        {d.data.map((b: any) => (<tr key={b.slug}><td><b>{b.name}</b>{b.is_own && <> <Chip tone="own">Own</Chip></>}{b.notes && <div className="tiny faint" style={{ maxWidth: 260 }}>{b.notes}</div>}</td>
          <td><input type="text" defaultValue={b.domains.join(", ")} onBlur={async (e) => { await api.patch(`/api/admin/brands/${b.slug}`, { domains: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) }); }} style={{ width: 240 }} /></td>
          <td>{["VIC", "NSW", "QLD", "SA"].map((st) => <label key={st} className="small" style={{ marginRight: 8 }}><input type="checkbox" checked={b.states.includes(st)} onChange={() => toggleState(b, st)} /> {st}</label>)}</td>
          <td className="small">{b.aliases.map((a: any) => <div key={a.alias}>{a.alias} <span className="faint tiny">{a.kind}{a.validated ? " · validated" : ""}</span></div>)}</td>
          <td><input type="checkbox" checked={b.active} onChange={async (e) => { await api.patch(`/api/admin/brands/${b.slug}`, { active: e.target.checked }); d.reload(); }} /></td></tr>))}</tbody></table></div>
      <form className="card row" style={{ alignItems: "flex-end" }} onSubmit={async (e) => { e.preventDefault(); try { await api.post("/api/admin/brands", { name: nu.name, domains: nu.domains.split(",").map((x) => x.trim()).filter(Boolean), states: nu.states }); setNu({ name: "", domains: "", states: [] }); d.reload(); setMsg("Added"); } catch (x) { setMsg((x as Error).message); } }}>
        <label className="field">New brand<input type="text" value={nu.name} onChange={(e) => setNu({ ...nu, name: e.target.value })} required /></label>
        <label className="field">Domains (comma separated)<input type="text" value={nu.domains} onChange={(e) => setNu({ ...nu, domains: e.target.value })} /></label>
        {["VIC", "NSW", "QLD", "SA"].map((st) => <label key={st} className="small"><input type="checkbox" checked={nu.states.includes(st)} onChange={() => setNu({ ...nu, states: nu.states.includes(st) ? nu.states.filter((x) => x !== st) : [...nu.states, st] })} /> {st}</label>)}
        <button className="btn primary">Add brand</button><span className="small muted">{msg}</span></form>
    </div>
  );
}

function Search() {
  const g = useFetch(`/api/demand/gsc`);
  const [q, setQ] = useState("");
  const [unmapped, setUnmapped] = useState(true);
  const rows = useFetch(`/api/admin/gsc/queries?limit=60&unmapped=${unmapped}&q=${encodeURIComponent(q)}`, [q, unmapped]);
  const themes = useFetch(`/api/admin/themes`);
  const [file, setFile] = useState<File | null>(null);
  const [period, setPeriod] = useState({ start: "2026-01-01", end: "2026-10-10", note: "Reported by the client: 2026 year to date, approximately to 10 October. The export has no date column." });
  const [msg, setMsg] = useState("");
  const upload = async () => {
    if (!file) return;
    const f = new FormData(); f.append("file", file); f.append("period_start", period.start); f.append("period_end", period.end); f.append("period_note", period.note);
    try { const r = await api.upload("/api/admin/gsc/import", f); setMsg(`Import queued (job ${r.job_id}). Identical data is recognised and not imported twice.`); setTimeout(() => { g.reload(); rows.reload(); }, 6000); } catch (e) { setMsg((e as Error).message); }
  };
  const assign = async (query: string, theme: string | null, excluded = false) => { await api.post("/api/admin/gsc/override", { query, theme, excluded }); rows.reload(); };
  return (
    <div className="stack">
      <section className="card"><h3>Import a Search Console export</h3>
        <p className="small muted">Two tabs: queries and pages. They are stored as separate datasets and never joined or added together. Re-importing the same data changes nothing; a new export is added alongside the old.</p>
        <div className="row" style={{ alignItems: "flex-end" }}><input type="file" accept=".xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <label className="field">Period start<input type="date" value={period.start} onChange={(e) => setPeriod({ ...period, start: e.target.value })} /></label>
          <label className="field">Period end<input type="date" value={period.end} onChange={(e) => setPeriod({ ...period, end: e.target.value })} /></label>
          <label className="field" style={{ flex: 1, minWidth: 260 }}>Note<input type="text" value={period.note} onChange={(e) => setPeriod({ ...period, note: e.target.value })} /></label>
          <button className="btn primary" disabled={!file} onClick={upload}>Import</button></div>
        <div className="small muted" style={{ marginTop: 6 }}>{msg}</div>
        {g.data?.imported && <p className="small">Latest import: <b>{g.data.import.file_name}</b> · {g.data.import.query_rows.toLocaleString("en-AU")} query rows · {g.data.import.page_rows.toLocaleString("en-AU")} page rows · imported {fmtDate(g.data.import.imported_at)}</p>}
        <div className="row"><button className="btn sm" onClick={async () => { await api.post("/api/admin/gsc/remap"); setMsg("Rules re-applied"); rows.reload(); }}>Re-apply theme rules</button><button className="btn sm" onClick={async () => { await api.post("/api/admin/jobs/run", { name: "classify_gsc" }); setMsg("Claude batch classification queued (needs the AI model)"); }}>Classify unmapped with Claude</button></div></section>
      <section className="card"><h3>Review query mappings</h3>
        <div className="row"><input type="text" placeholder="Search queries…" value={q} onChange={(e) => setQ(e.target.value)} /><label className="small"><input type="checkbox" checked={unmapped} onChange={(e) => setUnmapped(e.target.checked)} /> Unmapped only</label></div>
        <div className="tablewrap" style={{ marginTop: 8, maxHeight: 420, overflow: "auto" }}><table><thead><tr><th>Query</th><th className="r">Impressions</th><th className="r">Clicks</th><th>Theme</th><th>Set theme</th></tr></thead><tbody>
          {rows.data?.map((r: any) => (<tr key={r.id}><td>{r.query}</td><td className="r num">{r.impressions.toLocaleString("en-AU")}</td><td className="r num">{r.clicks}</td><td>{r.excluded ? <Chip tone="warn">excluded</Chip> : r.theme ?? <span className="faint">unmapped</span>} <span className="tiny faint">{r.method}</span></td>
            <td><select value="" onChange={(e) => e.target.value === "__x" ? assign(r.query, null, true) : e.target.value && assign(r.query, e.target.value)}><option value="">Choose…</option>{themes.data?.map((t: any) => <option key={t.slug} value={t.slug}>{t.name}</option>)}<option value="__x">Exclude (off-topic)</option></select></td></tr>))}</tbody></table></div></section>
    </div>
  );
}

function Themes() {
  const d = useFetch(`/api/admin/themes`);
  const [nk, setNk] = useState<Record<string, string>>({});
  const [nr, setNr] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  if (!d.data) return d.error ? <ErrorBox message={d.error} /> : <Loading />;
  return (
    <div className="stack">
      <Banner tone="info">Representative keywords are the only terms sent to Google Trends – “tracked” ones are used in the weekly plans (max five per comparison). The full Search Console keyword set is never sent to a paid API. {msg}</Banner>
      {d.data.map((t: any) => (
        <section className="card" key={t.slug}>
          <div className="row between"><h3>{t.name}</h3><label className="small"><input type="checkbox" checked={t.active} onChange={async (e) => { await api.patch(`/api/admin/themes/${t.slug}`, { active: e.target.checked }); d.reload(); }} /> Active</label></div>
          <div className="grid g2" style={{ marginTop: 8 }}>
            <div><h4>Mapping rules</h4>{t.rules.map((r: any) => <div key={r.id} className="small row between" style={{ borderTop: "1px solid var(--line-soft)", padding: "4px 0" }}><code style={{ wordBreak: "break-all" }}>{r.pattern.slice(0, 140)}</code><button className="btn sm danger" onClick={async () => { await api.del(`/api/admin/rules/${r.id}`); d.reload(); }}>×</button></div>)}
              <div className="row" style={{ marginTop: 6 }}><input type="text" placeholder="regex pattern" value={nr[t.slug] ?? ""} onChange={(e) => setNr({ ...nr, [t.slug]: e.target.value })} style={{ flex: 1 }} /><button className="btn sm" onClick={async () => { try { await api.post(`/api/admin/themes/${t.slug}/rules`, { pattern: nr[t.slug], priority: 100 }); setNr({ ...nr, [t.slug]: "" }); d.reload(); } catch (e) { setMsg((e as Error).message); } }}>Add rule</button></div></div>
            <div><h4>Representative keywords</h4>{t.keywords.map((k: any) => <div key={k.id} className="small row between" style={{ borderTop: "1px solid var(--line-soft)", padding: "3px 0" }}><label><input type="checkbox" checked={k.tracked} onChange={async (e) => { await api.patch(`/api/admin/keywords/${k.id}`, { tracked: e.target.checked }); d.reload(); }} /> {k.keyword} <span className="faint tiny">{k.source}{k.gsc_impressions ? ` · ${k.gsc_impressions.toLocaleString("en-AU")} impr.` : ""}</span></label><button className="btn sm danger" onClick={async () => { await api.del(`/api/admin/keywords/${k.id}`); d.reload(); }}>×</button></div>)}
              <div className="row" style={{ marginTop: 6 }}><input type="text" placeholder="add keyword" value={nk[t.slug] ?? ""} onChange={(e) => setNk({ ...nk, [t.slug]: e.target.value })} style={{ flex: 1 }} /><button className="btn sm" onClick={async () => { try { await api.post(`/api/admin/themes/${t.slug}/keywords`, { keyword: nk[t.slug], tracked: false }); setNk({ ...nk, [t.slug]: "" }); d.reload(); } catch (e) { setMsg((e as Error).message); } }}>Add</button></div></div>
          </div></section>))}
    </div>
  );
}

function Trends() {
  const plans = useFetch(`/api/admin/trend-plans`);
  const rel = useFetch(`/api/admin/related?status=proposed`);
  const caps = useFetch(`/api/demand/capabilities`);
  const [msg, setMsg] = useState("");
  return (
    <div className="stack">
      {msg && <Banner tone={msg.startsWith("Could not") || msg.startsWith("Failed") ? "bad" : "info"}>{msg}</Banner>}
      <section className="card"><div className="row between"><h3>Provider capability tests</h3><button className="btn sm" onClick={async () => { try { await api.post("/api/admin/jobs/run", { name: "trends_capability", params: { cities: true } }); setMsg("Capability test started: 9 live calls, roughly 5 minutes. Watch it under the Jobs tab; this page won\u2019t update until you reload it afterwards."); } catch (e) { setMsg("Could not start the test: " + (e as Error).message); } }}>Run capability test (incl. cities)</button></div>
        <p className="small muted">{msg} Tests resolve each location from the provider’s current catalogue and check a generic home-building query returns usable data.</p>
        <div className="tablewrap"><table><thead><tr><th>Geography</th><th>Tested</th><th>Supported</th><th>Coverage</th><th>Detail</th></tr></thead><tbody>{caps.data?.map((c: any) => <tr key={c.geo}><td>{c.geo}</td><td>{c.tested ? fmtDate(c.tested_at) : "Not tested"}</td><td>{c.supported == null ? "–" : c.supported ? "Yes" : "No"}</td><td>{c.coverage ?? "–"}</td><td className="small muted">{c.detail?.reason ?? ""}</td></tr>)}</tbody></table></div></section>
      <section className="card"><div className="row between"><h3>Refresh plans</h3><button className="btn sm" onClick={async () => { try { const r = await api.post("/api/admin/trend-plans/activate-all"); setMsg(`Activated ${r.activated} plans. They are fetched on the next weekly run, or run “refresh trends” under Jobs now.`); plans.reload(); } catch (e) { setMsg("Failed: " + (e as Error).message); } }}>Activate all plans</button></div><p className="small muted">Each plan is one comparable request (≤5 keywords, one geography). Only the 8 pilot plans (Australia and Victoria) start active, so NSW, QLD and SA stay empty until you switch them on. All 61 plans cost roughly $0.67 a week at DataForSEO’s list price.</p>
        <div className="tablewrap" style={{ maxHeight: 360, overflow: "auto" }}><table><thead><tr><th>Plan</th><th>Geo</th><th>Keywords</th><th>Pilot</th><th>Active</th></tr></thead><tbody>
          {plans.data?.map((p: any) => <tr key={p.id}><td className="small">{p.key}</td><td>{p.geo}</td><td className="small">{p.keywords.join(", ")}</td><td>{p.pilot ? "Yes" : ""}</td><td><input type="checkbox" checked={p.active} onChange={async (e) => { await api.patch(`/api/admin/trend-plans/${p.id}`, { active: e.target.checked }); plans.reload(); }} /></td></tr>)}</tbody></table></div></section>
      <section className="card"><h3>Proposed subthemes to review</h3>
        {rel.data?.length ? <div className="tablewrap"><table><thead><tr><th>Query</th><th>Seed</th><th>Kind</th><th>Suggested theme</th><th /></tr></thead><tbody>{rel.data.map((r: any) => <tr key={r.id}><td>{r.query}</td><td>{r.seed}</td><td>{r.kind}</td><td>{r.suggested_theme ?? "–"}</td>
          <td className="row"><button className="btn sm" onClick={async () => { await api.patch(`/api/admin/related/${r.id}`, { status: "accepted" }); rel.reload(); }}>Accept as keyword</button><button className="btn sm" onClick={async () => { await api.patch(`/api/admin/related/${r.id}`, { status: "rejected" }); rel.reload(); }}>Reject</button></td></tr>)}</tbody></table></div> : <Empty>Nothing waiting for review.</Empty>}</section>
    </div>
  );
}

function Limits() {
  const d = useFetch(`/api/admin/limits`);
  const [v, setV] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  if (!d.data) return d.error ? <ErrorBox message={d.error} /> : <Loading />;
  const L = d.data.limits;
  const fields: [string, string, number][] = [["dataforseo_daily_usd_limit", "DataForSEO daily limit (USD)", L.dataforseo_daily_usd], ["dataforseo_monthly_usd_limit", "DataForSEO monthly limit (USD)", L.dataforseo_monthly_usd], ["dataforseo_max_requests_per_job", "DataForSEO requests per job", L.dataforseo_max_requests_per_job], ["anthropic_max_calls_per_job", "Claude calls per job", L.anthropic_max_calls_per_job], ["anthropic_daily_call_limit", "Claude calls per day", L.anthropic_daily_calls]];
  return (
    <div className="stack">
      <Banner tone="info">{d.data.note}</Banner>
      <section className="card"><div className="grid g3">{fields.map(([k, label, cur]) => <label className="field" key={k}>{label}<input type="number" step="any" defaultValue={cur} onChange={(e) => setV({ ...v, [k]: e.target.value })} /></label>)}</div>
        <div className="row" style={{ marginTop: 10 }}><button className="btn primary" onClick={async () => { const body: any = {}; Object.entries(v).forEach(([k, x]) => (body[k] = Number(x))); try { await api.put("/api/admin/limits", body); setMsg("Saved"); d.reload(); } catch (e) { setMsg((e as Error).message); } }}>Save limits</button><span className="small muted">{msg}</span></div></section>
      <div className="grid g2">{(["dataforseo", "anthropic"] as const).map((p) => <section className="card" key={p}><h3>{p === "dataforseo" ? "DataForSEO" : "Anthropic"} · spend so far</h3>
        <dl className="kv" style={{ marginTop: 8 }}><dt>Today – reported (USD)</dt><dd className="num">{d.data.spent[p].day_reported.toFixed(4)}</dd><dt>Today – estimated (USD)</dt><dd className="num">{d.data.spent[p].day_estimated.toFixed(4)}</dd><dt>Month – reported</dt><dd className="num">{d.data.spent[p].month_reported.toFixed(4)}</dd><dt>Month – estimated</dt><dd className="num">{d.data.spent[p].month_estimated.toFixed(4)}</dd><dt>Calls today</dt><dd className="num">{d.data.spent[p].day_calls}</dd></dl></section>)}</div>
    </div>
  );
}

function Users() {
  const d = useFetch(`/api/admin/users`);
  const [nu, setNu] = useState({ email: "", password: "", role: "viewer", name: "" });
  const [msg, setMsg] = useState("");
  return (
    <div className="stack">
      <form className="card row" style={{ alignItems: "flex-end" }} onSubmit={async (e) => { e.preventDefault(); try { await api.post("/api/admin/users", nu); setNu({ email: "", password: "", role: "viewer", name: "" }); d.reload(); setMsg("User created"); } catch (x) { setMsg((x as Error).message); } }}>
        <label className="field">Email<input type="email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} required /></label>
        <label className="field">Name<input type="text" value={nu.name} onChange={(e) => setNu({ ...nu, name: e.target.value })} /></label>
        <label className="field">Temporary password (10+ chars)<input type="password" autoComplete="new-password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} required minLength={10} /></label>
        <label className="field">Role<select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}><option value="viewer">Viewer</option><option value="admin">Administrator</option></select></label>
        <button className="btn primary">Create account</button><span className="small muted">{msg}</span></form>
      <div className="tablewrap"><table><thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Active</th><th>Last sign-in</th><th /></tr></thead><tbody>
        {d.data?.map((u: any) => <tr key={u.id}><td>{u.email}</td><td>{u.name}</td><td><select value={u.role} onChange={async (e) => { try { await api.patch(`/api/admin/users/${u.id}`, { role: e.target.value }); } catch (x) { setMsg((x as Error).message); } d.reload(); }}><option value="viewer">Viewer</option><option value="admin">Administrator</option></select></td>
          <td><input type="checkbox" checked={u.active} onChange={async (e) => { try { await api.patch(`/api/admin/users/${u.id}`, { active: e.target.checked }); } catch (x) { setMsg((x as Error).message); } d.reload(); }} /></td><td className="small">{fmtDateTime(u.last_login_at)}</td>
          <td><button className="btn sm" onClick={async () => { const pw = window.prompt("New password (10+ characters)"); if (pw) { try { await api.patch(`/api/admin/users/${u.id}`, { password: pw }); setMsg("Password changed"); } catch (x) { setMsg((x as Error).message); } } }}>Reset password</button></td></tr>)}</tbody></table></div>
    </div>
  );
}

function Audit() {
  const d = useFetch(`/api/admin/audit?limit=200`);
  if (!d.data) return d.error ? <ErrorBox message={d.error} /> : <Loading />;
  return <div className="tablewrap"><table><thead><tr><th>When</th><th>Action</th><th>Entity</th><th>Change</th><th>Note</th></tr></thead><tbody>{d.data.map((a: any, i: number) => <tr key={i}><td className="small">{fmtDateTime(a.at)}</td><td>{a.action}</td><td className="small">{a.entity} {a.entity_id}</td><td className="small" style={{ maxWidth: 420, wordBreak: "break-word" }}>{a.before != null && <del>{JSON.stringify(a.before).slice(0, 160)}</del>} {a.after != null && <ins>{JSON.stringify(a.after).slice(0, 200)}</ins>}</td><td className="small muted">{a.note}</td></tr>)}</tbody></table></div>;
}
