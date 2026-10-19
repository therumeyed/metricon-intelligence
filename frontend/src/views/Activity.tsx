import { useState } from "react";
import { useApp } from "../App";
import { fmtDate, qs } from "../api";
import { Banner, Chip, Empty, ErrorBox, Loading, useFetch } from "../ui";

const TYPES: Record<string, string> = {
  new_design: "New design observed", new_display: "New display home observed", new_package: "New package observed", new_estate: "New estate observed", price_change: "Advertised price change",
  new_offer: "New offer observed", campaign_change: "Campaign update", offer_no_longer_observed: "Offer no longer observed",
  news_mention: "In the news", news_campaign: "Campaign or promotion in the news", news_product: "New product, estate or display in the news", news_corporate: "Company news",
  news_award: "Award or recognition", news_community: "Community or sponsorship", news_issue: "Complaint, legal or regulatory issue", news_other: "Other news",
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
      <div className="page-head"><div><h1>Competitor activity</h1><p>What each builder is doing: press and Google News coverage (dated by the publisher), plus changes we spot on official pages (dated by when we first saw them – not launch dates – unless the source states one).</p></div>
        <div className="row"><label className="field">Brand<select value={brand} onChange={(e) => setBrand(e.target.value)}><option value="">All monitored brands</option>{roster.map((b: any) => <option key={b.slug} value={b.slug}>{b.name}</option>)}</select></label>
          <label className="field">Type<select value={type} onChange={(e) => setType(e.target.value)}><option value="">All</option>{Object.entries(TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label className="field">Period<select value={days} onChange={(e) => setDays(+e.target.value)}><option value={14}>Last 14 days</option><option value={60}>Last 60 days</option><option value={180}>Last 6 months</option></select></label></div></div>
      {d.loading && !d.data && <Loading />}
      {d.error && <ErrorBox message={d.error} />}
      {d.data && <Banner tone="info">News comes from Google News results for each brand (headline and snippet only – open the article for detail). {d.data.tracked_items} design, package and display items are also tracked on official pages; the first crawl of any page is a baseline and not counted as news.</Banner>}
      {d.data && d.data.events.length === 0 && <Empty>No changes observed for these filters. Quiet periods and newly started monitoring look the same here – check the source list under Admin to see what has been baselined.</Empty>}
      <div className="stack">
        {d.data?.events.map((e: any) => (
          <article className="card" key={e.id}>
            <div className="row between"><div className="row"><Chip tone={e.brand === "Metricon" ? "own" : undefined}>{e.brand}</Chip><Chip tone="info">{TYPES[e.event_type] ?? e.event_type}</Chip>{e.state && <Chip>{e.state}</Chip>}</div><span className="small muted">{e.published_at ? `Published ${fmtDate(e.published_at)}` : `First observed ${fmtDate(e.observed_at)}`}</span></div>
            <h3 style={{ margin: "8px 0 4px" }}>{e.title}</h3>
            {e.summary && <p className="muted small" style={{ margin: "0 0 6px" }}>{e.summary}</p>}
            {e.publisher && <p className="tiny faint" style={{ margin: "0 0 6px" }}>{e.publisher} · {e.note}</p>}
            {(e.before || e.after) && (
              <div className="tablewrap" style={{ marginTop: 6 }}><table><thead><tr><th>Detail</th><th>Before</th><th>After</th></tr></thead><tbody>
                {[...new Set([...Object.keys(e.before ?? {}), ...Object.keys(e.after ?? {})])].map((k) => (
                  <tr key={k}><td className="small"><code>{k}</code></td><td className="small">{e.before?.[k] != null ? <del>{typeof e.before[k] === "object" ? JSON.stringify(e.before[k]) : String(e.before[k])}</del> : <span className="faint">–</span>}</td><td className="small">{e.after?.[k] != null ? <ins>{typeof e.after[k] === "object" ? JSON.stringify(e.after[k]) : String(e.after[k])}</ins> : <span className="faint">–</span>}</td></tr>))}</tbody></table></div>)}
            <div className="small" style={{ marginTop: 8 }}>{e.date_note}{e.source_url && <> · <a href={e.source_url} target="_blank" rel="noreferrer noopener">{e.origin === "search_news" ? "Read the article" : "Source page"}</a></>}{e.offer_campaign_id && <span className="faint"> · offer {e.offer_campaign_id}</span>}</div>
          </article>))}
      </div>
    </div>
  );
}
