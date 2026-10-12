import { useApp } from "../App";
import { fmtDate, fmtDateTime, pct } from "../api";
import { Banner, Chip, Empty, ErrorBox, Loading, Tile, download, useFetch } from "../ui";

const GEO_NAME: Record<string, string> = { AU: "Australia", VIC: "Victoria", NSW: "New South Wales", QLD: "Queensland", SA: "South Australia" };

export default function MarketBrief() {
  const { geo, meta, navigate } = useApp();
  const brief = useFetch(`/api/brief?scope=${geo}`, [geo]);
  const macro = useFetch(`/api/macro?geo=${geo}`, [geo]);
  const events = useFetch(`/api/events?state=${geo}&days=14`, [geo]);
  const news = useFetch(`/api/news?state=${geo}&limit=6`, [geo]);
  const b = brief.data?.brief;
  const ai = meta.modules.ai;

  const tile = (indicator: string, g?: string) => macro.data?.series.find((s: any) => s.indicator === indicator && (g ? s.geography === g : true));
  const approvals = tile("house_approvals");
  const ppi = tile("house_output_ppi");
  const cash = tile("cash_rate");

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Market brief · {GEO_NAME[geo]}</h1>
          <p>What changed in buyer demand, competitor offers and the wider market, with the evidence behind each point. Facts, interpretation and suggested actions are kept apart.</p>
        </div>
        {b && <button className="btn" onClick={() => download(`/api/brief/export.md?scope=${geo}`)}>Download brief (Markdown)</button>}
      </div>

      {!ai.connected && <Banner><strong>Weekly brief writing is switched off.</strong> {ai.reason}. Tables, charts and exports below still work; no placeholder text is generated.</Banner>}
      {brief.loading && <Loading />}
      {brief.error && <ErrorBox message={brief.error} />}

      {brief.data && !b && <Empty><strong>Brief unavailable</strong><div className="small">No brief has been generated for {GEO_NAME[geo]} yet.{brief.data.latest_attempt?.error ? ` Last attempt: ${brief.data.latest_attempt.error}` : ""}</div></Empty>}

      {b && (
        <div className="stack">
          <div className="row small muted">
            <Chip tone="info">Week commencing {fmtDate(b.week_start)}</Chip>
            <span>Generated {fmtDateTime(b.generated_at)}</span>
            {brief.data.unavailable && <Chip tone="warn">This week’s brief is unavailable – showing the previous one</Chip>}
          </div>
          {b.note && <p className="muted small" style={{ margin: 0 }}>{b.note}</p>}
          {b.highlights.length === 0 && <Empty>No substantive, evidence-backed highlights this week.</Empty>}
          {b.highlights.map((h: any) => (
            <article className="hl" key={h.position}>
              <header>
                <h2>{h.position}. {h.headline}</h2>
                <div className="row small muted" style={{ marginTop: 4 }}>
                  <span>{h.scope.geography}</span>
                  {h.scope.brands?.map((x: string) => <Chip key={x}>{x}</Chip>)}
                  {h.scope.themes?.map((x: string) => <Chip key={x} tone="info">{x}</Chip>)}
                </div>
              </header>
              <div className="sec"><div className="k fact">Observed</div><div>{h.observation}</div></div>
              <div className="sec"><div className="k interp">Interpretation</div><div>{h.interpretation}</div></div>
              <div className="sec"><div className="k act">Marketing</div><div>{h.marketing_implication}</div></div>
              <div className="sec"><div className="k act">Sales</div><div>{h.sales_implication}</div></div>
              {h.evidence_limitation && <div className="sec"><div className="k">Limits</div><div className="muted">{h.evidence_limitation}</div></div>}
              <div className="sec"><div className="k">Evidence</div><div className="small">{h.evidence.map((e: any) => (
                <div key={e.id}>{e.url ? <a href={e.url} target="_blank" rel="noreferrer noopener">{e.label}</a> : e.label} <span className="faint tiny">({e.type.replace(/_/g, " ")})</span></div>))}</div></div>
            </article>
          ))}
          {b.data_gaps?.length > 0 && <details className="small muted"><summary>Data gaps behind this brief</summary><ul>{b.data_gaps.map((g: string) => <li key={g}>{g}</li>)}</ul></details>}
        </div>
      )}

      <h2 style={{ margin: "26px 0 10px" }}>Market snapshot</h2>
      <div className="grid g3">
        <Tile label="Private houses approved" help={approvals?.description} value={approvals?.latest ? Math.round(approvals.latest.value).toLocaleString("en-AU") : "–"}
          sub={approvals?.latest ? <>{approvals.latest.period} · {pct(approvals.comparison?.vs_year_ago?.pct)} vs a year earlier · original data ({approvals.geography})</> : "Not collected yet"} />
        <Tile label="House construction output prices" help={ppi?.description} value={ppi?.latest ? ppi.latest.value.toFixed(1) : "–"}
          sub={ppi?.latest ? <>{ppi.latest.period} · {pct(ppi.comparison?.vs_year_ago?.pct)} vs a year earlier · index points ({ppi.geography})</> : "Not collected yet"} />
        <Tile label="Cash rate target (national)" help={cash?.description} value={cash?.latest ? cash.latest.value.toFixed(2) + "%" : "–"}
          sub={cash?.latest ? <>Set {fmtDate(cash.latest.period)} · previous {cash.comparison?.previous_value?.toFixed(2)}%</> : "Not collected yet"} />
      </div>

      <div className="grid g2" style={{ marginTop: 14 }}>
        <section className="card">
          <div className="row between"><h3>Competitor moves, last 14 days</h3><a href={`/activity?geo=${geo}`} onClick={(e) => { e.preventDefault(); navigate(`/activity?geo=${geo}`); }}>All activity</a></div>
          {events.data?.events.length ? events.data.events.slice(0, 6).map((e: any) => (
            <div key={e.id} style={{ padding: "8px 0", borderTop: "1px solid var(--line-soft)" }}>
              <div><b>{e.brand}</b> · {e.title}</div>
              <div className="tiny faint">First observed {fmtDate(e.observed_at)} · {e.date_note}</div>
            </div>)) : <p className="muted small">No new designs, packages or offer changes observed in this period. {events.data?.tracked_items === 0 && "Competitor design/package pages have not been registered or baselined yet."}</p>}
        </section>
        <section className="card">
          <div className="row between"><h3>In the news</h3><span className="tiny faint">Third-party headlines</span></div>
          {news.data?.length ? news.data.map((n: any) => (
            <div key={n.id} style={{ padding: "8px 0", borderTop: "1px solid var(--line-soft)" }}>
              <a href={n.url} target="_blank" rel="noreferrer noopener">{n.title}</a>
              <div className="tiny faint">{n.publisher} · published {fmtDate(n.published_at)} · {n.label}</div>
            </div>)) : <p className="muted small">No relevant items collected yet.</p>}
        </section>
      </div>
    </div>
  );
}
