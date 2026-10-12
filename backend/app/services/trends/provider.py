"""DataForSEO Google Trends adapter (the Google-sourced Explore endpoint; NOT the separate proprietary DataForSEO Trends product).

Endpoints used (documented):
  POST /v3/keywords_data/google_trends/explore/live     one task per call, <=5 keywords; queries/topics lists need exactly 1 keyword
  GET  /v3/keywords_data/google_trends/locations/{country}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from ...config import get_settings

BAD_CHARS = re.compile(r"[<>|\"\-+=~!:*()\[\]{}]")


class ProviderError(RuntimeError):
    def __init__(self, msg: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status_code, self.retryable = status_code, retryable


def clean_keyword(k: str) -> str:
    """Provider forbids several characters (including hyphens). Strip them rather than send a request that will fail."""
    k = BAD_CHARS.sub(" ", k).replace(",", " ")
    return re.sub(r"\s+", " ", k).strip()[:100]


@dataclass
class ExploreResult:
    cost: float | None
    raw: dict
    graph: dict[str, list[dict]]  # keyword -> [{start,end,value,missing}]
    queries: dict[str, list[dict]]  # {'top': [...], 'rising': [...]}
    check_url: str | None
    location_code: int | None


class DataForSEOTrends:
    def __init__(self, client: httpx.Client | None = None):
        s = get_settings()
        if not s.dataforseo_ready:
            raise ProviderError("DataForSEO credentials not set (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD)")
        self.base = s.dataforseo_base_url.rstrip("/")
        self.client = client or httpx.Client(auth=(s.dataforseo_login, s.dataforseo_password), timeout=httpx.Timeout(90.0, connect=15.0))

    def _post(self, path: str, body: list[dict]) -> dict:
        try:
            r = self.client.post(f"{self.base}{path}", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"network error: {e}", retryable=True) from e
        if r.status_code in (429, 500, 502, 503, 504):
            raise ProviderError(f"HTTP {r.status_code}", r.status_code, retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json()

    def locations(self, country_iso: str = "au") -> list[dict]:
        try:
            r = self.client.get(f"{self.base}/v3/keywords_data/google_trends/locations/{country_iso.lower()}")
        except httpx.HTTPError as e:
            raise ProviderError(f"network error: {e}", retryable=True) from e
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code}", r.status_code, retryable=r.status_code >= 500)
        j = r.json()
        task = (j.get("tasks") or [{}])[0]
        if task.get("status_code") != 20000:
            raise ProviderError(f"{task.get('status_code')}: {task.get('status_message')}")
        return (task.get("result") or [])

    def explore(self, *, keywords: list[str], location_code: int | None, date_from: str | None = None, date_to: str | None = None,
                time_range: str | None = None, search_type: str = "web", category_code: int = 0, item_types: list[str] | None = None) -> ExploreResult:
        kws = [clean_keyword(k) for k in keywords]
        item_types = item_types or ["google_trends_graph"]
        if len(kws) > 5:
            raise ProviderError("Trends comparison accepts at most 5 keywords")
        if ("google_trends_queries_list" in item_types or "google_trends_topics_list" in item_types) and len(kws) != 1:
            raise ProviderError("related queries/topics need exactly one keyword")
        task: dict[str, Any] = {"keywords": kws, "type": search_type, "category_code": category_code, "item_types": item_types}
        if location_code is not None:
            task["location_code"] = location_code
        if date_from or date_to:
            task["date_from"], task["date_to"] = date_from, date_to
        elif time_range:
            task["time_range"] = time_range
        j = self._post("/v3/keywords_data/google_trends/explore/live", [task])
        t = (j.get("tasks") or [{}])[0]
        if t.get("status_code") != 20000:
            raise ProviderError(f"provider task error {t.get('status_code')}: {t.get('status_message')}", t.get("status_code"),
                                retryable=str(t.get("status_code", "")).startswith("5"))
        res = (t.get("result") or [{}])[0]
        graph: dict[str, list[dict]] = {}
        queries: dict[str, list[dict]] = {}
        for item in res.get("items") or []:
            typ = item.get("type")
            if typ == "google_trends_graph":
                kw_list = item.get("keywords") or res.get("keywords") or kws
                for pt in item.get("data") or []:
                    vals = pt.get("values")
                    vals = vals if isinstance(vals, list) else [vals]
                    for i, kw in enumerate(kw_list):
                        v = vals[i] if i < len(vals) else None
                        graph.setdefault(kw, []).append({"start": pt.get("date_from"), "end": pt.get("date_to"), "value": v,
                                                         "missing": bool(pt.get("missing_data"))})
            elif typ == "google_trends_queries_list":
                data = item.get("data") or {}
                queries = {"top": data.get("top") or [], "rising": data.get("rising") or []}
        return ExploreResult(cost=t.get("cost") if t.get("cost") is not None else j.get("cost"), raw=j, graph=graph, queries=queries,
                             check_url=res.get("check_url"), location_code=res.get("location_code"))
