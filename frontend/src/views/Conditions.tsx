import { useState } from "react";
import { useApp } from "../App";
import { fmtDate, pct } from "../api";
import { Banner, Chip, ErrorBox, Help, LineSeriesChart, Loading, Tabs, useFetch } from "../ui";

const ORDER = ["house_approvals", "house_commencements", "house_output_ppi", "cash_rate", "mortgage_rate"];
const TITLES: Record<string, string> = {
  house_approvals: "Private-sector house approvals", house_commencements: "Private-sector houses commenced", house_output_ppi: "House construction output price index",
  cash_rate: "Cash rate target", mortgage_rate: "Home loan rates (owner-occupier)",
};
const RATE = new Set(["cash_rate", "mortgage_rate"]);
const GEOS = ["AU", "VIC", "NSW", "QLD", "SA"];
const STANCE: Record<string, { label: string; tone?: "good" | "warn" | "bad" | "info" }> = {
  supportive: { label: "Supportive", tone: "good" }, headwind: { label: "Headwind", tone: "bad" }, weaker: { label: "Weaker", tone: "bad" },
  neutral: { label: "Neutral", tone: "info" }, flat: { label: "Flat", tone: "info" }, unknown: { label: "Not enough data" },
};
const TOPICS = [{ id: "all", label: "All" }, { id: "rates", label: "Rates & lending" }, { id: "policy", label: "Policy & grants" }, { id: "costs", label: "Costs & supply" }, { id: "activity", label: "Building activity" }, { id: "other", label: "Other" }] as const;

function Read({ geo }: { geo: string }) {
  const d = useFetch(`/api/macro/conditions?geo=${geo}`, [geo]);
  if (d.error) return <ErrorBox message={d.error} />;
  if (!d.data) return <Loading />;
  return (
    <section className="card">
      <h2 style={{ margin: 0 }}>What the numbers say, and what to do with them</h2>
      <p style={{ margin: "6px 0 10px", fontSize: 15 }}>{d.data.summary}</p>
      <div className="tablewrap"><table><thead><tr><th>Indicator</th><th>Read</th><th>Evidence</th><th>What to do with it</th></tr></thead><tbody>
        {d.data.items.map((i: any) => (
          <tr key={i.indicator}><td style={{ minWidth: 150 }}><b>{TITLES[i.indicator]}</b><div className="tiny faint">{i.scope === "national series" ? "National series" : i.geography}</div></td>
            <td><Chip tone={STANCE[i.stance]?.tone}>{STANCE[i.stance]?.label}</Chip> <Help text={i.rule ?? ""} /></td>
            <td className="small" style={{ minWidth: 220 }}>{i.basis}</td><td className="small" style={{ minWidth: 280 }}>{i.use ?? <span className="faint">–</span>}</td></tr>))}
      </tbody></table></div>
      <p className="tiny faint" style={{ margin: "8px 0 0" }}>{d.data.note} Hover the “?” for the rule behind each read.</p>
    </section>
  );
}

function Compare({ geo }: { geo: string }) {
  const d = useFetch(`/api/macro/compare`, []);
  if (!d.data) return null;
  return (
    <section className="card">
      <h2 style={{ margin: 0 }}>Which state is firmer?</h2>
      <p className="small muted" style={{ margin: "2px 0 8px" }}>The same measure for each state, side by side. Use it to decide where to lean in and where to be careful. State series are published by the ABS; nothing here is filled in from the national figure.</p>
      <div className="tablewrap"><table><thead><tr><th>Measure</th>{GEOS.map((g) => <th key={g} className="r" style={{ fontWeight: g === geo ? 800 : 500 }}>{g === "AU" ? "Australia" : g}</th>)}</tr></thead><tbody>
        {d.data.rows.map((r: any) => (
          <tr key={r.indicator}><td>{r.label}</td>{GEOS.map((g) => { const c = r.cells[g]; return (
            <td key={g} className="r num" style={{ background: g === geo ? "var(--surface-2)" : undefined }}>{c ? <><Chip tone={STANCE[c.stance]?.tone}>{pct(c.pct)}</Chip><div className="tiny faint">{c.period}</div></> : <span className="faint">–</span>}</td>); })}</tr>))}
      </tbody></table></div>
    </section>
  );
}

function Indicator({ ind, series, geo }: { ind: string; series: any[]; geo: string }) {
  const s = series.find((x: any) => x.geography === geo) ?? series[0];
  if (!s) return <section className="card"><h2>{TITLES[ind]}</h2><p className="muted">No data for {geo} – this indicator is not published for the selected geography.</p></section>;
  const national = s.geography === "AU" && geo !== "AU";
  const lag = s.release?.publication_lag_days;
  const rate = RATE.has(ind);
  const fmt = (v: number) => (rate ? v.toFixed(2) : v.toLocaleString("en-AU", { maximumFractionDigits: 1 }));
  const lines = ind === "mortgage_rate" ? series : [s];
  const cmp = s.comparison;
  return (
    <section className="card">
      <div className="row between">
        <h2>{ind === "mortgage_rate" ? TITLES[ind] : s.name} <Help text={s.description} /></h2>
        <div className="row small muted"><Chip tone={national ? "warn" : "info"}>{national ? "National series – shared by every state" : s.geography === "AU" ? "National data" : "State data"}</Chip><Chip>{s.frequency}</Chip><Chip>{s.adjustment}</Chip></div>
      </div>
      <div className="row" style={{ margin: "8px 0 12px", gap: 28 }}>
        <div><div className="tiny faint">Latest ({s.latest?.period})</div><div style={{ fontSize: 26, fontWeight: 700 }} className="num">{s.latest ? (rate ? s.latest.value.toFixed(2) + "%" : fmt(s.latest.value)) : "–"}</div><div className="tiny faint">{s.unit}</div></div>
        {ind === "cash_rate" ? <div><div className="tiny faint">Previous rate</div><b className="num">{cmp?.previous_value?.toFixed(2)}% (set {fmtDate(cmp?.previous_change_date)})</b></div>
          : rate ? <div><div className="tiny faint">vs a year earlier</div><b className="num">{cmp?.vs_year_ago?.diff != null ? `${cmp.vs_year_ago.diff > 0 ? "+" : ""}${cmp.vs_year_ago.diff.toFixed(2)} pts` : "–"}</b></div>
          : <><div><div className="tiny faint">vs previous {s.frequency === "monthly" ? "month" : "quarter"}</div><b className="num">{pct(cmp?.vs_previous_period?.pct)}</b></div>
              <div><div className="tiny faint">vs same period last year</div><b className="num">{pct(cmp?.vs_year_ago?.pct)}</b></div></>}
        <div><div className="tiny faint">Published</div><b>{fmtDate(s.release?.publication_date)}</b>{lag != null && <div className="tiny faint">{lag} days after the period ended</div>}</div>
        {s.next_release && <div><div className="tiny faint">Next release</div><b>{s.next_release}</b></div>}
      </div>
      <LineSeriesChart height={220} series={lines.map((x: any) => ({ key: x.code, label: ind === "mortgage_rate" ? x.name.replace("Home loan rate - ", "") : x.name, points: x.points.map((p: any) => ({ date: p.period_end ?? p.period, value: p.value })) }))}
        valueFmt={(v) => (rate ? v.toFixed(2) : v.toLocaleString("en-AU", { maximumFractionDigits: 1 }))} yLabel={s.unit} periodLabel="Period ending" step={ind === "cash_rate"} />
      <div className="tiny faint" style={{ marginTop: 6 }}>Source: <a href={s.source_url} target="_blank" rel="noreferrer noopener">{s.source}</a> · series <code>{s.dataset}:{s.series_id ?? s.series_key}</code> · retrieved {fmtDate(s.last_retrieved)}</div>
    </section>
  );
}

function Calendar() {
  const d = useFetch(`/api/macro/calendar`, []);
  if (!d.data?.length) return null;
  return (
    <section className="card">
      <h2 style={{ margin: 0 }}>When the next numbers land</h2>
      <p className="small muted" style={{ margin: "2px 0 8px" }}>Worth a diary note: a fresh release is the trigger to re-read the table at the top.</p>
      <div className="tablewrap"><table><thead><tr><th>Release</th><th>Latest period</th><th>Last published</th><th>Next release</th></tr></thead><tbody>
        {d.data.map((r: any) => <tr key={r.source + r.dataset}><td><a href={r.url} target="_blank" rel="noreferrer noopener">{r.name}</a></td><td>{r.latest_period ?? "–"}</td><td>{fmtDate(r.published)}</td><td>{r.next_release ? <b>{fmtDate(r.next_release)}</b> : <span className="faint">{r.source === "RBA" ? "Set by RBA meeting dates" : "Not published"}</span>}</td></tr>)}</tbody></table></div>
    </section>
  );
}

function News({ geo }: { geo: string }) {
  const [topic, setTopic] = useState<(typeof TOPICS)[number]["id"]>("all");
  const news = useFetch(`/api/news?state=${geo}&limit=60${topic === "all" ? "" : `&topic=${topic}`}`, [geo, topic]);
  return (
    <section className="card">
      <div className="row between"><h2 style={{ margin: 0 }}>Policy, industry and market reporting</h2><span className="tiny faint">Headlines from a news aggregator unless marked “Article”. Original sources linked.</span></div>
      <div style={{ margin: "8px 0" }}><Tabs value={topic} onChange={setTopic} items={TOPICS.map((t) => ({ id: t.id, label: t.label }))} /></div>
      {news.data?.length ? <div className="tablewrap"><table><thead><tr><th>Headline</th><th>Topic</th><th>Publisher</th><th>Published</th></tr></thead><tbody>
        {news.data.slice(0, 25).map((n: any) => <tr key={n.id}><td><a href={n.url} target="_blank" rel="noreferrer noopener">{n.title}</a>{n.also_reported_by?.length > 0 && <div className="tiny faint">Also: {n.also_reported_by.join(", ")}</div>}</td>
          <td className="small">{n.topic_label}</td><td>{n.publisher}<div className="tiny faint">{n.source_class.replace("_", " ")} · {n.label}</div></td><td className="num">{fmtDate(n.published_at)}</td></tr>)}</tbody></table></div>
        : <p className="muted">{news.loading ? "Loading…" : "No relevant items collected for this view yet."}</p>}
    </section>
  );
}

export default function Conditions() {
  const { geo } = useApp();
  const macro = useFetch(`/api/macro?geo=${geo}`, [geo]);
  if (macro.error) return <ErrorBox message={macro.error} />;
  if (!macro.data) return <Loading />;
  const groups = ORDER.map((ind) => ({ ind, series: (macro.data?.series ?? []).filter((s: any) => (ind === "mortgage_rate" ? s.indicator === "mortgage_rate" : s.indicator === ind)) }));
  return (
    <div>
      <div className="page-head"><div><h1>Market conditions</h1><p>Official statistics on building activity, builder prices and borrowing costs – read plainly, with what each one means for how you sell. Where a state series is not published it is shown as missing, never filled from the national figure.</p></div></div>
      <div className="stack">
        <Read geo={geo} />
        <Compare geo={geo} />
        {groups.map(({ ind, series }) => <Indicator key={ind} ind={ind} series={series} geo={geo} />)}
        <Calendar />
        <News geo={geo} />
      </div>
      {geo !== "AU" && <Banner tone="info">The cash rate and home loan rates are single national figures. Approvals, commencements and the output price index are state series where the ABS publishes them.</Banner>}
    </div>
  );
}
