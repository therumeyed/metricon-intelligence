"""DataForSEO SERP adapter (Google organic + Google News, live/advanced).

Endpoints used (documented):
  POST /v3/serp/google/organic/live/advanced   items of type 'organic'
  POST /v3/serp/google/news/live/advanced      items of type 'news_search'
  GET  /v3/serp/google/locations/{country}     location catalogue (free)

One task per request; billed per results page of 10. `site:` / `intitle:` operators cost several times more, so we never send them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from ...config import get_settings
from ..trends.provider import ProviderError


@dataclass
class SerpItem:
    title: str
    url: str
    domain: str
    snippet: str
    published_at: datetime | None = None
    publisher: str | None = None
    rank: int | None = None


@dataclass
class SerpResult:
    query: str
    kind: str  # organic | news
    items: list[SerpItem]
    cost: float | None
    check_url: str | None = None
    location_code: int | None = None
    raw_count: int = 0
    raw: dict = field(default_factory=dict)


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\s*([+-]\d{2}):?(\d{2}))?")


def parse_timestamp(v: Any) -> datetime | None:
    """Provider timestamps look like '2026-10-12 03:21:00 +00:00'. Anything else is treated as undated rather than guessed."""
    if not v or not isinstance(v, str):
        return None
    m = _TS.match(v.strip())
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
    except ValueError:
        return None
    if m.group(3):
        from datetime import timedelta

        sign = -1 if m.group(3).startswith("-") else 1
        off = timedelta(hours=abs(int(m.group(3))), minutes=int(m.group(4) or 0)) * sign
        return (dt - off).replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _flatten(items: list[dict]) -> list[dict]:
    out = []
    for it in items or []:
        sub = it.get("items")
        if isinstance(sub, list) and it.get("type") not in ("organic", "news_search"):
            out.extend(_flatten(sub))  # e.g. top_stories containers
        else:
            out.append(it)
    return out


def parse_items(items: list[dict], kind: str) -> list[SerpItem]:
    wanted = {"organic": ("organic",), "news": ("news_search", "top_stories", "organic")}[kind]
    out, seen = [], set()
    for it in _flatten(items):
        if it.get("type") not in wanted:
            continue
        url = (it.get("url") or "").strip()
        title = (it.get("title") or "").strip()
        if not url or not title or url in seen:
            continue
        seen.add(url)
        snippet = " ".join(x.strip() for x in (it.get("description"), it.get("snippet"), it.get("extended_snippet")) if isinstance(x, str) and x.strip())
        domain = (it.get("domain") or "").lower().removeprefix("www.")
        out.append(SerpItem(title=title, url=url, domain=domain, snippet=snippet[:700], published_at=parse_timestamp(it.get("timestamp") or it.get("time_published")),
                            publisher=(it.get("source") or it.get("publisher") or None), rank=it.get("rank_absolute") or it.get("rank_group")))
    return out


class DataForSEOSerp:
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
            r = self.client.get(f"{self.base}/v3/serp/google/locations/{country_iso.lower()}")
        except httpx.HTTPError as e:
            raise ProviderError(f"network error: {e}", retryable=True) from e
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code}", r.status_code, retryable=r.status_code >= 500)
        task = (r.json().get("tasks") or [{}])[0]
        if task.get("status_code") != 20000:
            raise ProviderError(f"{task.get('status_code')}: {task.get('status_message')}")
        return task.get("result") or []

    def search(self, *, query: str, kind: str = "organic", location_code: int | None = None, language_code: str = "en", depth: int = 10, device: str = "desktop") -> SerpResult:
        if kind not in ("organic", "news"):
            raise ProviderError(f"unknown search kind {kind!r}")
        if re.search(r"\b(site|intitle|inurl|allintitle|allinurl):", query, re.I):
            raise ProviderError("search operators are billed at a multiple of the normal price and are not used")
        task: dict[str, Any] = {"keyword": query, "language_code": language_code, "depth": depth, "device": device}
        if location_code is not None:
            task["location_code"] = location_code
        j = self._post(f"/v3/serp/google/{kind}/live/advanced", [task])
        t = (j.get("tasks") or [{}])[0]
        if t.get("status_code") != 20000:
            raise ProviderError(f"provider task error {t.get('status_code')}: {t.get('status_message')}", t.get("status_code"),
                                retryable=str(t.get("status_code", "")).startswith("5"))
        res = (t.get("result") or [{}])[0]
        raw_items = res.get("items") or []
        return SerpResult(query=query, kind=kind, items=parse_items(raw_items, kind), cost=t.get("cost") if t.get("cost") is not None else j.get("cost"),
                          check_url=res.get("check_url"), location_code=res.get("location_code") or location_code, raw_count=len(raw_items), raw={"type_counts": _counts(raw_items)})


def _counts(items: list[dict]) -> dict:
    c: dict[str, int] = {}
    for it in items:
        c[it.get("type") or "?"] = c.get(it.get("type") or "?", 0) + 1
    return c
