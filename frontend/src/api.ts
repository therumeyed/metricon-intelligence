export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

async function req<T>(method: string, url: string, body?: unknown, isForm = false): Promise<T> {
  const headers: Record<string, string> = { "X-Requested-With": "metricon-ui" };
  let payload: BodyInit | undefined;
  if (body !== undefined) {
    if (isForm) payload = body as FormData;
    else { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  }
  const r = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    throw new ApiError(r.status, msg);
  }
  const ct = r.headers.get("content-type") || "";
  return (ct.includes("json") ? r.json() : r.text()) as Promise<T>;
}

export const api = {
  get: <T = any>(url: string) => req<T>("GET", url),
  post: <T = any>(url: string, body?: unknown) => req<T>("POST", url, body ?? {}),
  put: <T = any>(url: string, body?: unknown) => req<T>("PUT", url, body ?? {}),
  patch: <T = any>(url: string, body?: unknown) => req<T>("PATCH", url, body ?? {}),
  del: <T = any>(url: string) => req<T>("DELETE", url),
  upload: <T = any>(url: string, form: FormData) => req<T>("POST", url, form, true),
};

export function qs(params: Record<string, string | number | string[] | boolean | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x)); else u.append(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

export const fmtDate = (s?: string | null) => (s ? new Date(s.length === 10 ? s + "T00:00:00" : s).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric", timeZone: s && s.length > 10 ? "Australia/Sydney" : undefined }) : "–");
export const fmtDateTime = (s?: string | null) => (s ? new Date(s).toLocaleString("en-AU", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Australia/Sydney" }) : "–");
export const money = (n?: number | null) => (n == null ? "–" : "$" + Math.round(n).toLocaleString("en-AU"));
export const pct = (n?: number | null) => (n == null ? "–" : `${n > 0 ? "+" : ""}${n.toFixed(1)}%`);
