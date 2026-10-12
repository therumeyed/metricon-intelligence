import { useState } from "react";
import { useApp } from "../App";
import { fmtDate, qs } from "../api";
import { Banner, Chip, Empty, ErrorBox, Loading, useFetch } from "../ui";

const TYPES: Record<string, string> = {
  new_design: "New design observed", new_display: "New display home observed", new_package: "New package observed", new_estate: "New estate observed", price_change: "Advertised price change",
  new_offer: "New offer observed", campaign_change: "Campaign update", offer_no_longer_observed: "Offer no longer observed",
};

export default function Activity() {
  const { geo, meta } = useApp();
  const [brand, setBrand] = useState("");
  const [type, setType] = useState("");
  const [days, setDays] = useState(60);
  const d = useFetch(`/api/events${qs({ state: geo === "AU" ? undefined : geo, brand: brand ? [brand] : [], event_type: type, days: String(days) })}`, [geo, brand, type, days]);
  const roster = meta.brands.filter((b: any) => geo === "AU" || b.states.includes(geo));
  return (
    <div>
      <div className="page-head"><div><h1>Competitor activity</h1><p>New or changed designs, packages, display homes and campaign messaging on the official pages we monitor. Dates are when our crawler first saw a change – not launch dates – unless the source states one.</p></div>
        <div className="row"><label className="field">Brand<select value={brand} onChange={(e) => setBrand(e.target.value)}><option value="">All monitored brands</option>{roster.map((b: any) => <option key={b.slug} value={b.slug}>{b.name}</option>)}</select></label>
          <label className="field">Type<select value={type} onChange={(e) => setType(e.target.value)}><option value="">All</option>{Object.entries(TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label className="field">Period<select value={days} onChange={(e) => setDays(+e.target.value)}><option value={14}>Last 14 days</option><option value={60}>Last 60 days</option><option value={180}>Last 6 months</option></select></label></div></div>
      {d.loading && !d.data && <Loading />}
      {d.error && <ErrorBox message={d.error} />}
      {d.data && <Banner tone="info">{d.data.tracked_items} design, package and display items are being tracked. {d.data.note} The first crawl of any page is a baseline: it records what exists but does not count as news. Normal package churn and formatting-only changes are not reported.</Banner>}
      {d.data && d.data.events.length === 0 && <Empty>No changes observed for these filters. Quiet periods and newly started monitoring look the same here – check the source list under Admin to see what has been baselined.</Empty>}
      <div className="stack">
        {d.data?.events.map((e: any) => (
          <article className="card" key={e.id}>
            <div className="row between"><div className="row"><Chip tone={e.brand === "Metricon" ? "own" : undefined}>{e.brand}</Chip><Chip tone="info">{TYPES[e.event_type] ?? e.event_type}</Chip>{e.state && <Chip>{e.state}</Chip>}</div><span className="small muted">First observed {fmtDate(e.observed_at)}</span></div>
            <h3 style={{ margin: "8px 0 4px" }}>{e.title}</h3>
            {e.summary && <p className="muted small" style={{ margin: "0 0 6px" }}>{e.summary}</p>}
            {(e.before || e.after) && (
              <div className="tablewrap" style={{ marginTop: 6 }}><table><thead><tr><th>Detail</th><th>Before</th><th>After</th></tr></thead><tbody>
                {[...new Set([...Object.keys(e.before ?? {}), ...Object.keys(e.after ?? {})])].map((k) => (
                  <tr key={k}><td className="small"><code>{k}</code></td><td className="small">{e.before?.[k] != null ? <del>{typeof e.before[k] === "object" ? JSON.stringify(e.before[k]) : String(e.before[k])}</del> : <span className="faint">–</span>}</td><td className="small">{e.after?.[k] != null ? <ins>{typeof e.after[k] === "object" ? JSON.stringify(e.after[k]) : String(e.after[k])}</ins> : <span className="faint">–</span>}</td></tr>))}</tbody></table></div>)}
            <div className="small" style={{ marginTop: 8 }}>{e.date_note}{e.source_url && <> · <a href={e.source_url} target="_blank" rel="noreferrer noopener">Source page</a></>}{e.offer_campaign_id && <span className="faint"> · offer {e.offer_campaign_id}</span>}</div>
          </article>))}
      </div>
    </div>
  );
}
