import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApp } from "../App";
import { fmtDate, fmtDateTime } from "../api";
import { Banner, Chip, Empty, ErrorBox, Help, LineSeriesChart, Loading, SERIES, Sparkline, Tabs, SeriesIn, changeLabel, download, useFetch } from "../ui";

type Win = "90" | "365" | "735";
const WINS: { id: Win; label: string }[] = [{ id: "90", label: "90 days" }, { id: "365", label: "12 months" }, { id: "735", label: "24 months" }];

function cut(points: any[], days: number) {
  if (!points.length) return points;
  const last = new Date(points[points.length - 1].date + "T00:00:00").getTime();
  return points.filter((p) => last - new Date(p.date + "T00:00:00").getTime() <= days * 86400000);
}

/** Same series shifted forward 52 weeks, to overlay "same period last year" on identical axes. */
function lastYear(points: any[]) {
  return points.map((p) => ({ ...p, date: new Date(new Date(p.date + "T00:00:00").getTime() + 364 * 86400000).toISOString().slice(0, 10) }));
}

function GroupChart({ g, win, yoy }: { g: any; win: Win; yoy: boolean }) {
  const series: SeriesIn[] = useMemo(() => {
    const base = g.series.map((s: any, i: number) => ({ key: s.keyword, label: s.brand ?? s.keyword, color: SERIES[i % 8], points: cut(s.points, +win).map((p: any) => ({ date: p.date, value: p.value, partial: p.partial })) }));
    if (!yoy) return base;
    const shifted = g.series.slice(0, 2).map((s: any, i: number) => ({ key: s.keyword + "__ly", label: `${s.brand ?? s.keyword} (a year earlier)`, color: SERIES[i % 8], dashed: true, points: cut(lastYear(s.points), +win) }));
    return [...base, ...shifted];
  }, [g, win, yoy]);
  const m = g.meta;
  return (
    <section className="card">
      <div className="row between">
        <h3>{m.location_name ?? m.geo} · Google Trends search interest</h3>
        <div className="row small muted"><Chip>{m.geo_level === "country" ? "National data" : "Statewide data"}</Chip><span>Retrieved {fmtDateTime(m.retrieved_at)} · {m.search_type} search · category {m.category_code === 0 ? "all" : m.category_code}</span></div>
      </div>
      <LineSeriesChart series={series} yDomain={[0, 100]} asOfNote={`${g.scale_note} Data to ${fmtDate(m.date_to)}. The provider’s final, incomplete week is flagged in the table and left out of the chart and the change figures.`} />
      <div className="tablewrap" style={{ marginTop: 12 }}>
        <table>
          <thead><tr><th>Brand</th><th>Search term</th><th>Trend (own scale)<Help text="Each sparkline is scaled to that brand’s own peak. Compare shapes, not heights." /></th><th className="r">Latest 4 weeks vs previous 4</th><th className="r">Latest 4 weeks vs a year earlier</th></tr></thead>
          <tbody>{g.series.map((s: any, i: number) => (
            <tr key={s.keyword} className={s.brand === "Metricon" ? "own" : ""}>
              <td><span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: SERIES[i % 8], marginRight: 8 }} />{s.brand}</td>
              <td className="muted">{s.keyword}</td>
              <td><Sparkline points={s.sparkline_individually_normalised} color={SERIES[i % 8]} /></td>
              <td className="r" title={s.mom4.status === "ok" ? `${s.mom4.current_avg} vs ${s.mom4.comparison_avg} index points` : ""}>{changeLabel(s.mom4)}</td>
              <td className="r" title={s.yoy4.status === "ok" ? `${s.yoy4.current_avg} vs ${s.yoy4.comparison_avg} index points` : ""}>{changeLabel(s.yoy4)}</td>
            </tr>))}</tbody>
        </table>
      </div>
    </section>
  );
}

function Brands({ win, yoy }: { win: Win; yoy: boolean }) {
  const { geo } = useApp();
  const d = useFetch(`/api/demand/brands?geo=${geo}`, [geo]);
  if (d.error) return <ErrorBox message={d.error} />;
  if (!d.data) return <Loading />;
  const groups = d.data.groups;
  const have = groups.filter((g: any) => g.status === "ok");
  return (
    <div className="stack">
      <Banner tone="info"><strong>{d.data.geo_label}.</strong> {d.data.note} Brands are compared in groups of up to five on one shared scale (Metricon in every group). Different groups are separate requests and are never ranked against each other.</Banner>
      {have.length === 0 && <Empty><strong>No search-interest data stored for {geo} yet.</strong><div className="small">Insufficient search data, or the Google Trends provider is not connected / has not covered this geography. Nothing is shown as zero.</div></Empty>}
      {have.map((g: any) => <GroupChart key={g.plan_key} g={g} win={win} yoy={yoy} />)}
      {groups.filter((g: any) => g.status !== "ok").length > 0 && <p className="small muted">Not yet collected: {groups.filter((g: any) => g.status !== "ok").map((g: any) => g.brands.filter((b: string) => b !== "Metricon").join(", ")).join(" · ")}</p>}
    </div>
  );
}

const DIR: Record<string, { label: string; tone?: "good" | "warn" | "bad" | "info" }> = {
  rising: { label: "Rising", tone: "good" }, falling: { label: "Falling", tone: "bad" }, steady: { label: "Steady", tone: "info" }, no_data: { label: "Not enough data" },
};
const nfmt = (n?: number | null) => (n == null ? "–" : Math.round(n).toLocaleString("en-AU"));

/** Index a series to its own recent peak so direction can be compared between terms of very different size. */
function ownScale(points: any[]) {
  const vals = points.filter((p) => !p.partial && p.value != null).slice(-52).map((p) => p.value as number);
  const peak = Math.max(0, ...vals);
  return points.map((p) => ({ ...p, value: p.value == null || !peak ? p.value : Math.round((p.value / peak) * 100) }));
}

function Themes({ win }: { win: Win }) {
  const { geo, navigate } = useApp();
  const d = useFetch(`/api/demand/themes?geo=${geo}`, [geo]);
  const rel = useFetch(`/api/demand/related`, []);
  const [open, setOpen] = useState<string | null>(null);
  const [scale, setScale] = useState<"own" | "shared">("own");
  if (d.error) return <ErrorBox message={d.error} />;
  if (!d.data) return <Loading />;
  const themes = d.data.themes;
  const charted = themes.filter((t: any) => t.status === "ok");
  const sparse = themes.filter((t: any) => t.status === "sparse");
  const pending = themes.filter((t: any) => t.status === "no_data");
  const order: Record<string, number> = { rising: 0, falling: 1, steady: 2, no_data: 3 };
  const ranked = [...charted].sort((a: any, b: any) => (order[a.headline.direction] - order[b.headline.direction]) || ((b.headline.mom4.value_pct ?? -999) - (a.headline.mom4.value_pct ?? -999)));
  return (
    <div className="stack">
      <Banner tone="info"><strong>{d.data.geo_label}.</strong> Each theme follows a few broad search terms. A term appears only if Google Trends reports it in most weeks – very rare searches come back as 0 (“insufficient data”) and are listed instead of being drawn as flat lines. Terms are never averaged into a “market demand” total.</Banner>
      {charted.length === 0 && <Empty><strong>No theme search-interest data stored for {geo} yet.</strong><div className="small">{sparse.length ? "Every term tried so far is searched too rarely to chart. The weekly job swaps in broader terms." : "Theme groups are refreshed weekly once the Trends provider is connected and the group is switched on in Admin."}</div></Empty>}

      {ranked.length > 0 && <section className="card">
        <div className="row between"><div><h3 style={{ margin: 0 }}>Where search interest is moving</h3><p className="small muted" style={{ margin: "2px 0 0" }}>One line per theme, taken from its busiest usable search term. Use it to see what’s heating up, then open the card below or the <a href={`/analysis?geo=${geo}`} onClick={(e) => { e.preventDefault(); navigate(`/analysis?geo=${geo}`); }}>AI analysis</a> for what to do.</p></div></div>
        <div className="tablewrap" style={{ marginTop: 8 }}><table><thead><tr><th>Theme</th><th>Based on the term</th><th>Shape (own scale)</th><th className="r">Latest 4 weeks vs previous 4</th><th className="r">vs a year earlier</th><th>Direction</th><th className="r">Metricon search impressions<Help text="From the Search Console export: how often Metricon appeared for searches in this theme" /></th></tr></thead>
          <tbody>{ranked.map((t: any) => (
            <tr key={t.theme}><td><b>{t.theme_name}</b></td><td className="muted">{t.headline.keyword}</td><td><Sparkline points={t.headline.sparkline} color="var(--s1)" /></td>
              <td className="r">{changeLabel(t.headline.mom4)}</td><td className="r">{changeLabel(t.headline.yoy4)}</td>
              <td><Chip tone={DIR[t.headline.direction].tone}>{DIR[t.headline.direction].label}</Chip></td><td className="r num">{t.gsc ? nfmt(t.gsc.impressions) : <span className="faint">–</span>}</td></tr>))}</tbody></table></div>
      </section>}

      {charted.length > 0 && <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="small muted">Charts show weekly search interest for the window you chose above.</div>
        <div className="row small" role="group" aria-label="Chart scale"><span className="muted">Scale:</span>
          <button className={`btn sm ${scale === "own" ? "primary" : ""}`} onClick={() => setScale("own")} title="Each term indexed to its own recent peak – compares direction">Each term on its own scale</button>
          <button className={`btn sm ${scale === "shared" ? "primary" : ""}`} onClick={() => setScale("shared")} title="Google's shared 0–100 scale – compares size, small terms look flat">Shared scale (compare size)</button></div></div>}

      <div className="grid g2">
        {charted.map((t: any) => {
          const swamped = scale === "shared" ? t.series.filter((s: any) => (s.quality.relative_to_busiest_pct ?? 100) < 20).map((s: any) => s.keyword) : [];
          return (
            <section className="card" key={t.theme}>
              <div className="row between"><h3>{t.theme_name}</h3><div className="row"><Chip tone={DIR[t.headline.direction].tone}>{DIR[t.headline.direction].label}</Chip><button className="btn sm" onClick={() => setOpen(open === t.theme ? null : t.theme)}>{open === t.theme ? "Hide detail" : "Term detail"}</button></div></div>
              <LineSeriesChart height={210} yDomain={[0, 100]} periodLabel="Week starting" asOfNote={scale === "own" ? "Each term is indexed to its own peak over the last 52 weeks (100 = its busiest week). Shows direction, not size." : "Google’s shared scale within this theme: 100 = the busiest term’s busiest week."}
                series={t.series.map((s: any, i: number) => { const pts = cut(s.points, +win).map((p: any) => ({ date: p.date, value: p.value, partial: p.partial })); return { key: s.keyword, label: s.keyword, color: SERIES[i % 8], points: scale === "own" ? ownScale(pts) : pts }; })} />
              {swamped.length > 0 && <p className="tiny faint" style={{ margin: "4px 0 0" }}>{swamped.join(", ")} {swamped.length > 1 ? "are" : "is"} far smaller than the busiest term, so {swamped.length > 1 ? "look" : "looks"} flat here. Switch to “own scale” to see {swamped.length > 1 ? "their" : "its"} shape.</p>}
              {t.sparse.length > 0 && <p className="tiny faint" style={{ margin: "4px 0 0" }}>Too little search volume to chart: {t.sparse.map((x: any) => x.keyword).join(", ")}.</p>}
              {open === t.theme && <div className="tablewrap" style={{ marginTop: 10 }}><table><thead><tr><th>Search term</th><th className="r">Size vs busiest</th><th className="r">4 wks vs previous 4</th><th className="r">vs a year earlier</th></tr></thead>
                <tbody>{t.series.map((s: any) => <tr key={s.keyword}><td>{s.keyword}</td><td className="r num">{s.quality.relative_to_busiest_pct != null ? `${s.quality.relative_to_busiest_pct}%` : "–"}</td><td className="r">{changeLabel(s.mom4)}</td><td className="r">{changeLabel(s.yoy4)}</td></tr>)}</tbody></table></div>}
            </section>);
        })}
      </div>

      {(sparse.length > 0 || pending.length > 0) && <section className="card">
        <h3>Not charted</h3>
        {sparse.length > 0 && <><p className="small muted" style={{ margin: "2px 0 6px" }}>Searched too rarely for Google Trends to report most weeks in this geography. That is “not enough data”, not “no demand” – Metricon’s own Search Console numbers are the better read for these.</p>
          <ul className="small">{sparse.map((t: any) => <li key={t.theme}><b>{t.theme_name}</b> – tried: {t.sparse.map((x: any) => x.keyword).join(", ")}</li>)}</ul></>}
        {pending.length > 0 && <p className="small muted" style={{ margin: "6px 0 0" }}>Waiting for the next weekly refresh: {pending.map((t: any) => t.theme_name).join(", ")}{pending.some((t: any) => !t.active) ? " (some groups are switched off in Admin → Trends)" : ""}.</p>}
      </section>}

      <section className="card">
        <h3>New and rising searches</h3>
        <p className="muted small">Related searches growing on Google, filtered for home-building relevance. The <a href={`/analysis?geo=${geo}`} onClick={(e) => { e.preventDefault(); navigate(`/analysis?geo=${geo}`); }}>AI analysis</a> checks each against Metricon’s visibility and existing pages.</p>
        {rel.data?.length ? <div className="tablewrap"><table><thead><tr><th>Search</th><th>Found through</th><th>Type</th><th>Suggested theme</th><th>Status</th></tr></thead><tbody>
          {rel.data.slice(0, 20).map((r: any) => <tr key={r.id}><td>{r.query}</td><td className="muted">{r.seed}</td><td>{r.kind}{r.value != null ? ` (${r.value >= 5000 ? "breakout" : "+" + r.value + "%"})` : ""}</td><td>{r.theme ?? "–"}</td><td><Chip tone={r.validated_trend ? "good" : "warn"}>{r.label}</Chip></td></tr>)}</tbody></table></div> : <p className="muted small">None yet – they come from the weekly discovery job.</p>}
      </section>
    </div>
  );
}

function GscInsights() {
  const { geo, navigate } = useApp();
  const a = useFetch(`/api/analysis?scope=${geo}`, [geo]);
  const c = a.data?.computed;
  const r = a.data?.report;
  if (!c?.gsc_available) return null;
  const go = (e: any) => { e.preventDefault(); navigate(`/analysis?geo=${geo}`); };
  return (
    <section className="card">
      <div className="row between"><div><h3 style={{ margin: 0 }}>What this means – content to fix, add or sharpen</h3><p className="small muted" style={{ margin: "2px 0 0" }}>Worked out from Metricon’s own numbers plus every page already on the site{r ? "" : "; the written recommendations appear in the AI analysis once generated"}.</p></div>
        <a className="btn sm" href={`/analysis?geo=${geo}`} onClick={go}>Full analysis →</a></div>
      {r?.summary && <p style={{ margin: "10px 0 0" }}>{r.summary}</p>}
      <div className="grid g2" style={{ marginTop: 12 }}>
        <div><h4 style={{ margin: "0 0 4px" }}>Quick wins – just off page one</h4>
          {c.quick_wins.slice(0, 6).map((q: any) => <div key={q.id} className="small" style={{ padding: "3px 0", borderTop: "1px solid var(--line-soft)" }}>{q.query} <span className="faint">· {nfmt(q.impressions)} impressions · position {q.position}</span></div>)}
          {c.quick_wins.length === 0 && <p className="small muted">None found.</p>}</div>
        <div><h4 style={{ margin: "0 0 4px" }}>Gaps – demand with no page for it</h4>
          {c.content_gaps.filter((g: any) => g.page_count === 0).slice(0, 6).map((g: any) => <div key={g.id} className="small" style={{ padding: "3px 0", borderTop: "1px solid var(--line-soft)" }}><b>{g.theme}</b> in {g.state} <span className="faint">· {nfmt(g.impressions)} impressions · e.g. “{g.top_queries[0]?.query}”</span></div>)}
          {c.content_gaps.filter((g: any) => g.page_count === 0).length === 0 && <p className="small muted">No state-level gaps found.</p>}</div>
        <div><h4 style={{ margin: "0 0 4px" }}>New searches with no page yet</h4>
          {c.new_searches.filter((n: any) => !n.topic_has_page).slice(0, 6).map((n: any) => <div key={n.id} className="small" style={{ padding: "3px 0", borderTop: "1px solid var(--line-soft)" }}>{n.query} <span className="faint">· rising{n.rise_pct != null && n.rise_pct < 5000 ? ` +${nfmt(n.rise_pct)}%` : ""}</span></div>)}
          {c.new_searches.filter((n: any) => !n.topic_has_page).length === 0 && <p className="small muted">None found – or no rising searches stored yet.</p>}</div>
        <div><h4 style={{ margin: "0 0 4px" }}>Ranking well, clicked rarely</h4>
          {c.low_ctr.slice(0, 6).map((q: any) => <div key={q.id} className="small" style={{ padding: "3px 0", borderTop: "1px solid var(--line-soft)" }}>{q.query} <span className="faint">· position {q.position}, CTR {q.ctr_pct}% vs usual {q.benchmark_ctr_pct}%</span></div>)}
          {c.low_ctr.length === 0 && <p className="small muted">None found.</p>}</div>
      </div>
    </section>
  );
}

function Gsc() {
  const d = useFetch(`/api/demand/gsc`);
  if (d.error) return <ErrorBox message={d.error} />;
  if (!d.data) return <Loading />;
  if (!d.data.imported) return <Empty>No Search Console export has been imported yet (Admin → Search data).</Empty>;
  const i = d.data.import;
  const rows = d.data.themes.map((t: any) => ({ ...t, label: t.name }));
  return (
    <div className="stack">
      <Banner tone="info"><strong>Metricon’s own search visibility only.</strong> {d.data.caveat} Export: {i.file_name}, reported period {fmtDate(i.reported_period_start)} – {fmtDate(i.reported_period_end)} (entered at import; the file itself has no date column), imported {fmtDate(i.imported_at)}.</Banner>
      <div className="grid g2">
        <section className="card"><h3>Search queries by theme · impressions</h3>
          <div style={{ height: 400 }}><ResponsiveContainer width="100%" height="100%"><BarChart data={rows} layout="vertical" margin={{ left: 120, right: 20 }}>
            <CartesianGrid stroke="var(--line-soft)" horizontal={false} /><XAxis type="number" tick={{ fontSize: 11, fill: "var(--ink-3)" }} tickFormatter={(v) => (v / 1000).toLocaleString("en-AU") + "k"} />
            <YAxis type="category" dataKey="label" width={118} tick={{ fontSize: 12, fill: "var(--ink-2)" }} tickLine={false} axisLine={false} />
            <Tooltip formatter={(v: any, n: any, p: any) => [`${Number(v).toLocaleString("en-AU")} impressions · ${p.payload.queries.toLocaleString("en-AU")} queries · ${p.payload.clicks.toLocaleString("en-AU")} clicks`, ""]} contentStyle={{ background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 8 }} />
            <Bar dataKey="impressions" fill="var(--s1)" radius={[0, 4, 4, 0]} barSize={14} />
          </BarChart></ResponsiveContainer></div>
          <p className="tiny faint">Queries only. Excludes off-topic searches ({"rentals, decorating, jobs"}) and queries not yet mapped to a theme.</p>
        </section>
        <section className="card"><h3>Metricon pages by type · impressions</h3>
          <div className="tablewrap"><table><thead><tr><th>Page type</th><th className="r">Pages</th><th className="r">Impressions</th><th className="r">Clicks</th></tr></thead><tbody>
            {d.data.pages.by_kind.map((k: any) => <tr key={String(k.kind)}><td>{(k.kind ?? "other").replace(/_/g, " ")}</td><td className="r num">{k.pages.toLocaleString("en-AU")}</td><td className="r num">{k.impressions.toLocaleString("en-AU")}</td><td className="r num">{k.clicks.toLocaleString("en-AU")}</td></tr>)}</tbody></table></div>
          <p className="tiny faint">{d.data.pages.note}</p>
        </section>
      </div>
      <GscInsights />
    </div>
  );
}

function Capabilities() {
  const d = useFetch(`/api/demand/capabilities`);
  if (!d.data) return null;
  return (
    <details className="card small"><summary><b>Provider coverage by geography</b> <span className="muted">(what the Google Trends provider supports, from recorded tests)</span></summary>
      <div className="tablewrap" style={{ marginTop: 8 }}><table><thead><tr><th>Geography</th><th>Level</th><th>Tested</th><th>Supported</th><th>Coverage</th><th>Detail</th></tr></thead><tbody>
        {d.data.map((c: any) => <tr key={c.geo}><td>{c.geo.replace("city:", "")}</td><td>{c.level}</td><td>{c.tested ? fmtDate(c.tested_at) : "Not tested"}</td><td>{c.supported == null ? "–" : c.supported ? "Yes" : <Chip tone="bad">No</Chip>}</td><td>{c.coverage ?? "–"}</td><td className="muted">{c.detail?.reason ?? (c.detail?.points ? `${c.detail.points} weekly points` : "")}</td></tr>)}</tbody></table></div></details>
  );
}

export default function Demand() {
  const { geo, meta, user } = useApp();
  const [tab, setTab] = useState<"brands" | "themes" | "gsc">("brands");
  const [win, setWin] = useState<Win>("365");
  const [yoy, setYoy] = useState(false);
  const trends = meta.modules.trends;
  return (
    <div>
      <div className="page-head">
        <div><h1>Demand trends</h1><p>Google Trends shows <b>relative search interest</b> – an index, not search volume, enquiries, sales or market share. Statewide series are statewide; they are not city series.</p></div>
        <div className="row">
          {tab !== "gsc" && <><label className="field">Window<select value={win} onChange={(e) => setWin(e.target.value as Win)}>{WINS.map((w) => <option key={w.id} value={w.id}>{w.label}</option>)}</select></label>
            {tab === "brands" && <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={yoy} onChange={(e) => setYoy(e.target.checked)} />Overlay a year earlier (first two brands)</label>}
            <button className="btn" onClick={() => download(`/api/demand/export.csv?geo=${geo}&kind=${tab === "themes" ? "themes" : "brands"}`)}>Export CSV</button></>}
        </div>
      </div>
      {!trends.connected && <Banner><strong>Search-interest data is not connected.</strong> {user.role === "admin" ? `${trends.reason}. ` : ""}Anything already stored is shown; nothing new is being collected. The Metricon search visibility tab is unaffected.</Banner>}
      <Tabs value={tab} onChange={setTab} items={[{ id: "brands", label: "Brand comparison" }, { id: "themes", label: "Categories & subthemes" }, { id: "gsc", label: "Metricon search visibility" }]} />
      {tab === "brands" && <Brands win={win} yoy={yoy} />}
      {tab === "themes" && <Themes win={win} />}
      {tab === "gsc" && <Gsc />}
      <div style={{ marginTop: 16 }}><Capabilities /></div>
    </div>
  );
}
