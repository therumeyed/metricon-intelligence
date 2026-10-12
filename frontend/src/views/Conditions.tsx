import { useApp } from "../App";
import { fmtDate, pct } from "../api";
import { Banner, Chip, ErrorBox, Help, LineSeriesChart, Loading, useFetch } from "../ui";

const ORDER = ["house_approvals", "house_output_ppi", "cash_rate"];
const TITLES: Record<string, string> = { house_approvals: "Private-sector house approvals", house_output_ppi: "House construction output price index", cash_rate: "Cash rate target" };

export default function Conditions() {
  const { geo } = useApp();
  const macro = useFetch(`/api/macro?geo=${geo}`, [geo]);
  const news = useFetch(`/api/news?state=${geo}&limit=25`, [geo]);
  if (macro.error) return <ErrorBox message={macro.error} />;
  if (!macro.data) return <Loading />;
  const groups = ORDER.map((ind) => ({ ind, series: (macro.data?.series ?? []).filter((s: any) => s.indicator === ind) }));
  return (
    <div>
      <div className="page-head"><div><h1>Market conditions</h1><p>Official statistics, with the exact series, units, adjustment basis and release dates. Where a state series is not published, it is shown as missing – never filled from the national figure.</p></div></div>
      <div className="stack">
        {groups.map(({ ind, series }) => {
          const s = series.find((x: any) => x.geography === geo) ?? series[0];
          if (!s) return <section className="card" key={ind}><h2>{TITLES[ind]}</h2><p className="muted">No data for {geo} – this indicator is not published for the selected geography.</p></section>;
          const national = s.geography === "AU" && geo !== "AU";
          const lag = s.release?.publication_lag_days;
          return (
            <section className="card" key={ind}>
              <div className="row between">
                <h2>{s.name} <Help text={s.description} /></h2>
                <div className="row small muted">
                  <Chip tone={national ? "warn" : "info"}>{national ? "National series – shared by every state" : s.geography === "AU" ? "National data" : "State data"}</Chip>
                  <Chip>{s.frequency}</Chip><Chip>{s.adjustment}</Chip>
                </div>
              </div>
              <div className="row" style={{ margin: "8px 0 12px", gap: 28 }}>
                <div><div className="tiny faint">Latest ({s.latest?.period})</div><div style={{ fontSize: 26, fontWeight: 700 }} className="num">{s.latest ? (ind === "cash_rate" ? s.latest.value.toFixed(2) + "%" : s.latest.value.toLocaleString("en-AU", { maximumFractionDigits: 1 })) : "–"}</div><div className="tiny faint">{s.unit}</div></div>
                {ind !== "cash_rate" ? <>
                  <div><div className="tiny faint">vs previous {s.frequency === "monthly" ? "month" : "quarter"}</div><b className="num">{pct(s.comparison?.vs_previous_period?.pct)}</b></div>
                  <div><div className="tiny faint">vs same period last year</div><b className="num">{pct(s.comparison?.vs_year_ago?.pct)}</b></div>
                </> : <div><div className="tiny faint">Previous rate</div><b className="num">{s.comparison?.previous_value?.toFixed(2)}% (set {fmtDate(s.comparison?.previous_change_date)})</b></div>}
                <div><div className="tiny faint">Published</div><b>{fmtDate(s.release?.publication_date)}</b>{lag != null && <div className="tiny faint">{lag} days after the period ended</div>}</div>
                {s.next_release && <div><div className="tiny faint">Next release</div><b>{s.next_release}</b></div>}
              </div>
              <LineSeriesChart height={220} series={[{ key: "v", label: s.name, points: s.points.map((p: any) => ({ date: p.period_end ?? p.period, value: p.value })) }]} valueFmt={(v) => ind === "cash_rate" ? v.toFixed(2) : v.toLocaleString("en-AU", { maximumFractionDigits: 1 })} yLabel={s.unit} periodLabel="Period ending" step={ind === "cash_rate"} />
              <div className="tiny faint" style={{ marginTop: 6 }}>Source: <a href={s.source_url} target="_blank" rel="noreferrer noopener">{s.source}</a> · series key <code>{s.dataset}:{s.series_key}</code> · retrieved {fmtDate(s.last_retrieved)}</div>
            </section>
          );
        })}
        <section className="card">
          <div className="row between"><h2>Policy, industry and market reporting</h2><span className="tiny faint">Headlines from a news aggregator unless marked “Article”. Original sources linked.</span></div>
          {news.data?.length ? <div className="tablewrap" style={{ marginTop: 10 }}><table><thead><tr><th>Headline</th><th>Publisher</th><th>Type</th><th>Published</th></tr></thead><tbody>
            {news.data.map((n: any) => <tr key={n.id}><td><a href={n.url} target="_blank" rel="noreferrer noopener">{n.title}</a>{n.also_reported_by?.length > 0 && <div className="tiny faint">Also: {n.also_reported_by.join(", ")}</div>}</td><td>{n.publisher}</td>
              <td><Chip tone={n.source_class === "government" || n.source_class === "official_stats" ? "good" : undefined}>{n.source_class.replace("_", " ")}</Chip> <span className="tiny faint">{n.label}</span></td><td className="num">{fmtDate(n.published_at)}</td></tr>)}</tbody></table></div>
            : <p className="muted">No relevant items collected for this view yet.</p>}
        </section>
      </div>
      {geo !== "AU" && <Banner tone="info">The cash rate is a single national rate. Approvals and the output price index are state series where the ABS publishes them.</Banner>}
    </div>
  );
}
