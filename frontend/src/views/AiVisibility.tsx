import { useState } from "react";
import { fmtDate, fmtDateTime, qs } from "../api";
import { Banner, Chip, Empty, ErrorBox, Help, Loading, Panel, Tabs, Tile, useFetch } from "../ui";

const nf = (n?: number | null) => (n == null ? "–" : Math.round(n).toLocaleString("en-AU"));
const pc = (v?: number | null) => (v == null ? "–" : `${Math.round(v * 100)}%`);
const TAG: Record<string, string> = { "G.J. Gardner": "GJ", Carlisle: "CAR", Simonds: "SIM", "Porter Davis": "PD" };
const BRANDS: Record<string, string> = { "G.J. Gardner": "G.J. Gardner", Carlisle: "Carlisle", Simonds: "Simonds", "Porter Davis": "Porter Davis" };
const OUT_ORDER = ["won", "named_not_cited", "cited_not_named", "rival_only", "nobody", "other"];
const OUT_TEXT: Record<string, string> = {
  won: "Metricon named and its site cited", named_not_cited: "Metricon named, site not cited", cited_not_named: "Site cited, Metricon not named",
  rival_only: "A rival is named, Metricon isn't", nobody: "No builder named (open ground)", other: "Other",
};
const TIER: Record<string, string> = { core: "Core", secondary: "Decision support", awareness: "Awareness" };
const BRAND_TYPES = [{ id: "non_branded", label: "Non-branded searches" }, { id: "competitor_branded", label: "Competitor-branded" }, { id: "metricon_branded", label: "Metricon-branded" }, { id: "all", label: "Everything" }];
const bin = (v: number) => (v < 0.15 ? "h0" : v < 0.3 ? "h1" : v < 0.45 ? "h2" : v < 0.6 ? "h3" : "h4");

type Drill = { topic?: string; region?: string; city?: string; outcome?: string; title: string };

function Segmented<T extends string>({ value, onChange, items, label }: { value: T; onChange: (v: T) => void; items: { id: T; label: string }[]; label: string }) {
  return <div className="row small" role="group" aria-label={label}>{items.map((i) => <button key={i.id} className={`btn sm ${value === i.id ? "primary" : ""}`} aria-pressed={value === i.id} onClick={() => onChange(i.id)}>{i.label}</button>)}</div>;
}

function OutcomeBar({ o, n }: { o: Record<string, number>; n: number }) {
  return (
    <div className="stack100" role="img" aria-label={OUT_ORDER.map((k) => `${OUT_TEXT[k]} ${pc((o[k] ?? 0) / n)}`).join(", ")}>
      {OUT_ORDER.filter((k) => (o[k] ?? 0) > 0).map((k) => {
        const w = ((o[k] ?? 0) / n) * 100;
        return <span key={k} className={`o-${k}`} style={{ width: `${w}%` }} title={`${OUT_TEXT[k]}: ${nf(o[k])} answers (${pc((o[k] ?? 0) / n)})`}>{w >= 9 ? `${Math.round(w)}%` : ""}</span>;
      })}
    </div>
  );
}

function OutcomeLegend() {
  return <div className="legendrow">{OUT_ORDER.filter((k) => k !== "other").map((k) => <span key={k}><i className={`sw o-${k}`} />{OUT_TEXT[k]}</span>)}</div>;
}

function DrillPanel({ d, filters, onClose }: { d: Drill; filters: { brand_type: string; platform: string }; onClose: () => void }) {
  const k = useFetch(`/api/ai-visibility/keywords${qs({ topic: d.topic, region: d.region, city: d.city, outcome: d.outcome, ...filters })}`, [JSON.stringify(d), filters.brand_type, filters.platform]);
  return (
    <Panel onClose={onClose} title={<><h2 style={{ margin: 0 }}>{d.title}</h2><div className="small muted">{d.outcome ? `Searches where: ${OUT_TEXT[d.outcome]}` : "All searches in this slice"} · ranked by opportunity score</div></>}>
      {k.loading && !k.data && <Loading />}
      {k.error && <ErrorBox message={k.error} />}
      {k.data && k.data.length === 0 && <Empty>No searches match.</Empty>}
      {k.data?.length > 0 && <div className="tablewrap"><table><thead><tr><th>Search</th><th className="r">Monthly searches<Help text="Ahrefs volume, capped at 5,000 so one outlier can't dominate" /></th><th className="r">Metricon named</th><th className="r">Site cited</th><th>Rivals named</th><th className="r">Score</th></tr></thead><tbody>
        {k.data.map((x: any) => <tr key={x.keyword}><td><b>{x.keyword}</b><div className="tiny faint">{x.journey} · {x.region}</div></td><td className="r num">{nf(x.volume)}</td><td className="r num">{x.named_on} of {x.platforms}</td><td className="r num">{x.cited_on} of {x.platforms}</td>
          <td className="small">{x.rivals.length ? x.rivals.join(", ") : <span className="faint">none</span>}</td><td className="r num">{x.score}</td></tr>)}</tbody></table></div>}
      <p className="tiny faint">“{`named on 2 of 3`}” means that many of the AI platforms tracked for this search named Metricon in their answer.</p>
    </Panel>
  );
}

function Heatmap({ ov, onPick }: { ov: any; onPick: (d: Drill) => void }) {
  const [metric, setMetric] = useState<"mention" | "cited">("mention");
  const [small, setSmall] = useState(false);
  const [by, setBy] = useState<"state" | "city">("state");
  const city = by === "city";
  const cols: string[] = city ? ov.city_columns.map((c: any) => c.city) : ov.columns;
  const grid = city ? ov.city_grid : ov.grid;
  const floor = city ? 30 : 60;
  const big = grid.filter((g: any) => g.all.n >= floor);
  const tiny = grid.filter((g: any) => g.all.n < floor);
  const pick = (topic: string, place: string, label: string) => onPick({ topic, ...(place === "all" ? {} : city ? { city: place } : { region: place }), title: `${topic} · ${place === "all" ? (city ? "all named cities" : "all places") : label}` });
  const cell = (c: any, topic: string, region: string, label: string) => {
    if (!c || !c.n) return <div className="hc none" title="No answers in this slice">–</div>;
    const v = c[metric];
    if (c.low_sample) return <div className="hc none low" title={`${topic} · ${label}: only ${c.n} answers – too few to read (Metricon named in ${pc(c.mention)})`}>n={c.n}</div>;
    const rivalAhead = c.winner !== "Metricon" && c.winner !== "Tie" && c.winner !== "No tracked builder" && !c.low_sample;
    return (
      <div className={`hc ${bin(v)} ${c.low_sample ? "low" : ""}`} tabIndex={0} role="button" aria-label={`${topic}, ${label}: ${pc(v)} ${metric === "mention" ? "named" : "cited"}, ${c.n} answers${rivalAhead ? `, ${c.winner} ahead` : ""}`}
        title={`${topic} · ${label}\nMetricon named in ${pc(c.mention)} of ${c.n} answers, site cited in ${pc(c.cited)}\n${c.low_sample ? `Fewer than ${ov.min_rows} answers: directional only` : c.winner === "Metricon" ? `Metricon leads by ${c.lead_pts} pts` : `${c.winner} leads${c.lead_pts ? ` by ${c.lead_pts} pts` : ""}`}`}
        onClick={() => pick(topic, region, label)} onKeyDown={(e) => e.key === "Enter" && pick(topic, region, label)}>
        {pc(v)}{rivalAhead ? <span className="rival">{TAG[c.winner] ?? c.winner} ahead</span> : null}
      </div>
    );
  };
  return (
    <section className="card">
      <div className="row between"><div><h3 style={{ margin: 0 }}>Where Metricon shows up – and where it doesn’t</h3>
        <p className="small muted" style={{ margin: "2px 0 0" }}>Each square is a topic in a {city ? "city" : "state"}. The number is how often AI answers {metric === "mention" ? "name Metricon" : "cite metricon.com.au"}; an orange tag means a rival is named more often. Click any square to see the searches behind it.</p></div>
        <div className="stack" style={{ gap: 6, alignItems: "flex-end" }}>
          <Segmented label="Place" value={by} onChange={setBy} items={[{ id: "state", label: "By state" }, { id: "city", label: "By city" }]} />
          <Segmented label="Measure" value={metric} onChange={setMetric} items={[{ id: "mention", label: "Metricon named" }, { id: "cited", label: "Website cited" }]} /></div></div>
      <div className="tablewrap" style={{ border: 0, marginTop: 8 }}><table className="heat"><thead><tr><th className="rowh">Topic</th>{cols.map((c) => <th key={c}>{c}{c === "No state named" && <Help text="National or generic searches with no place in them, such as “house designs”" />}{city && <div className="tiny faint" style={{ fontWeight: 500 }}>{ov.city_columns.find((x: any) => x.city === c)?.state}</div>}</th>)}<th>{city ? "All named cities" : "All places"}</th></tr></thead>
        <tbody>{(small ? [...big, ...tiny] : big).map((g: any) => (
          <tr key={g.topic}><td className="rowh">{g.topic}<div className="tiny faint" style={{ fontWeight: 500 }}>{TIER[g.tier]} · {nf(g.all.n)} answers</div></td>
            {cols.map((c) => <td key={c}>{cell(g.cells[c], g.topic, c, c)}</td>)}<td>{cell(g.all, g.topic, "all", city ? "all named cities" : "all places")}</td></tr>))}</tbody></table></div>
      {city && <p className="tiny faint" style={{ margin: "6px 0 0" }}>{ov.city_note}</p>}
      {tiny.length > 0 && <button className="btn sm" onClick={() => setSmall(!small)}>{small ? "Hide" : "Show"} {tiny.length} small topics (under {floor} answers: {tiny.map((t: any) => t.topic).join(", ")})</button>}
      <div className="legendrow"><span>Share of answers:</span>{[["h0", "under 15%"], ["h1", "15–30%"], ["h2", "30–45%"], ["h3", "45–60%"], ["h4", "60%+"]].map(([c, t]) => <span key={c}><i className={`sw hc ${c}`} style={{ display: "inline-block", minHeight: 14 }} />{t}</span>)}
        <span><i className="sw" style={{ background: "var(--s2)" }} />rival named more often</span><span><i className="sw" style={{ border: "1px dashed var(--ink-3)" }} />under {ov.min_rows} answers – too few to read</span></div>
      {!city && Object.keys(ov.outside_markets).length > 0 && <p className="tiny faint" style={{ margin: "6px 0 0" }}>Not shown: {Object.entries(ov.outside_markets).map(([k, v]) => `${k} (${v} answers)`).join(", ")} – outside Metricon’s states.</p>}
    </section>
  );
}

function Lift({ ov, onPick }: { ov: any; onPick: (d: Drill) => void }) {
  const [more, setMore] = useState(false);
  const max = Math.max(...ov.pockets.map((p: any) => p.score), 1);
  const row = (p: any, i: number) => (
    <div key={p.topic + p.region} className="rankbar" role="button" tabIndex={0} onClick={() => onPick({ topic: p.topic, region: p.region, outcome: p.dominant_gap, title: `${p.topic} · ${p.region}` })}>
      <div><b>{i + 1}. {p.topic}</b><div className="small muted">{p.region} · Metricon named in {pc(p.mention)} · {p.winner === "Metricon" ? "Metricon leads" : p.winner === "Tie" ? "no clear leader" : `${p.winner} leads`}</div></div>
      <div className="track" title={`Opportunity score ${nf(p.score)}`}><div className="fill" style={{ width: `${(p.score / max) * 100}%` }} /></div>
      <div className="small"><b>{p.dominant_gap_label}</b> <span className="muted">({nf(p.gap_rows)} of {nf(p.n)} answers)</span><div className="tiny faint">e.g. {p.examples.map((e: any) => e.keyword).join(" · ")}</div></div>
    </div>
  );
  return (
    <section className="card">
      <h3 style={{ margin: 0 }}>Where the biggest lift is</h3>
      <p className="small muted" style={{ margin: "2px 0 8px" }}>Topic-and-place pockets ranked by opportunity score (search demand × how much of the answer Metricon is missing × how close the topic is to a sale). Bar length = score. Click one for the searches to go after.</p>
      {ov.pockets.map(row)}
      {ov.awareness_pockets?.length > 0 && <div style={{ marginTop: 8 }}><button className="btn sm" onClick={() => setMore(!more)}>{more ? "Hide" : "Show"} awareness-stage topics ({ov.awareness_pockets.length})</button>
        {more && <div style={{ marginTop: 6 }}><p className="tiny faint">Top-of-funnel topics (interiors, facades, energy…): big audiences, further from a sale, so ranked separately.</p>{ov.awareness_pockets.map(row)}</div>}</div>}
    </section>
  );
}

function Outcomes({ ov, onPick }: { ov: any; onPick: (d: Drill) => void }) {
  const [by, setBy] = useState<"topic" | "state" | "city">("topic");
  const rows: { key: string; label: string; sub: string; o: Record<string, number>; n: number; drill: Drill }[] =
    by === "topic" ? ov.grid.map((g: any) => ({ key: g.topic, label: g.topic, sub: `${nf(g.all.n)} answers`, o: g.all.outcomes, n: g.all.n, drill: { topic: g.topic, title: g.topic } }))
    : by === "state" ? ov.by_state.map((s: any) => ({ key: s.region, label: s.region, sub: `${nf(s.n)} answers`, o: s.outcomes, n: s.n, drill: { region: s.region, title: s.region } }))
    : ov.by_city.map((c: any) => ({ key: c.city, label: c.city, sub: c.n ? `${c.state} · ${nf(c.n)} answers${c.low_sample ? " · few" : ""}` : `${c.state} · no answers`, o: c.outcomes ?? {}, n: c.n, drill: { city: c.city, title: c.city } }));
  return (
    <section className="card">
      <div className="row between"><h3 style={{ margin: 0 }}>What AI does when it answers</h3><Segmented label="Group by" value={by} onChange={setBy} items={[{ id: "topic", label: "By topic" }, { id: "state", label: "By state" }, { id: "city", label: "By city" }]} /></div>
      <p className="small muted" style={{ margin: "2px 0 8px" }}>For each {by}, the share of answers in each outcome. Blue is good: Metricon is named. Orange is the sharpest gap: a rival is named and Metricon isn’t. Grey is open ground nobody owns yet.</p>
      <OutcomeLegend />
      <div style={{ marginTop: 10 }}>{rows.map((r) => (
        <div key={r.key} style={{ display: "grid", gridTemplateColumns: "minmax(170px, 1fr) minmax(220px, 3fr)", gap: 12, alignItems: "center", padding: "5px 0", cursor: r.n ? "pointer" : "default" }} onClick={() => r.n && onPick(r.drill)}>
          <div className="small"><b>{r.label}</b><div className="tiny faint">{r.sub}</div></div>{r.n ? <OutcomeBar o={r.o} n={r.n} /> : <div className="tiny faint">No searches name this city in the data</div>}</div>))}</div>
    </section>
  );
}

function Priorities({ items, onMore }: { items: any[]; onMore: () => void }) {
  const p1 = items.filter((i) => i.Priority === "P1").slice(0, 5);
  if (!p1.length) return null;
  return (
    <section className="card">
      <div className="row between"><h3 style={{ margin: 0 }}>Top priorities</h3><button className="btn sm" onClick={onMore}>All recommended next steps →</button></div>
      {p1.map((i, n) => <div key={n} style={{ padding: "8px 0", borderTop: n ? "1px solid var(--line-soft)" : 0 }}><b>{i.Theme}</b> <span className="muted small">· {i.Segment}</span><div className="small">{i["Recommended next step (for decision)"]}</div></div>)}
    </section>
  );
}

function NextSteps({ items }: { items: any[] }) {
  if (!items.length) return <Empty>No recommended next steps in this workbook.</Empty>;
  const tone = (p: string) => (p === "P1" ? "bad" : p === "P2" ? "warn" : undefined);
  return (
    <div className="stack">
      <Banner tone="info">These are the agency analyst’s options for decision, as written in the workbook – they are not generated by this dashboard. Figures in the text come from the workbook; the charts on the Overview are recalculated from the answers.</Banner>
      {items.map((i, n) => (
        <article className="hl" key={n}>
          <header><div className="row"><Chip tone={tone(i.Priority) as any}>{i.Priority}</Chip><Chip>{i.Theme}</Chip><Chip>{i.Segment}</Chip>{i.Impact && <Chip>Impact: {i.Impact}</Chip>}{i.Effort && <Chip>Effort: {i.Effort}</Chip>}</div></header>
          <div className="sec"><div className="k fact">Finding</div><div>{i["Finding (verify figures)"]}</div></div>
          <div className="sec"><div className="k act">Next step</div><div>{i["Recommended next step (for decision)"]}</div></div>
          {i["KPI to track"] && <div className="sec"><div className="k">Track</div><div className="small">{i["KPI to track"]}</div></div>}
          {i["Current winner"] && <div className="sec"><div className="k">Winning today</div><div className="small">{i["Current winner"]}</div></div>}
        </article>))}
    </div>
  );
}

const MEASURES = [
  { id: "first", label: "Named first", help: "Share of all answers in which this builder is the FIRST tracked builder the AI names" },
  { id: "named", label: "Named at all", help: "Share of answers that name this builder anywhere" },
  { id: "first_when_named", label: "First, when named", help: "When the AI names this builder, how often it is listed first. Shows pull, independent of how often it comes up" },
  { id: "rec_first", label: "First in who-to-choose questions", help: "Only searches phrased as a recommendation request (e.g. 'best home builders in Melbourne'). Needs at least 10 such answers" },
] as const;
type MeasureId = (typeof MEASURES)[number]["id"];

function Strength({ ov }: { ov: any }) {
  const st = ov.strength;
  const [by, setBy] = useState<"topic" | "state" | "city">("topic");
  const [measure, setMeasure] = useState<MeasureId>("first");
  const [small, setSmall] = useState(false);
  if (!st?.available) return <Empty><strong>Needs a fresh upload</strong><div className="small">{st?.note}</div></Empty>;
  const t = st[by];
  const floor = by === "topic" ? 60 : 0;
  const keep = t.columns.filter((c: any) => c.n >= floor);
  const tiny = t.columns.filter((c: any) => c.n < floor);
  const cols: any[] = small ? [...keep, ...tiny] : keep;
  const val = (c: any) => (c && c.n ? c[measure] : null);
  const rank = (col: string) => {
    const vs = t.brands.map((b: any) => ({ brand: b.brand, v: val(b.cells[col]) })).filter((x: any) => x.v != null);
    vs.sort((a: any, b: any) => b.v - a.v);
    const i = vs.findIndex((x: any) => x.brand === "Metricon");
    return i < 0 || vs[i].v === 0 ? null : 1 + vs.filter((x: any) => x.v > vs[i].v).length;
  };
  const cell = (c: any, brand: string, col: string) => {
    if (!c || !c.n) return <div className="hc none">–</div>;
    if (c.low_sample) return <div className="hc none low" title={`${brand} · ${col}: only ${c.n} answers`}>n={c.n}</div>;
    const v = c[measure];
    if (v == null) return <div className="hc none" title="Too few answers of this kind to read">n/a</div>;
    const leads = measure === "first" && t.leaders[col] === brand;
    return <div className={`hc ${bin(measure === "first" ? v * 1.5 : v)}`} style={leads ? { boxShadow: "inset 0 0 0 2px var(--ink)" } : undefined}
      title={`${brand} · ${col}\nNamed in ${pc(c.named)} of ${c.n} answers; first in ${pc(c.first)}${c.first_when_named != null ? `; first in ${pc(c.first_when_named)} of the answers that name it` : ""}${c.rec_first != null ? `\nFirst in ${pc(c.rec_first)} of ${c.rec_n} who-to-choose answers` : ""}`}>{pc(v)}</div>;
  };
  const m = MEASURES.find((x) => x.id === measure)!;
  return (
    <div className="stack">
      <Banner tone="info"><strong>How strongly AI puts each builder forward – prominence, not sentiment.</strong> {st.note}</Banner>
      <section className="card">
        <div className="row between" style={{ alignItems: "flex-start" }}>
          <div><h3 style={{ margin: 0 }}>Overall scoreboard</h3><p className="small muted" style={{ margin: "2px 0 0" }}>All tracked builders across every answer in the current view.</p></div></div>
        <div className="tablewrap" style={{ marginTop: 8 }}><table><thead><tr><th>Builder</th><th className="r">Named at all</th><th className="r">Named first</th><th className="r">First, when named<Help text={MEASURES[2].help} /></th><th className="r">First in who-to-choose questions<Help text={MEASURES[3].help} /></th></tr></thead><tbody>
          {t.brands.map((b: any) => <tr key={b.brand} className={b.brand === "Metricon" ? "own" : ""}><td><b>{b.brand}</b></td><td className="r num">{pc(b.all.named)}</td><td className="r num"><b>{pc(b.all.first)}</b></td><td className="r num">{pc(b.all.first_when_named)}</td><td className="r num">{b.all.rec_first != null ? pc(b.all.rec_first) : <span className="faint">n/a</span>}</td></tr>)}</tbody></table></div>
      </section>
      <section className="card">
        <div className="row between" style={{ alignItems: "flex-start" }}>
          <div><h3 style={{ margin: 0 }}>Each builder against each {by === "topic" ? "category" : by}</h3><p className="small muted" style={{ margin: "2px 0 0" }}>{m.help}. A black outline marks the builder AI names first most often in that column.</p></div>
          <div className="stack" style={{ gap: 6, alignItems: "flex-end" }}>
            <Segmented label="Group by" value={by} onChange={setBy} items={[{ id: "topic", label: "By category" }, { id: "state", label: "By state" }, { id: "city", label: "By city" }]} />
            <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: 6 }}><span className="small muted">Measure</span><select value={measure} onChange={(e) => setMeasure(e.target.value as MeasureId)}>{MEASURES.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}</select></label></div></div>
        <div className="tablewrap" style={{ border: 0, marginTop: 8 }}><table className="heat"><thead><tr><th className="rowh">Builder</th>{cols.map((c: any) => <th key={c.key}>{c.key}<div className="tiny faint" style={{ fontWeight: 500 }}>{nf(c.n)} answers</div></th>)}<th>All</th></tr></thead>
          <tbody>{t.brands.map((b: any) => (
            <tr key={b.brand}><td className="rowh" style={b.brand === "Metricon" ? { color: "var(--s1)" } : undefined}>{b.brand}</td>{cols.map((c: any) => <td key={c.key}>{cell(b.cells[c.key], b.brand, c.key)}</td>)}<td>{cell(b.all, b.brand, "All")}</td></tr>))}
            <tr><td className="rowh small muted">Metricon’s rank</td>{cols.map((c: any) => { const r = rank(c.key); return <td key={c.key} className="small muted" style={{ textAlign: "center" }}>{r ? `#${r} of 5` : "–"}</td>; })}<td /></tr></tbody></table></div>
        {tiny.length > 0 && <button className="btn sm" onClick={() => setSmall(!small)}>{small ? "Hide" : "Show"} {tiny.length} small categories (under {floor} answers)</button>}
        <div className="legendrow"><span>Share:</span>{[["h0", "low"], ["h1", ""], ["h2", ""], ["h3", ""], ["h4", "high"]].map(([c, t2]) => <span key={c}><i className={`sw hc ${c}`} style={{ display: "inline-block", minHeight: 14 }} />{t2}</span>)}<span><i className="sw" style={{ border: "1px dashed var(--ink-3)" }} />under {ov.min_rows} answers</span><span><i className="sw" style={{ boxShadow: "inset 0 0 0 2px var(--ink)" }} />leads the column</span></div>
        <p className="tiny faint" style={{ margin: "6px 0 0" }}>Porter Davis is tracked but rarely appears in AI answers. Builders AI names that aren’t tracked (Coral Homes, Hickinbotham, Clarendon…) are on the Platforms, sources & rivals tab.{by === "city" ? ` ${ov.city_note}` : ""}</p>
      </section>
    </div>
  );
}

function shortAction(a: string) {
  if (a.startsWith("Named but not linked")) return "Strengthen the landing page AI already names";
  if (a.startsWith("Build/upgrade")) return "Build or upgrade a page that answers this; earn third-party citations";
  if (a.startsWith("Publish an authoritative")) return "Publish an answer page (FAQ or guide) – nobody owns this answer";
  return a;
}

function Opportunities({ rows, onPick }: { rows: any[]; onPick: (d: Drill) => void }) {
  const [type, setType] = useState("Non-branded");
  const [topic, setTopic] = useState("");
  const [region, setRegion] = useState("");
  const topics = [...new Set(rows.map((r) => r["Product / topic"]))].sort();
  const regions = [...new Set(rows.map((r) => r["Region"]))].sort();
  const view = rows.filter((r) => (!type || r["Brand type"] === type) && (!topic || r["Product / topic"] === topic) && (!region || r["Region"] === region));
  return (
    <div className="stack">
      <section className="card"><div className="row" style={{ alignItems: "flex-end" }}>
        <label className="field">Who’s searching<select value={type} onChange={(e) => setType(e.target.value)}><option value="Non-branded">Non-branded (no builder named)</option><option value="Competitor branded">Competitor-branded (conquest)</option><option value="">Both</option></select></label>
        <label className="field">Topic<select value={topic} onChange={(e) => setTopic(e.target.value)}><option value="">All</option>{topics.map((t) => <option key={t}>{t}</option>)}</select></label>
        <label className="field">Region<select value={region} onChange={(e) => setRegion(e.target.value)}><option value="">All</option>{regions.map((t) => <option key={t}>{t}</option>)}</select></label>
        <span className="small muted">{view.length} of {rows.length} searches · the workbook’s top 200 by opportunity score</span></div></section>
      <div className="tablewrap"><table><thead><tr><th className="r">#</th><th>Search</th><th>Topic · place</th><th>Stage</th><th className="r">Monthly searches</th><th className="r">Score<Help text="0–100: demand × gap type × journey stage × topic relevance, using the weights in the workbook's Assumptions sheet" /></th><th>Why it's a gap</th><th>Rivals named</th><th>What to do</th></tr></thead><tbody>
        {view.map((r) => <tr key={r["Rank"]} className="click" onClick={() => onPick({ topic: r["Product / topic"], title: r["Keyword"] })}><td className="r num">{r["Rank"]}</td><td><b>{r["Keyword"]}</b></td><td className="small">{r["Product / topic"]}<div className="tiny faint">{r["Region"]}</div></td><td className="small">{r["Journey stage"]}</td>
          <td className="r num">{nf(r["Search volume"])}</td><td className="r num"><b>{r["Opportunity score"]}</b></td><td className="small">{String(r["Dominant gap type"]).split(" – ")[0]}</td>
          <td className="small">{r["Competitors named"] ?? <span className="faint">none</span>}</td><td className="small" style={{ minWidth: 240 }}>{shortAction(String(r["Suggested action"] ?? ""))}{/\((\w+) localised page\)/.test(String(r["Suggested action"])) && <Chip tone="info">{String(r["Suggested action"]).match(/\((\w+) localised page\)/)![1]} page</Chip>}</td></tr>)}</tbody></table></div>
    </div>
  );
}

function MiniBars({ rows }: { rows: { label: string; v: number; me?: boolean; note?: string }[] }) {
  return <div>{rows.map((r) => <div key={r.label} className={`minibar ${r.me ? "me" : ""}`}><span>{r.label}</span><div className="t"><div className="f" style={{ width: `${Math.min(100, r.v * 100)}%` }} /></div><span className="num r">{pc(r.v)}</span></div>)}</div>;
}

function Sources({ ov, domains, untracked }: { ov: any; domains: any; untracked: any }) {
  const [by, setBy] = useState<"state" | "city">("state");
  const places: { key: string; label: string; n: number; low?: boolean; winner?: string; rates?: any }[] = by === "state"
    ? ov.by_state.map((s: any) => ({ key: s.region, label: s.region, n: s.n, low: s.low_sample, winner: s.winner, rates: s.rates }))
    : ov.by_city.map((c: any) => ({ key: c.city, label: `${c.city} (${c.state})`, n: c.n, low: c.low_sample, winner: c.winner, rates: c.rates }));
  const dr = (domains.rows ?? []).slice(0, 14);
  const maxd = Math.max(...dr.map((d: any) => d["% of all answers"] ?? 0), 0.01);
  return (
    <div className="stack">
      <div className="grid g2">
        <section className="card"><h3 style={{ margin: 0 }}>By AI platform</h3><p className="small muted" style={{ margin: "2px 0 8px" }}>How often each platform names Metricon (blue) and cites its website (grey).</p>
          {ov.platforms.map((p: any) => (
            <div key={p.platform} style={{ padding: "6px 0", borderTop: "1px solid var(--line-soft)" }}><div className="row between small"><b>{p.platform}</b><span className="muted">{nf(p.n)} answers{p.low_sample ? " · directional" : ""}</span></div>
              <MiniBars rows={[{ label: "Named", v: p.mention, me: true }, { label: "Site cited", v: p.cited }]} /></div>))}</section>
        <section className="card"><div className="row between"><h3 style={{ margin: 0 }}>Who AI names, by {by}</h3><Segmented label="Place" value={by} onChange={setBy} items={[{ id: "state", label: "State" }, { id: "city", label: "City" }]} /></div>
          <p className="small muted" style={{ margin: "2px 0 8px" }}>Share of answers naming each tracked builder. Metricon in blue.{by === "city" ? ` ${ov.city_note}` : ""}</p>
          {places.map((s) => (
            <div key={s.key} style={{ padding: "6px 0", borderTop: "1px solid var(--line-soft)" }}><div className="row between small"><b>{s.label}</b><span className="muted">{s.n ? `${nf(s.n)} answers${s.low ? " · few" : s.winner === "Metricon" ? " · Metricon ahead" : s.winner && s.winner !== "Tie" && s.winner !== "No tracked builder" ? ` · ${s.winner} ahead` : ""}` : "no answers"}</span></div>
              {s.n ? <MiniBars rows={[{ label: "Metricon", v: s.rates["Metricon"], me: true }, ...Object.keys(BRANDS).filter((b) => s.rates[b] > 0.005).map((b) => ({ label: BRANDS[b], v: s.rates[b] }))]} /> : <div className="tiny faint">No searches name this city in the data.</div>}</div>))}</section>
      </div>
      <section className="card"><h3 style={{ margin: 0 }}>Which websites AI cites</h3><p className="small muted" style={{ margin: "2px 0 8px" }}>{domains.caption} The column on the right is where Metricon is <i>not</i> named – the sources shaping those answers, and so the places to earn a mention.</p>
        <div className="tablewrap"><table><thead><tr><th>Website</th><th>Type</th><th style={{ minWidth: 200 }}>Cited in % of all answers</th><th className="r">Where Metricon is absent</th></tr></thead><tbody>
          {dr.map((d: any) => <tr key={d["Domain"]} style={d["Domain type"] === "Metricon (owned)" ? { fontWeight: 700 } : undefined}><td>{d["Domain"]}</td><td className="small">{d["Domain type"]}</td>
            <td><div className="minibar me" style={{ gridTemplateColumns: "1fr 44px" }}><div className="t"><div className="f" style={{ width: `${((d["% of all answers"] ?? 0) / maxd) * 100}%`, background: d["Domain type"] === "Metricon (owned)" ? "var(--s1)" : "var(--ink-3)" }} /></div><span className="num r">{pc(d["% of all answers"])}</span></div></td>
            <td className="r num">{pc(d["% of Metricon-absent answers"])}</td></tr>)}</tbody></table></div></section>
      {untracked.rows?.length > 0 && <section className="card"><h3 style={{ margin: 0 }}>Builders AI names that aren’t being tracked</h3><p className="small muted" style={{ margin: "2px 0 8px" }}>{untracked.caption} Darker = named more often.</p>
        <div className="tablewrap"><table className="heat"><thead><tr><th className="rowh">Builder</th><th>All</th><th>VIC</th><th>QLD</th><th>NSW</th><th>SA</th></tr></thead><tbody>
          {untracked.rows.map((u: any) => <tr key={u["Builder"]}><td className="rowh">{u["Builder"]}</td>{["% answers – All", "% answers – VIC", "% answers – QLD", "% answers – NSW", "% answers – SA"].map((k) => { const v = u[k] ?? 0; return <td key={k}><div className={`hc ${bin(v * 2)}`} style={{ minHeight: 30 }}>{pc(v)}</div></td>; })}</tr>)}</tbody></table></div></section>}
    </div>
  );
}

function Reputation({ n }: { n: any }) {
  const rows = n.rows ?? [];
  const [pri, setPri] = useState("");
  const view = rows.filter((r: any) => !pri || r["Priority"] === pri).slice(0, 80);
  const tone = (p: string) => (p === "High" ? "bad" : p === "Medium" ? "warn" : p === "Own the answer" ? "info" : undefined);
  return (
    <div className="stack">
      <Banner tone="info">{n.caption}</Banner>
      <div className="row"><label className="field">Priority<select value={pri} onChange={(e) => setPri(e.target.value)}><option value="">All ({rows.length})</option>{["High", "Medium", "Own the answer", "Monitor"].map((p) => <option key={p} value={p}>{p} ({rows.filter((r: any) => r["Priority"] === p).length})</option>)}</select></label></div>
      <div className="tablewrap"><table><thead><tr><th>What people ask AI about Metricon</th><th className="r">Monthly searches</th><th>Stage</th><th className="r">Answers with negative themes<Help text="Share of AI answers containing 2+ negative terms. A keyword screen only – read the answers before acting." /></th><th className="r">Site cited</th><th>Priority</th></tr></thead><tbody>
        {view.map((r: any) => <tr key={r["Keyword"]}><td><b>{r["Keyword"]}</b><div className="tiny faint">{r["AI platforms"]}</div></td><td className="r num">{nf(r["Search volume"])}</td><td className="small">{r["Journey stage"]}</td>
          <td className="r num">{pc(r["Negative-theme share of answers"])}</td><td className="r num">{pc(r["Metricon.com.au cited (share of answers)"])}</td><td><Chip tone={tone(r["Priority"]) as any}>{r["Priority"]}</Chip></td></tr>)}</tbody></table></div>
    </div>
  );
}

export default function AiVisibility() {
  const [tab, setTab] = useState<"overview" | "strength" | "next" | "opps" | "sources" | "rep">("overview");
  const [brand, setBrand] = useState("non_branded");
  const [platform, setPlatform] = useState("");
  const [drill, setDrill] = useState<Drill | null>(null);
  const d = useFetch(`/api/ai-visibility${qs({ brand_type: brand, platform })}`, [brand, platform]);
  if (d.error) return <ErrorBox message={d.error} />;
  if (!d.data) return <Loading />;
  if (!d.data.imported) return <div><div className="page-head"><div><h1>AI search visibility</h1></div></div><Empty><strong>No AI visibility data yet</strong><div className="small">Upload the segmented Ahrefs Brand Radar workbook under Admin → Search data → “AI search visibility”.</div></Empty></div>;
  const im = d.data.import;
  const ov = d.data.overview;
  const failed = (im.checks ?? []).filter((c: any) => !c.ok);
  const filters = { brand_type: brand, platform };
  const k = ov.kpi;
  return (
    <div>
      <div className="page-head"><div><h1>AI search visibility</h1><p>When people ask Google AI, ChatGPT, Gemini, Copilot or Perplexity about building a home, does the answer name Metricon and send them to metricon.com.au – and where do rivals get the mention instead?</p></div>
        <div className="row"><label className="field">Who’s asking<select value={brand} onChange={(e) => setBrand(e.target.value)}>{BRAND_TYPES.map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}</select></label>
          <label className="field">AI platform<select value={platform} onChange={(e) => setPlatform(e.target.value)}><option value="">All platforms</option>{d.data.platform_options.map((p: string) => <option key={p}>{p}</option>)}</select></label></div></div>
      <Banner tone="info"><strong>Draft data.</strong> {im.source_note ? im.source_note : "Source: Ahrefs Brand Radar export."} Answers {fmtDate(im.period_start)} – {fmtDate(im.period_end)} · {nf(im.rows)} answers · imported {fmtDateTime(im.imported_at)}.{failed.length > 0 && <> <b>Cross-check warning:</b> {failed.map((c: any) => `${c.check} (${c.detail})`).join("; ")}.</>}</Banner>
      <Tabs value={tab} onChange={setTab} items={[{ id: "overview", label: "Overview" }, { id: "strength", label: "Builder comparison" }, { id: "next", label: "Recommended next steps" }, { id: "opps", label: "Search opportunities" }, { id: "sources", label: "Platforms, sources & rivals" }, { id: "rep", label: "Reputation" }]} />
      {ov.empty && tab === "overview" && <Empty>No answers match these filters.</Empty>}
      {tab === "overview" && !ov.empty && <div className="stack">
        <div className="grid g4">
          <Tile label="Metricon is named in" value={pc(k.mention)} sub={`of ${nf(k.answers)} AI answers${k.rates["G.J. Gardner"] ? ` · G.J. Gardner ${pc(k.rates["G.J. Gardner"])}` : ""}`} />
          <Tile label="metricon.com.au is cited in" value={pc(k.cited)} sub={`${nf(k.outcomes.named_not_cited)} answers name Metricon but don’t link the site`} />
          <Tile label="A rival is named, Metricon isn’t" value={pc(k.outcomes.rival_only / k.answers)} sub={`${nf(k.outcomes.rival_only)} answers – the clearest gap`} />
          <Tile label="Open ground: no builder named" value={pc(k.outcomes.nobody / k.answers)} sub={`${nf(k.outcomes.nobody)} answers nobody owns yet`} />
        </div>
        <Heatmap ov={ov} onPick={setDrill} />
        <Lift ov={ov} onPick={setDrill} />
        <Outcomes ov={ov} onPick={setDrill} />
        <Priorities items={d.data.insights} onMore={() => setTab("next")} />
      </div>}
      {tab === "strength" && !ov.empty && <Strength ov={ov} />}
      {tab === "next" && <NextSteps items={d.data.insights} />}
      {tab === "opps" && <Opportunities rows={d.data.opportunities} onPick={setDrill} />}
      {tab === "sources" && !ov.empty && <Sources ov={ov} domains={d.data.domains} untracked={d.data.untracked} />}
      {tab === "rep" && <Reputation n={d.data.narrative} />}
      <details className="small muted" style={{ marginTop: 14 }}><summary>How these numbers are worked out</summary>
        <ul><li>Every figure on Overview and Platforms is recalculated from the {nf(im.rows)} answer rows (one row = one search on one AI platform). Off-topic rows are excluded.</li>
          <li>“Named” = Metricon appears in the answer text. “Cited” = a metricon.com.au link is used as a source.</li>
          <li>A square or pocket with fewer than {im.min_rows} answers is shown faded and never called a win or a loss.</li>
          {(im.checks ?? []).map((c: any) => <li key={c.check}>{c.ok ? "✓" : "⚠"} {c.check}: {c.detail}</li>)}</ul></details>
      {drill && <DrillPanel d={drill} filters={filters} onClose={() => setDrill(null)} />}
    </div>
  );
}
