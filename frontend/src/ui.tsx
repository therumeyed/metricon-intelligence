import { ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, ReferenceLine } from "recharts";
import { api } from "./api";

export function useFetch<T = any>(url: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!url) return;
    let alive = true;
    setLoading(true);
    setError(null);
    api.get<T>(url).then((d) => alive && setData(d)).catch((e) => alive && setError(e.message)).finally(() => alive && setLoading(false));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, tick, ...deps]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}

export const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];

export function Chip({ children, tone }: { children: ReactNode; tone?: "good" | "warn" | "bad" | "info" | "own" }) {
  return <span className={`chip ${tone ?? ""}`}>{children}</span>;
}
export function Help({ text }: { text: string }) { return <span className="help" title={text} aria-label={text} tabIndex={0}>?</span>; }
export function Banner({ children, tone }: { children: ReactNode; tone?: "info" | "bad" }) { return <div className={`banner ${tone ?? ""}`} role="status">{children}</div>; }
export function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
export function Loading() { return <div className="muted" style={{ padding: 20 }}>Loading…</div>; }
export function ErrorBox({ message }: { message: string }) { return <Banner tone="bad"><strong>Couldn’t load this.</strong> {message}</Banner>; }

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { id: T; label: string }[] }) {
  return <div className="tabs" role="tablist">{items.map((i) => <button key={i.id} role="tab" aria-selected={value === i.id} className={value === i.id ? "on" : ""} onClick={() => onChange(i.id)}>{i.label}</button>)}</div>;
}

export function Tile({ label, value, sub, help }: { label: ReactNode; value: ReactNode; sub?: ReactNode; help?: string }) {
  return <div className="tile"><div className="label">{label}{help && <Help text={help} />}</div><div className="value">{value}</div><div className="sub">{sub}</div></div>;
}

export function Panel({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (<><div className="scrim" onClick={onClose} /><aside className="panel" role="dialog" aria-modal="true"><header><div>{title}</div><button className="btn sm" onClick={onClose}>Close</button></header><div className="body">{children}</div></aside></>);
}

export function download(url: string) { window.location.href = url; }

export function Sparkline({ points, color = "var(--s1)", w = 120, h = 30 }: { points: { v: number | null }[]; color?: string; w?: number; h?: number }) {
  const vals = points.filter((p) => p.v != null);
  if (vals.length < 3) return <span className="faint tiny">No data</span>;
  const step = w / Math.max(1, points.length - 1);
  let d = "";
  let pen = false;
  points.forEach((p, i) => {
    if (p.v == null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${(i * step).toFixed(1)},${(h - 2 - (p.v / 100) * (h - 4)).toFixed(1)}`;
    pen = true;
  });
  return <svg width={w} height={h} role="img" aria-label="Trend, individually normalised"><path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" /></svg>;
}

type Pt = { date: string; value: number | null; partial?: boolean };
export type SeriesIn = { key: string; label: string; color?: string; points: Pt[]; dashed?: boolean };

/** Multi-series line chart on ONE shared axis. Gaps stay gaps. Legend always shown for >1 series; table view for accessibility. */
export function LineSeriesChart({ series, yLabel, height = 300, yDomain, valueFmt, asOfNote, periodLabel = "Week starting", step }: { series: SeriesIn[]; yLabel?: string; height?: number; yDomain?: [number, number]; valueFmt?: (v: number) => string; asOfNote?: string; periodLabel?: string; step?: boolean }) {
  const [table, setTable] = useState(false);
  const rows = useMemo(() => {
    const m = new Map<string, any>();
    series.forEach((s) => s.points.forEach((p) => { const r = m.get(p.date) ?? { date: p.date }; r[s.key] = p.value; if (p.partial) r[s.key + "__partial"] = true; m.set(p.date, r); }));
    return [...m.values()].sort((a, b) => a.date.localeCompare(b.date));
  }, [series]);
  // the provider's final, incomplete period stays in the table (flagged) but is not drawn as if it were a finished week
  const chartRows = useMemo(() => rows.map((r) => { const c = { ...r }; series.forEach((s) => { if (r[s.key + "__partial"]) c[s.key] = null; }); return c; }), [rows, series]);
  if (!rows.length) return <Empty>No observations in this window.</Empty>;
  const fmt = valueFmt ?? ((v: number) => String(v));
  const direct = series.length > 1 && series.length <= 4;
  return (
    <div>
      <div className="row between">
        <div className="legend">{series.map((s, i) => <span key={s.key}><i style={{ background: s.color ?? SERIES[i % 8], opacity: s.dashed ? .55 : 1 }} />{s.label}</span>)}</div>
        <button className="btn sm" onClick={() => setTable(!table)}>{table ? "Show chart" : "Show table"}</button>
      </div>
      {table ? (
        <div className="tablewrap" style={{ maxHeight: height, overflow: "auto" }}>
          <table><thead><tr><th>Period</th>{series.map((s) => <th key={s.key} className="r">{s.label}</th>)}</tr></thead>
            <tbody>{rows.map((r) => <tr key={r.date}><td>{r.date}</td>{series.map((s) => <td key={s.key} className="r num">{r[s.key] == null ? "–" : fmt(r[s.key])}{r[s.key + "__partial"] ? " (partial)" : ""}</td>)}</tr>)}</tbody></table>
        </div>
      ) : (
        <div style={{ height }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartRows} margin={{ top: 8, right: direct ? 90 : 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--line-soft)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: "var(--ink-3)" }} tickLine={false} axisLine={{ stroke: "var(--line)" }} minTickGap={44} tickFormatter={(d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "2-digit" })} />
              <YAxis tick={{ fontSize: 11, fill: "var(--ink-3)" }} tickLine={false} axisLine={false} width={42} domain={yDomain ?? ["auto", "auto"]} label={yLabel ? { value: yLabel, angle: -90, position: "insideLeft", fontSize: 11, fill: "var(--ink-3)" } : undefined} tickFormatter={fmt as any} />
              <Tooltip content={<ChartTip series={series} fmt={fmt} periodLabel={periodLabel} />} cursor={{ stroke: "var(--ink-3)", strokeDasharray: "3 3" }} />
              {series.map((s, i) => (
                <Line key={s.key} type={step ? "stepAfter" : "monotone"} dataKey={s.key} stroke={s.color ?? SERIES[i % 8]} strokeWidth={2} strokeDasharray={s.dashed ? "5 4" : undefined} dot={false} activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }} connectNulls={false} isAnimationActive={false}
                  label={direct && !s.dashed ? (p: any) => (p.index === rows.length - 1 && p.value != null ? <text key={s.key} x={p.x + 8} y={p.y + 4} fontSize={11.5} fill="var(--ink-2)">{s.label.length > 16 ? s.label.slice(0, 15) + "…" : s.label}</text> : <g key={p.index} />) : undefined} />
              ))}
              <ReferenceLine y={0} stroke="var(--line)" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      {asOfNote && <div className="tiny faint" style={{ marginTop: 4 }}>{asOfNote}</div>}
    </div>
  );
}

function ChartTip({ active, label, payload, series, fmt, periodLabel }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <div className="tip">
      <div className="t">{periodLabel} {label}</div>
      {series.map((s: SeriesIn, i: number) => row[s.key] == null ? null : (
        <div className="r" key={s.key}><span><i style={{ background: s.color ?? SERIES[i % 8] }} />{s.label}</span><b className="num">{fmt(row[s.key])}{row[s.key + "__partial"] ? " · partial" : ""}</b></div>
      ))}
    </div>
  );
}

export function changeLabel(m?: { status: string; value_pct?: number | null; label?: string | null; reason?: string | null }) {
  if (!m) return <span className="faint">–</span>;
  if (m.status !== "ok" || m.value_pct == null) return <span className="faint" title={m.reason ?? ""}>{m.label || "Insufficient search data"}</span>;
  const up = m.value_pct > 0;
  return <span className="num"><b>{up ? "▲" : m.value_pct < 0 ? "▼" : "■"} {Math.abs(m.value_pct).toFixed(1)}%</b></span>;
}
