import { useEffect, useState } from "react";
import { useApp } from "../App";
import { api, fmtDate, fmtDateTime } from "../api";
import { Banner, Chip, Empty, ErrorBox, Help, Loading, Tabs, useFetch } from "../ui";

const GEO_NAME: Record<string, string> = { AU: "Australia", VIC: "Victoria", NSW: "New South Wales", QLD: "Queensland", SA: "South Australia" };
const REC: Record<string, { label: string; tone?: "good" | "warn" | "info" }> = {
  create_page: { label: "New page", tone: "good" }, optimise_page: { label: "Improve a page", tone: "info" }, expand_content: { label: "Add to a page", tone: "info" },
  campaign: { label: "Campaign" }, offer_review: { label: "Review offer" }, monitor: { label: "Keep watching" },
};
const PRIORITY: Record<string, "bad" | "warn" | undefined> = { high: "bad", medium: "warn", low: undefined };
const nf = (n?: number | null) => (n == null ? "–" : Math.round(n).toLocaleString("en-AU"));

function factLabel(f: any): string {
  return f.title || f.query || f.name || f.theme || f.keyword || (f.brand ? `${f.brand} ${f.kind}` : f.kind);
}

function Evidence({ ids, evidence }: { ids: string[]; evidence: Record<string, any> }) {
  if (!ids?.length) return null;
  return <details className="small muted"><summary>Evidence ({ids.length})</summary>
    <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{ids.map((i) => <li key={i}>{evidence[i] ? factLabel(evidence[i]) : i} <span className="faint tiny">({(evidence[i]?.kind ?? "").replace(/_/g, " ")})</span>
      {(evidence[i]?.source_url || evidence[i]?.url) && <> · <a href={evidence[i].source_url || evidence[i].url} target="_blank" rel="noreferrer noopener">source</a></>}</li>)}</ul></details>;
}

function Explain({ title, children }: { title: string; children: React.ReactNode }) {
  return <div style={{ marginBottom: 8 }}><h3 style={{ margin: 0 }}>{title}</h3><p className="small muted" style={{ margin: "2px 0 0" }}>{children}</p></div>;
}

export default function Analysis() {
  const { geo, user } = useApp();
  const [tab, setTab] = useState<"recs" | "new" | "themes" | "tables" | "pages">("recs");
  const d = useFetch(`/api/analysis?scope=${geo}`, [geo]);
  const [msg, setMsg] = useState("");
  const gen = d.data?.generating;
  useEffect(() => { if (!gen) return; const t = setInterval(d.reload, 5000); return () => clearInterval(t); }, [gen]);
  const r = d.data?.report;
  const c = d.data?.computed;
  const ev = r?.evidence ?? {};
  const run = async (name: string, params: any, text: string) => {
    try { await api.post("/api/admin/jobs/run", { name, params }); setMsg(text); setTimeout(d.reload, 1500); } catch (e) { setMsg((e as Error).message); }
  };

  return (
    <div>
      <div className="page-head">
        <div><h1>AI analysis & recommendations · {GEO_NAME[geo]}</h1>
          <p>What people are newly searching for, how Metricon shows up for it, what competitors are doing – and what to do next. Recommendations are checked against every page already on the website, so you won’t be told to build something that exists.</p></div>
        {user.role === "admin" && <div className="row"><button className="btn primary" disabled={gen || !d.data?.ai_connected} onClick={() => run("generate_analysis", { scope: geo }, "Analysis is being written – this takes a minute or two and the page will update itself.")}>{gen ? "Writing…" : r ? "Refresh analysis" : "Write analysis"}</button></div>}
      </div>
      {msg && <Banner tone="info">{msg}</Banner>}
      {d.loading && !d.data && <Loading />}
      {d.error && <ErrorBox message={d.error} />}
      {d.data && !d.data.ai_connected && <Banner><strong>The written analysis is switched off</strong> because the AI model isn’t connected. The data tables below are computed from your own numbers and still work.</Banner>}
      {c && !c.gsc_available && <Banner><strong>No Search Console export has been imported.</strong> Metricon’s own visibility, quick wins and content gaps need it (Admin → Search Console). Rising searches from Google Trends still show.</Banner>}
      {d.data && d.data.ai_connected && !r && <Empty><strong>No analysis written yet</strong><div className="small">{d.data.latest_attempt?.error ? `Last attempt: ${d.data.latest_attempt.error}` : "It’s generated weekly, or press “Write analysis”."}</div></Empty>}

      {r && <div className="row small muted" style={{ marginBottom: 10 }}><Chip tone="info">Week commencing {fmtDate(r.week_start)}</Chip><span>Written {fmtDateTime(r.generated_at)}</span>
        <span>· checked against {nf(r.checked_against_pages)} known pages on metricon.com.au</span>
        {d.data.latest_attempt?.status === "failed" && <Chip tone="warn">Latest refresh failed – showing the previous analysis</Chip>}</div>}
      {r?.summary && <section className="card" style={{ marginBottom: 12 }}><p style={{ margin: 0, fontSize: 15 }}>{r.summary}</p></section>}

      <Tabs value={tab} onChange={setTab} items={[
        { id: "recs", label: `Recommendations${r ? ` (${r.recommendations.length})` : ""}` }, { id: "new", label: "What’s new in the market" }, { id: "themes", label: "Themes" },
        { id: "tables", label: "Your search opportunities" }, { id: "pages", label: "Existing pages" }]} />

      {tab === "recs" && <div className="stack">
        {!r && <Empty>Recommendations appear here once the analysis has been written.</Empty>}
        {r?.recommendations.length === 0 && <Empty>The evidence this week didn’t support a firm recommendation.</Empty>}
        {r?.recommendations.map((x: any, i: number) => (
          <article className="hl" key={i}>
            <header><div className="row"><Chip tone={REC[x.type]?.tone}>{REC[x.type]?.label ?? x.type}</Chip><Chip tone={PRIORITY[x.priority]}>{x.priority} priority</Chip><Chip>{x.effort} effort</Chip>{x.state && <Chip>{x.state}</Chip>}</div>
              <h2 style={{ marginTop: 6 }}>{i + 1}. {x.title}</h2>{x.adjustment && <div className="small muted">{x.adjustment}</div>}</header>
            <div className="sec"><div className="k fact">Why</div><div>{x.why}</div></div>
            <div className="sec"><div className="k act">Do this</div><div>{x.action}</div></div>
            {x.existing_pages?.length > 0 && <div className="sec"><div className="k">Page{x.existing_pages.length > 1 ? "s" : ""} already on the site</div><div className="small">{x.existing_pages.map((p: any) => (
              <div key={p.url}><a href={p.url} target="_blank" rel="noreferrer noopener">{p.path}</a>{p.impressions != null && <span className="faint"> · {nf(p.impressions)} impressions{p.position ? `, avg position ${p.position}` : ""}</span>}</div>))}</div></div>}
            {x.type === "create_page" && <div className="sec"><div className="k">Checked</div><div className="small muted">No existing page matches “{x.topic_terms.join(" ")}” among {nf(r.checked_against_pages)} known URLs.</div></div>}
            <div className="sec"><div className="k">How you’ll know</div><div className="small">{x.success_signal}</div></div>
            {x.caveat && <div className="sec"><div className="k">Limits</div><div className="muted small">{x.caveat}</div></div>}
            <div className="sec"><div className="k">Evidence</div><div><Evidence ids={x.evidence} evidence={ev} /></div></div>
          </article>))}
        {r?.dropped?.length > 0 && <details className="small muted"><summary>{r.dropped.length} suggestion{r.dropped.length > 1 ? "s" : ""} removed by the checks</summary><ul>{r.dropped.map((x: any, i: number) => <li key={i}>{x.title}: {x.reason}</li>)}</ul></details>}
        {r?.data_gaps?.length > 0 && <details className="small muted" open><summary>Data gaps behind this analysis</summary><ul>{r.data_gaps.map((g: string) => <li key={g}>{g}</li>)}</ul></details>}
      </div>}

      {tab === "new" && <div className="stack">
        {r?.new_in_market.length > 0 && <section className="card"><Explain title="New and rising searches">Searches growing on Google (Trends “rising” queries across the home-building themes), what people probably want, and what to do. “Rising” means growing quickly from a base – it can still be small.</Explain>
          {r.new_in_market.map((n: any, i: number) => <div key={i} style={{ padding: "10px 0", borderTop: i ? "1px solid var(--line-soft)" : 0 }}><b>“{n.query}”</b>
            <div className="small" style={{ marginTop: 3 }}><span className="muted">Likely intent: </span>{n.what_people_want}</div>
            <div className="small"><span className="muted">What to do: </span>{n.what_to_do}</div><Evidence ids={n.evidence} evidence={ev} /></div>)}</section>}
        {c && <section className="card"><Explain title="Every rising search we’ve found, against your site">For each one: do you already show up for it (Search Console), and is there already a page on the topic? A rising search with no visibility and no page is the clearest gap; one with a page but weak visibility is a job for that page.</Explain>
          {c.new_searches.length === 0 ? <Empty>No rising searches stored yet. They come from the weekly Google Trends discovery job (needs the DataForSEO connection).</Empty> :
            <div className="tablewrap"><table><thead><tr><th>Search</th><th>Rising</th><th>Found</th><th>Metricon visibility<Help text="From the Search Console export: queries containing all the words, with their impressions and average position" /></th><th>Existing page on the topic</th></tr></thead><tbody>
              {c.new_searches.map((n: any) => <tr key={n.id}><td><b>{n.query}</b><div className="tiny faint">via “{n.seed}”</div></td><td className="num">{n.rise_pct != null ? (n.rise_pct >= 5000 ? "Breakout" : `+${nf(n.rise_pct)}%`) : "–"}</td>
                <td className="small">{n.new_this_period ? <Chip tone="good">New</Chip> : fmtDate(n.first_seen)}</td>
                <td className="small">{typeof n.metricon_visibility === "string" ? <span className="faint">{n.metricon_visibility}</span> : <>{nf(n.metricon_visibility.impressions)} impressions · avg position {n.metricon_visibility.avg_position}</>}</td>
                <td className="small">{n.existing_pages_matching_topic.length ? n.existing_pages_matching_topic.slice(0, 2).map((p: any) => <div key={p.url}><a href={p.url} target="_blank" rel="noreferrer noopener">{p.path}</a></div>) : <Chip tone="warn">No page yet</Chip>}</td></tr>)}</tbody></table></div>}</section>}
      </div>}

      {tab === "themes" && <div className="stack">
        {r?.themes.length > 0 && <section className="card"><Explain title="Theme read">Metricon’s own visibility joined with market search movement, theme by theme.</Explain>
          {r.themes.map((t: any, i: number) => <div key={i} style={{ padding: "8px 0", borderTop: i ? "1px solid var(--line-soft)" : 0 }}><b>{t.theme}</b> – {t.headline}<Evidence ids={t.evidence} evidence={ev} /></div>)}</section>}
        {r?.competitor_moves.length > 0 && <section className="card"><Explain title="What competitors are doing">Only moves backed by an offer, a tracked page change or a news item.</Explain>
          {r.competitor_moves.map((m: any, i: number) => <div key={i} style={{ padding: "8px 0", borderTop: i ? "1px solid var(--line-soft)" : 0 }}><b>{m.brand}</b> – {m.observation}<div className="small muted">{m.implication}</div><Evidence ids={m.evidence} evidence={ev} /></div>)}</section>}
        {c?.theme_scorecard.length > 0 && <section className="card"><Explain title="Theme scorecard (Metricon’s Search Console data)">Positions are impression-weighted. A big “page 2” share means demand you’re close to winning.</Explain>
          <div className="tablewrap"><table><thead><tr><th>Theme</th><th className="r">Impressions</th><th className="r">Clicks</th><th className="r">CTR</th><th className="r">Avg position</th><th className="r">Top 3</th><th className="r">Top 10</th><th className="r">Page 2</th><th className="r">Branded</th><th className="r">Pages on site</th></tr></thead><tbody>
            {c.theme_scorecard.map((t: any) => <tr key={t.theme_slug}><td><b>{t.theme}</b></td><td className="r num">{nf(t.impressions)}</td><td className="r num">{nf(t.clicks)}</td><td className="r num">{t.ctr_pct}%</td><td className="r num">{t.avg_position_weighted}</td>
              <td className="r num">{t.share_impressions_top3_pct}%</td><td className="r num">{t.share_impressions_top10_pct}%</td><td className="r num">{t.share_impressions_page2_pct}%</td><td className="r num">{t.branded_share_pct}%</td><td className="r num">{t.site_pages_in_theme}</td></tr>)}</tbody></table></div></section>}
        {!r && !c?.theme_scorecard.length && <Empty>Theme reads appear once the analysis is written and a Search Console export is imported.</Empty>}
      </div>}

      {tab === "tables" && c && <div className="stack">
        <Banner tone="info">Computed from Metricon’s Search Console export{c.period ? ` (${c.period})` : ""} and the page inventory. The query and page exports can’t be joined, so these point at topics and themes – not at a specific URL that “ranks”.</Banner>
        <section className="card"><Explain title="Quick wins – close to page one">Unbranded searches where Metricon sits at position 4–19. Better titles, a clearer answer near the top and a few internal links often move these.</Explain>
          {c.quick_wins.length === 0 ? <Empty>None found.</Empty> : <div className="tablewrap"><table><thead><tr><th>Search</th><th className="r">Impressions</th><th className="r">Position</th><th className="r">CTR</th><th className="r">Extra clicks if position 3<Help text="Indicative: Metricon’s own CTR at position 3 in this export, applied to this query’s impressions" /></th></tr></thead><tbody>
            {c.quick_wins.map((q: any) => <tr key={q.id}><td>{q.query}{q.state_hint && <span className="tiny faint"> · {q.state_hint}</span>}</td><td className="r num">{nf(q.impressions)}</td><td className="r num">{q.position}</td><td className="r num">{q.ctr_pct}%</td><td className="r num">{nf(q.indicative_extra_clicks_if_position_3)}</td></tr>)}</tbody></table></div>}</section>
        <section className="card"><Explain title="Ranking well, clicked rarely">Top-six positions with click-through well below Metricon’s own norm for that position. Usually a title, description or snippet problem, not a ranking problem.</Explain>
          {c.low_ctr.length === 0 ? <Empty>None found.</Empty> : <div className="tablewrap"><table><thead><tr><th>Search</th><th className="r">Impressions</th><th className="r">Position</th><th className="r">CTR</th><th className="r">Usual CTR here</th></tr></thead><tbody>
            {c.low_ctr.map((q: any) => <tr key={q.id}><td>{q.query}</td><td className="r num">{nf(q.impressions)}</td><td className="r num">{q.position}</td><td className="r num">{q.ctr_pct}%</td><td className="r num">{q.benchmark_ctr_pct}%</td></tr>)}</tbody></table></div>}</section>
        <section className="card"><Explain title="Content gaps – demand in a state with no page for it">Searches that name a state, grouped by theme, where the site has no page for that theme in that state.</Explain>
          {c.content_gaps.length === 0 ? <Empty>None found.</Empty> : <div className="tablewrap"><table><thead><tr><th>Theme</th><th>State</th><th className="r">Impressions</th><th>Top searches</th><th>Pages for it</th></tr></thead><tbody>
            {c.content_gaps.map((g: any) => <tr key={g.id}><td><b>{g.theme}</b></td><td>{g.state}</td><td className="r num">{nf(g.impressions)}</td><td className="small">{g.top_queries.map((q: any) => <div key={q.query}>{q.query} <span className="faint">({nf(q.impressions)}, pos {q.position})</span></div>)}</td>
              <td className="small">{g.page_count === 0 ? <Chip tone="warn">None</Chip> : g.existing_pages_for_theme_and_state.map((p: any) => <div key={p.url}><a href={p.url} target="_blank" rel="noreferrer noopener">{p.url.replace("https://www.metricon.com.au", "")}</a></div>)}</td></tr>)}</tbody></table></div>}</section>
        <section className="card"><Explain title="Big searches where Metricon ranks past page one">Highest-impression unbranded searches beyond position 10, and whether a page on the topic already exists.</Explain>
          {c.topic_visibility.length === 0 ? <Empty>None found.</Empty> : <div className="tablewrap"><table><thead><tr><th>Search</th><th className="r">Impressions</th><th className="r">Position</th><th>Page on the topic</th></tr></thead><tbody>
            {c.topic_visibility.map((q: any) => <tr key={q.id}><td>{q.query}</td><td className="r num">{nf(q.impressions)}</td><td className="r num">{q.position}</td>
              <td className="small">{q.existing_pages_matching_topic.length ? q.existing_pages_matching_topic.slice(0, 2).map((p: any) => <div key={p.url}><a href={p.url} target="_blank" rel="noreferrer noopener">{p.path}</a></div>) : <Chip tone="warn">No page yet</Chip>}</td></tr>)}</tbody></table></div>}</section>
      </div>}

      {tab === "pages" && c && <div className="stack">
        <section className="card"><Explain title="Pages we know exist on metricon.com.au">Recommendations are tested against this list. It combines the sitemap with every page that earned impressions in Search Console.</Explain>
          <div className="row" style={{ gap: 20 }}><div><div className="tiny faint">Known pages</div><b style={{ fontSize: 22 }}>{nf(c.inventory.total_pages)}</b></div><div><div className="tiny faint">From the sitemap</div><b style={{ fontSize: 22 }}>{nf(c.inventory.sitemap_pages)}</b></div></div>
          {c.inventory.sitemap_pages === 0 && <Banner>The sitemap hasn’t been read yet, so only pages that appeared in Search Console are known. {user.role === "admin" && <button className="btn sm" onClick={() => run("refresh_site_inventory", {}, "Reading the sitemap…")}>Read the sitemap now</button>}</Banner>}
          <div className="tablewrap" style={{ marginTop: 10 }}><table><thead><tr><th>Kind of page</th><th className="r">Pages</th></tr></thead><tbody>{Object.entries(c.inventory.by_kind).sort((a: any, b: any) => b[1] - a[1]).map(([k, n]: any) => <tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="r num">{nf(n)}</td></tr>)}</tbody></table></div></section>
      </div>}
      {c?.gaps?.length > 0 && <details className="small muted" style={{ marginTop: 12 }}><summary>Limits of this data</summary><ul>{c.gaps.map((g: string) => <li key={g}>{g}</li>)}</ul></details>}
    </div>
  );
}
