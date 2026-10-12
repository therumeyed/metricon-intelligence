import { useMemo, useState } from "react";
import { useApp } from "../App";
import { api, fmtDate, fmtDateTime, money, qs } from "../api";
import { Banner, Chip, Empty, ErrorBox, Help, Loading, Panel, Tabs, download, useFetch } from "../ui";

const BENEFITS: Record<string, string> = {
  cash_discount: "Cash discount", upgrade_inclusion_package: "Upgrades / inclusions", deposit_assistance: "Deposit assistance", payment_arrangement: "Payment arrangement",
  price_hold: "Price hold / lock", site_costs: "Site costs", facade_benefit: "Facade benefit", build_time_promise: "Build-time promise", rebate: "Rebate",
  government_grant: "Government grant", finance_incentive: "Finance incentive", other: "Other",
};
const STATUS: Record<string, string> = { advertised: "Advertised", expired: "Expired", withdrawn: "Withdrawn (stated)", no_longer_observed: "No longer observed", needs_review: "Needs review" };
const KIND_NOTE: Record<string, string> = { cash_saving: "cash saving", inclusion_value: "claimed inclusion value – not a saving", payment_arrangement: "payment arrangement – not a discount", government_grant: "government grant – not builder funded" };

function statusChip(o: any) {
  const tone = o.status === "advertised" ? "good" : o.status === "needs_review" ? "warn" : "bad";
  return <Chip tone={tone as any}>{o.status_label}</Chip>;
}
function fresh(o: any) {
  return o.freshness === "fresh" ? <Chip tone="good">Fresh</Chip> : <Chip tone="warn" >Stale</Chip>;
}
function valueCell(o: any) {
  return <div>{o.mechanisms.length === 0 ? <span className="faint">Value not published</span> : o.mechanisms.map((m: any, i: number) => (
    <div key={i} className="num">
      {m.amount != null ? <><b>{m.qualifier === "up_to" ? "Up to " : m.qualifier === "from" ? "From " : ""}{money(m.amount)}</b> <span className="tiny faint">{KIND_NOTE[m.kind] ?? ""}</span></>
        : m.time_promise_days ? <b>{m.time_promise_days} days <span className="tiny faint">{m.time_promise_type === "start_of_construction" ? "to construction start" : m.time_promise_type === "completion" ? "to completion" : "(timing unclear)"}</span></b>
        : <span className="faint">Value not published</span>}
      {m.customer_payment_required != null && <div className="tiny">Customer pays {money(m.customer_payment_required)}</div>}
    </div>))}</div>;
}

export default function Offers() {
  const { geo, meta, user } = useApp();
  const [tab, setTab] = useState<"table" | "patterns">("table");
  const [brands, setBrands] = useState<string[]>([]);
  const [benefit, setBenefit] = useState<string[]>([]);
  const [status, setStatus] = useState<string>("current");
  const [range, setRange] = useState("");
  const [buyer, setBuyer] = useState("");
  const [hist, setHist] = useState(false);
  const [asOf, setAsOf] = useState(meta.today);
  const [open, setOpen] = useState<string | null>(null);

  const roster = meta.brands.filter((b: any) => geo === "AU" || b.states.includes(geo));
  const params = { state: geo === "AU" ? undefined : geo, brand: brands, benefit, status: status === "current" || status === "all" ? undefined : [status], current_only: status === "current" && !hist, range, buyer, as_of: hist ? asOf : undefined };
  const q = qs(params as any);
  const pat = useFetch(tab === "patterns" ? `/api/offers/patterns${qs({ state: geo === "AU" ? undefined : geo })}` : null, [geo, tab]);
  // status "all" must disable the current_only default
  const url = status === "all" ? `/api/offers${qs({ ...params, current_only: false } as any)}` : `/api/offers${q}`;
  const data = useFetch(tab === "table" ? url : null, [url, tab]);

  const rows = data.data?.offers ?? [];
  const sorted = useMemo(() => [...rows].sort((a: any, b: any) => Number(b.is_own) - Number(a.is_own) || a.brand.localeCompare(b.brand)), [rows]);
  const toggle = (arr: string[], v: string, set: (x: string[]) => void) => set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  return (
    <div>
      <div className="page-head">
        <div><h1>Offers in market</h1><p>What Metricon and the monitored builders are advertising, the conditions behind it, and how it has changed. Values are advertised claims taken from official pages – not confirmed prices or sales.</p></div>
        <div className="row"><button className="btn" onClick={() => download(`/api/offers/export.csv${qs({ ...params, current_only: status === "all" ? false : params.current_only } as any)}`)}>Export CSV (this view)</button></div>
      </div>
      <Tabs value={tab} onChange={setTab} items={[{ id: "table", label: "Offer comparison" }, { id: "patterns", label: "Promotional patterns" }]} />

      {tab === "table" && <>
        <section className="card" style={{ marginBottom: 12 }}>
          <div className="row" style={{ alignItems: "flex-end", gap: 16 }}>
            <div className="field"><span>View</span><div className="row small"><label><input type="radio" checked={!hist} onChange={() => setHist(false)} /> Current offers</label><label><input type="radio" checked={hist} onChange={() => setHist(true)} /> Historical: as of</label>
              <input type="date" value={asOf} max={meta.today} disabled={!hist} onChange={(e) => setAsOf(e.target.value)} /></div></div>
            <label className="field">Status<select value={status} onChange={(e) => setStatus(e.target.value)} disabled={hist}><option value="current">Advertised + needs review</option><option value="all">All statuses</option>{Object.entries(STATUS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
            <label className="field">Benefit type<select value={benefit[0] ?? ""} onChange={(e) => setBenefit(e.target.value ? [e.target.value] : [])}><option value="">All</option>{Object.entries(BENEFITS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
            <label className="field">Range or design<input type="text" value={range} onChange={(e) => setRange(e.target.value)} placeholder="e.g. Affinity" /></label>
            <label className="field">Buyer type<input type="text" value={buyer} onChange={(e) => setBuyer(e.target.value)} placeholder="e.g. first home" /></label>
          </div>
          <div className="row" style={{ marginTop: 10 }}><span className="small muted">Brands:</span>{roster.map((b: any) => (
            <button key={b.slug} className={`chip ${brands.includes(b.slug) ? "info" : ""}`} style={{ border: 0, cursor: "pointer" }} onClick={() => toggle(brands, b.slug, setBrands)} aria-pressed={brands.includes(b.slug)}>{b.name}{b.is_own ? " ★" : ""}</button>))}
            {brands.length > 0 && <button className="btn sm" onClick={() => setBrands([])}>Clear</button>}</div>
        </section>

        {hist ? <Banner tone="info"><strong>Historical view – offers as they stood on {fmtDate(asOf)}.</strong> Includes offers first observed by then and not past their published expiry. Dates are when we first saw an offer, so earlier campaigns may be missing. This is not the current picture.</Banner>
          : <Banner tone="info"><strong>Current view</strong> – {geo === "AU" ? "all monitored states" : `offers listed for ${geo}`}. Geography is shown exactly as stated; an offer without a stated region is flagged, never assumed national.</Banner>}
        {data.data?.own_blocked?.length > 0 && <Banner><strong>Metricon’s own offers aren’t being collected automatically yet.</strong> Its website blocks automated access. Metricon rows will appear once the site allows this tool through, or an administrator adds the page content (Admin → Sources).</Banner>}
        {data.loading && !data.data && <Loading />}
        {data.error && <ErrorBox message={data.error} />}
        {data.data && sorted.length === 0 && <Empty>No offers match these filters.</Empty>}
        {sorted.length > 0 && <div className="tablewrap"><table>
          <thead><tr><th>Brand</th><th>Region</th><th>Offer</th><th>Benefit type</th><th>Advertised value / customer cost</th><th>Range</th><th>Key conditions</th><th>Expiry</th><th>Status</th><th>Last checked</th><th>Source</th></tr></thead>
          <tbody>{sorted.map((o: any) => (
            <tr key={o.campaign_id} className={`click ${o.is_own ? "own" : ""}`} onClick={() => setOpen(o.campaign_id)}>
              <td><b>{o.brand}</b></td>
              <td className="small" style={{ maxWidth: 170 }}>{o.geography_unknown ? <span className="faint">Geography not stated</span> : o.geography.map((g: any) => <div key={g.name + g.state}>{g.name ?? g.state}{g.basis === "page_context" && <span className="tiny faint"> (page)</span>}</div>)}</td>
              <td style={{ maxWidth: 260 }}><b>{o.title}</b>{o.needs_review && <div><Chip tone="warn">Needs review</Chip></div>}{o.conflicts.length > 0 && <div><Chip tone="bad">Sources conflict</Chip></div>}</td>
              <td>{[...new Set(o.mechanisms.map((m: any) => m.type_label))].map((t: any) => <div key={t}>{t}</div>)}</td>
              <td>{valueCell(o)}</td>
              <td className="small">{o.ranges.length ? o.ranges.join(", ") : <span className="faint">Not stated</span>}</td>
              <td className="small" style={{ maxWidth: 240 }}>{o.key_conditions}</td>
              <td className="num small">{o.published_expiry ? <>{fmtDate(o.published_expiry)}{o.expiry_timezone_assumed && <div className="tiny faint">tz assumed</div>}</> : <span className="faint">Expiry not published</span>}</td>
              <td>{statusChip(o)}<div style={{ marginTop: 3 }}>{fresh(o)}</div></td>
              <td className="small num">{fmtDate(o.last_success_check_at)}</td>
              <td onClick={(e) => e.stopPropagation()} className="small"><a href={o.source_url} target="_blank" rel="noreferrer noopener">Page</a>{o.terms_url && <> · <a href={o.terms_url} target="_blank" rel="noreferrer noopener">Terms</a></>}</td>
            </tr>))}</tbody></table></div>}
        <p className="tiny faint">Retrieved {fmtDateTime(data.data?.retrieved)}. {data.data?.roster_note}</p>
      </>}

      {tab === "patterns" && (pat.loading && !pat.data ? <Loading /> : pat.error ? <ErrorBox message={pat.error} /> : pat.data && (
        <div className="stack">
          <Banner tone="info">Covers {pat.data.roster_with_current_offers} of {pat.data.roster_size} monitored brands with captured offers{pat.data.roster_missing.length ? ` (none captured for: ${pat.data.roster_missing.join(", ")})` : ""}. {pat.data.note}</Banner>
          {pat.data.mechanisms.length === 0 ? <Empty>No offers captured for this geography yet.</Empty> :
            <div className="tablewrap"><table><thead><tr><th>Mechanism</th><th className="r">Current offers</th><th>Brands</th><th className="r">Strengthened ({pat.data.window_days}d)<Help text="Advertised amount increased between versions" /></th><th className="r">Extended</th><th className="r">Removed / expired</th></tr></thead>
              <tbody>{pat.data.mechanisms.map((m: any) => <tr key={m.type}><td><b>{m.label}</b></td><td className="r num">{m.offers}</td><td className="small">{m.brands.join(", ")}</td><td className="r num">{m.strengthened}</td><td className="r num">{m.extended}</td><td className="r num">{m.removed}</td></tr>)}</tbody></table></div>}
        </div>))}

      {open && <OfferPanel id={open} admin={user.role === "admin"} onClose={() => { setOpen(null); data.reload(); }} />}
    </div>
  );
}

function OfferPanel({ id, admin, onClose }: { id: string; admin: boolean; onClose: () => void }) {
  const d = useFetch(`/api/offers/${id}`, [id]);
  const o = d.data;
  return (
    <Panel onClose={onClose} title={o ? <><h2>{o.title}</h2><div className="row small muted" style={{ marginTop: 4 }}><b>{o.brand}</b>{statusChip(o)}{fresh(o)}<span>{o.campaign_id}</span></div></> : "Loading…"}>
      {d.loading && !o && <Loading />}
      {d.error && <ErrorBox message={d.error} />}
      {o && <div className="stack">
        {o.review_reasons.length > 0 && <Banner>{o.review_reasons.map((r: string) => <div key={r}>• {r}</div>)}</Banner>}
        {o.conflicts.length > 0 && <Banner tone="bad"><strong>Landing page and terms disagree.</strong> Both are retained; a definitive comparison needs review.{o.conflicts.map((c: string) => <div key={c}>• {c}</div>)}</Banner>}
        {o.status_reason && <p className="small muted" style={{ margin: 0 }}>{o.status_reason}</p>}
        <section className="card"><h4>Benefits</h4><div className="tablewrap" style={{ marginTop: 8 }}><table><thead><tr><th>Type</th><th>What</th><th className="r">Advertised amount</th><th>Nature</th><th className="r">Customer pays</th></tr></thead><tbody>
          {o.mechanisms.map((m: any, i: number) => <tr key={i}><td>{m.type_label}</td><td>{m.label}{m.government_component && <div><Chip tone="info">Government component</Chip></div>}</td>
            <td className="r num">{m.amount != null ? <>{m.qualifier === "up_to" ? "up to " : m.qualifier === "from" ? "from " : ""}{money(m.amount)}{m.tax_basis && m.tax_basis !== "not_stated" ? <div className="tiny faint">{m.tax_basis === "incl_gst" ? "incl. GST" : "excl. GST"}</div> : null}</> : m.time_promise_days ? `${m.time_promise_days} days ${m.time_promise_type === "start_of_construction" ? "(to construction start)" : m.time_promise_type === "completion" ? "(to completion)" : ""}` : "Value not published"}</td>
            <td className="small">{KIND_NOTE[m.kind] ?? "–"}</td><td className="r num">{m.customer_payment_required != null ? money(m.customer_payment_required) : "–"}</td></tr>)}</tbody></table></div></section>
        <section className="card"><h4>Who it applies to</h4>
          <dl className="kv" style={{ marginTop: 8 }}>
            <dt>Geography</dt><dd>{o.geography.length ? o.geography.map((g: any, i: number) => <div key={i}>{g.name ?? g.state} <span className="faint small">({g.level}{g.state ? ", " + g.state : ""} · {g.basis === "page_context" ? "from the page it was listed on" : "stated in the offer text"})</span></div>) : <span className="faint">Not stated – not assumed to apply everywhere</span>}</dd>
            <dt>Ranges / designs</dt><dd>{[...o.ranges, ...o.designs].join(", ") || <span className="faint">Not stated</span>}</dd>
            <dt>Buyer type</dt><dd>{o.buyer_types.join(", ") || <span className="faint">Not stated</span>}</dd>
            <dt>Contract / deposit condition</dt><dd>{o.contract_condition || <span className="faint">Not stated</span>}</dd>
            <dt>Land status</dt><dd>{o.land_status || <span className="faint">Not stated</span>}</dd>
            <dt>Finance requirements</dt><dd>{o.finance_requirements.length ? o.finance_requirements.map((x: string) => <div key={x}>{x}</div>) : <span className="faint">None extracted</span>}</dd>
            <dt>Exclusions</dt><dd>{o.exclusions.length ? o.exclusions.map((x: string) => <div key={x}>{x}</div>) : <span className="faint">None extracted</span>}</dd>
          </dl></section>
        <section className="card"><h4>Dates</h4>
          <dl className="kv" style={{ marginTop: 8 }}>
            <dt>Published start</dt><dd>{o.published_start ? fmtDate(o.published_start) : <span className="faint">Not published</span>}</dd>
            <dt>Published expiry</dt><dd>{o.published_expiry ? <>{fmtDate(o.published_expiry)} <span className="faint small">({o.expiry_timezone}{o.expiry_timezone_assumed ? ", assumed – not stated" : ", as stated"})</span></> : <span className="faint">Expiry not published{o.expiry_text ? ` – “${o.expiry_text}”` : ""}</span>}</dd>
            <dt>First observed</dt><dd>{fmtDateTime(o.first_observed_at)} <span className="faint small">{o.is_baseline ? "– baseline capture: it may have launched earlier" : "– first time our crawler saw it"}</span></dd>
            <dt>Last successful check</dt><dd>{fmtDateTime(o.last_success_check_at)}</dd>
            <dt>Latest failed attempt</dt><dd>{o.last_failed_attempt_at ? <>{fmtDateTime(o.last_failed_attempt_at)} <span className="faint small">{o.failure_reason}</span></> : <span className="faint">None since last success</span>}</dd>
          </dl></section>
        <section className="card"><h4>Supporting evidence</h4>
          {o.evidence.length ? o.evidence.map((e: any, i: number) => <blockquote key={i}>“{e.excerpt}” <span className="tiny faint">({e.field}{e.source_url ? <> · <a href={e.source_url} target="_blank" rel="noreferrer noopener">source</a></> : null})</span></blockquote>) : <p className="muted small">No excerpts stored.</p>}
          {o.unresolved.length > 0 && <details className="small muted"><summary>Unresolved or removed during verification</summary><ul>{o.unresolved.map((u: string) => <li key={u}>{u}</li>)}</ul></details>}
          <div className="small" style={{ marginTop: 8 }}>{o.sources.map((s: any) => <div key={s.url}><a href={s.url} target="_blank" rel="noreferrer noopener">{s.url}</a> <span className="faint tiny">({s.role} · last seen {fmtDate(s.last_seen_at)} · fetch {s.last_outcome})</span></div>)}</div></section>
        <section className="card"><h4>Change history</h4>
          {o.versions.slice().reverse().map((v: any) => (
            <div key={v.version_no} style={{ padding: "8px 0", borderTop: "1px solid var(--line-soft)" }}>
              <div><b>Version {v.version_no}</b> <span className="faint small">· {fmtDateTime(v.observed_at)} · {v.method === "llm" ? "AI-assisted extraction" : "rules extraction"}{v.is_baseline ? " · baseline capture" : ""}</span></div>
              {v.diff.length === 0 ? <div className="small faint">{v.is_baseline || v.version_no === 1 ? "First capture – nothing to compare against." : "No field differences recorded."}</div> :
                v.diff.map((x: any, i: number) => <div key={i} className="small diff"><code>{x.field}</code>: <del>{JSON.stringify(x.before)}</del> → <ins>{JSON.stringify(x.after)}</ins></div>)}
            </div>))}
          {o.corrections.length > 0 && <><h4 style={{ marginTop: 12 }}>Corrections by people</h4>{o.corrections.map((c: any, i: number) => <div key={i} className="small" style={{ opacity: c.superseded ? .55 : 1 }}><code>{c.field}</code> → {JSON.stringify(c.value)} <span className="faint">({fmtDate(c.at)}{c.reason ? " · " + c.reason : ""}{c.superseded ? " · superseded" : ""})</span></div>)}</>}
        </section>
        {admin && <Correct o={o} onDone={d.reload} />}
      </div>}
    </Panel>
  );
}

const FIELDS: Record<string, "text" | "date" | "json" | "status"> = {
  status: "status", published_expiry: "date", published_start: "date", expiry_timezone: "text", expiry_text: "text", title: "text", headline: "text", terms_url: "text", geography: "json", eligibility: "json", benefits: "json",
};

function Correct({ o, onDone }: { o: any; onDone: () => void }) {
  const [field, setField] = useState("status");
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState("");
  const kind = FIELDS[field];
  const current: Record<string, any> = { geography: o.geography.map((g: any) => ({ level: g.level, state: g.state, name: g.name, as_stated: g.as_stated ?? g.name, basis: "stated" })), eligibility: { ranges: o.ranges, designs: o.designs, buyer_types: o.buyer_types, contract_condition: o.contract_condition, land_status: o.land_status, finance_requirements: o.finance_requirements, exclusions: o.exclusions },
    benefits: o.mechanisms.map((m: any) => ({ mechanism_type: m.type, label: m.label, amount: m.amount, amount_qualifier: m.qualifier, amount_kind: m.kind, customer_payment_required: m.customer_payment_required, builder_contribution: m.builder_contribution, government_component: m.government_component, tax_basis: m.tax_basis, time_promise_type: m.time_promise_type, time_promise_days: m.time_promise_days, evidence: m.excerpt })) };
  const submit = async () => {
    try {
      const v = kind === "json" ? JSON.parse(value) : value;
      await api.post(`/api/admin/offers/${o.campaign_id}/correct`, { field, value: v, reason });
      setMsg("Saved. The correction is kept across refreshes and recorded in the audit trail."); onDone();
    } catch (e) { setMsg("Not saved: " + (e as Error).message); }
  };
  return (
    <section className="card"><h4>Administrator: correct a field</h4>
      <p className="small muted">The machine extraction and its evidence stay on file; your value overrides what readers see until you remove it.</p>
      <div className="row"><label className="field">Field<select value={field} onChange={(e) => { setField(e.target.value); setValue(FIELDS[e.target.value] === "json" ? JSON.stringify(current[e.target.value], null, 2) : ""); }}>{Object.keys(FIELDS).map((f) => <option key={f}>{f}</option>)}</select></label>
        {kind === "status" && <label className="field">Status<select value={value} onChange={(e) => setValue(e.target.value)}><option value="">Choose…</option>{Object.entries(STATUS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>}
        {kind === "date" && <label className="field">Date<input type="date" value={value} onChange={(e) => setValue(e.target.value)} /></label>}
        {kind === "text" && <label className="field">Value<input type="text" value={value} onChange={(e) => setValue(e.target.value)} /></label>}</div>
      {kind === "json" && <textarea value={value} onChange={(e) => setValue(e.target.value)} />}
      <label className="field" style={{ marginTop: 8 }}>Reason (kept in the audit trail)<input type="text" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      <div className="row" style={{ marginTop: 10 }}><button className="btn primary" onClick={submit} disabled={!value}>Save correction</button>
        {o.status_set_by_person && <button className="btn" onClick={async () => { await api.post(`/api/admin/offers/${o.campaign_id}/release-status`); onDone(); }}>Return status to automatic</button>}
        <span className="small muted">{msg}</span></div></section>
  );
}
