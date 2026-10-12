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

function Themes({ win }: { win: Win }) {
  const { geo } = useApp();
  const d = useFetch(`/api/demand/themes?geo=${geo}`, [geo]);
  const rel = useFetch(`/api/demand/related`, []);
  const [open, setOpen] = useState<string | null>(null);
  if (d.error) return <ErrorBox message={d.error} />;
  if (!d.data) return <Loading />;
  const themes = d.data.themes;
  return (
    <div className="stack">
      <Banner tone="info"><strong>{d.data.geo_label}.</strong> Each theme tracks a few representative search terms, shown on one scale within the theme. They are never averaged into a “market demand” total – a rise in one term is not a rise in the theme.</Banner>
      {themes.every((t: any) => t.status !== "ok") && <Empty><strong>No theme search-interest data stored for {geo} yet.</strong><div className="small">Theme groups are refreshed weekly once the Trends provider is connected and the group is switched on in Admin.</div></Empty>}
      <div className="grid g2">
        {themes.filter((t: any) => t.status === "ok").map((t: any) => (
          <section className="card" key={t.theme}>
            <div className="row between"><h3>{t.theme_name}</h3><button className="btn sm" onClick={() => setOpen(open === t.theme ? null : t.theme)}>{open === t.theme ? "Hide detail" : "Keyword detail"}</button></div>
            <LineSeriesChart height={200} yDomain={[0, 100]} series={t.series.map((s: any, i: number) => ({ key: s.keyword, label: s.keyword, color: SERIES[i % 8], points: cut(s.points, +win).map((p: any) => ({ date: p.date, value: p.value, partial: p.partial })) }))} />
            {open === t.theme && <div className="tablewrap" style={{ marginTop: 10 }}><table><thead><tr><th>Keyword</th><th className="r">4 wks vs previous 4</th><th className="r">vs a year earlier</th></tr></thead>
              <tbody>{t.series.map((s: any) => <tr key={s.keyword}><td>{s.keyword}</td><td className="r">{changeLabel(s.mom4)}</td><td className="r">{changeLabel(s.yoy4)}</td></tr>)}</tbody></table></div>}
          </section>))}
      </div>
      <section className="card">
        <h3>Proposed subthemes from related and rising queries</h3>
        <p className="muted small">Filtered for home-building relevance and waiting for review. A proposal is not an established trend until its interest over time has been checked.</p>
        {rel.data?.length ? <div className="tablewrap"><table><thead><tr><th>Query</th><th>Seed</th><th>Type</th><th>Suggested theme</th><th>Status</th></tr></thead><tbody>
          {rel.data.slice(0, 20).map((r: any) => <tr key={r.id}><td>{r.query}</td><td className="muted">{r.seed}</td><td>{r.kind}{r.value != null ? ` (${r.value})` : ""}</td><td>{r.theme ?? "–"}</td><td><Chip tone={r.validated_trend ? "good" : "warn"}>{r.label}</Chip></td></tr>)}</tbody></table></div> : <p className="muted small">None yet.</p>}
      </section>
    </div>
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
