"""Polite, bounded HTTP fetching with snapshot storage.

* robots.txt is honoured (Disallow and Crawl-delay), with a per-host minimum delay.
* Outcome is classified (ok / http_error / blocked / timeout / network / too_large / robots_disallowed / empty).
  A bot-challenge page (e.g. AWS WAF challenge, Cloudflare interstitial) is `blocked`: we do not try to defeat it.
* Every attempt writes a SourceSnapshot row. Bodies are stored once; an unchanged re-fetch references the earlier
  snapshot. A failed fetch never changes offers - callers only get a flag back (brief s6 'failed fetch != withdrawn').
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Source, SourceSnapshot
from ..timeutil import now_utc
from . import content as content_mod

log = logging.getLogger(__name__)

_host_lock = threading.Lock()
_host_last: dict[str, float] = {}
_robots: dict[str, tuple[robotparser.RobotFileParser | None, float]] = {}


@dataclass
class FetchResult:
    outcome: str
    requested_url: str
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    body: bytes = b""
    chain: list[dict] = field(default_factory=list)
    error: str | None = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


def user_agent() -> str:
    s = get_settings()
    ua = s.user_agent
    return ua.replace("set FETCH_CONTACT", s.fetch_contact) if s.fetch_contact else ua


def extra_headers_for(url: str) -> dict:
    s = get_settings()
    if not s.fetch_extra_headers_json:
        return {}
    host = (urlsplit(url).hostname or "").lower()
    allowed = [h.strip().lower() for h in s.fetch_extra_headers_hosts.split(",") if h.strip()]
    if not any(host == h or host.endswith("." + h) for h in allowed):
        return {}
    try:
        return {str(k): str(v) for k, v in json.loads(s.fetch_extra_headers_json).items()}
    except (ValueError, AttributeError):
        return {}


def _client(url: str = "") -> httpx.Client:
    s = get_settings()
    return httpx.Client(follow_redirects=True, max_redirects=6, timeout=httpx.Timeout(s.fetch_timeout_s, connect=10.0),
                        headers={"User-Agent": user_agent(), "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
                                 "Accept-Language": "en-AU,en;q=0.8", **extra_headers_for(url)})


def _robots_for(client: httpx.Client, url: str) -> robotparser.RobotFileParser | None:
    p = urlsplit(url)
    key = f"{p.scheme}://{p.netloc}"
    cached = _robots.get(key)
    if cached and time.time() - cached[1] < 6 * 3600:
        return cached[0]
    rp = robotparser.RobotFileParser()
    try:
        r = client.get(f"{key}/robots.txt")
        if r.status_code == 200 and "html" not in (r.headers.get("content-type") or "").lower():
            rp.parse(r.text.splitlines())
        elif r.status_code in (401, 403):
            rp = None  # cannot read; fall back to polite defaults rather than assuming permission or denial
        else:
            rp.parse([])
    except httpx.HTTPError:
        rp = None
    _robots[key] = (rp, time.time())
    return rp


def _wait_for_host(host: str, delay: float) -> None:
    with _host_lock:
        last = _host_last.get(host, 0.0)
        wait = last + delay - time.time()
        _host_last[host] = max(time.time(), last + delay)
    if wait > 0:
        time.sleep(wait)


def _is_challenge(status: int, headers, body: bytes) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    if h.get("x-amzn-waf-action") == "challenge":
        return True
    if status in (202, 403, 429, 503):
        head = body[:4000].lower()
        if h.get("cf-mitigated") == "challenge" or b"just a moment" in head or b"captcha" in head or b"challenge-platform" in head:
            return True
        if status == 202 and len(body) == 0:
            return True
    return False


def fetch_url(url: str, *, honour_robots: bool = True) -> FetchResult:
    s = get_settings()
    res = FetchResult(outcome="network", requested_url=url)
    try:
        with _client(url) as client:
            host = urlsplit(url).netloc
            delay = s.fetch_per_host_delay_s
            if honour_robots:
                rp = _robots_for(client, url)
                if rp is not None:
                    if not rp.can_fetch(user_agent(), url):
                        res.outcome, res.error = "robots_disallowed", "disallowed by robots.txt"
                        return res
                    cd = rp.crawl_delay(user_agent()) or rp.crawl_delay("*")
                    if cd:
                        delay = max(delay, float(cd))
            _wait_for_host(host, delay)
            with client.stream("GET", url) as r:
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > s.fetch_max_bytes:
                        res.outcome, res.error = "too_large", f"response exceeded {s.fetch_max_bytes} bytes"
                        break
                    chunks.append(chunk)
                res.status = r.status_code
                res.final_url = str(r.url)
                res.content_type = (r.headers.get("content-type") or "").split(";")[0].strip().lower() or None
                res.headers = {k.lower(): v for k, v in r.headers.items() if k.lower() in {"etag", "last-modified", "x-amzn-waf-action", "server"}}
                res.chain = [{"url": str(h.url), "status": h.status_code} for h in r.history]
                if res.outcome == "too_large":
                    return res
                res.body = b"".join(chunks)
                if _is_challenge(r.status_code, r.headers, res.body):
                    res.outcome, res.error = "blocked", "bot-protection challenge returned instead of content (not bypassed)"
                elif r.status_code >= 400:
                    res.outcome, res.error = "http_error", f"HTTP {r.status_code}"
                elif not res.body.strip():
                    res.outcome, res.error = "empty", "empty response body"
                else:
                    res.outcome = "ok"
    except httpx.TimeoutException as e:
        res.outcome, res.error = "timeout", f"timeout: {e}"
    except httpx.HTTPError as e:
        res.outcome, res.error = "network", f"{type(e).__name__}: {e}"[:300]
    return res


def fetch_browser(url: str) -> FetchResult:
    """Playwright rendering for sources flagged fetch_mode=browser. Same rules: no stealth, challenges are reported."""
    s = get_settings()
    res = FetchResult(outcome="network", requested_url=url)
    if not s.allow_browser_fetch:
        res.outcome, res.error = "browser_disabled", "ALLOW_BROWSER_FETCH is off"
        return res
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        res.outcome, res.error = "browser_disabled", "playwright is not installed"
        return res
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=user_agent(), locale="en-AU")
            page = ctx.new_page()
            resp = page.goto(url, wait_until="networkidle", timeout=int(s.fetch_timeout_s * 1000))
            html = page.content()
            res.status = resp.status if resp else None
            res.final_url = page.url
            res.content_type = "text/html"
            res.body = html.encode()
            low = html.lower()
            if (resp and resp.headers.get("x-amzn-waf-action") == "challenge") or "awswaf" in low[:20000] and len(low) < 8000:
                res.outcome, res.error = "blocked", "bot-protection challenge persisted in the browser (not bypassed)"
            elif res.status and res.status >= 400:
                res.outcome, res.error = "http_error", f"HTTP {res.status}"
            else:
                res.outcome = "ok"
            browser.close()
    except Exception as e:  # noqa: BLE001
        res.outcome, res.error = "network", f"browser: {type(e).__name__}: {e}"[:300]
    return res


def fetch_source(db: Session, source: Source, *, force_mode: str | None = None) -> tuple[SourceSnapshot, content_mod.CleanDoc | None]:
    """Fetch one source, write a snapshot row, update the source's health fields. Returns (snapshot, clean doc)."""
    s = get_settings()
    mode = force_mode or source.fetch_mode
    r = fetch_browser(source.url) if mode == "browser" else fetch_url(source.url)
    now = now_utc()
    snap = SourceSnapshot(source_id=source.id, requested_url=source.url, final_url=r.final_url, redirect_chain=r.chain,
                          http_status=r.status, outcome=r.outcome, error=r.error, content_type=r.content_type, fetched_at=now,
                          fetch_mode=mode, context=source.context or {})
    doc = None
    source.last_attempt_at = now
    source.last_outcome = r.outcome
    if r.ok:
        is_pdf = (r.content_type or "").endswith("pdf") or (r.final_url or "").lower().split("?")[0].endswith(".pdf")
        try:
            doc = content_mod.pdf_to_clean(r.body) if is_pdf else content_mod.html_to_clean(r.body.decode(_charset(r), errors="replace"), r.final_url or source.url)
        except Exception as e:  # noqa: BLE001
            snap.outcome, snap.error = "parse_error", f"{type(e).__name__}: {e}"[:300]
            doc = None
        if doc is not None and not doc.blocks:
            snap.outcome, snap.error = "empty", "no readable text after cleaning"
            doc = None
        if doc is not None:
            snap.is_pdf = is_pdf
            snap.title = doc.title
            snap.clean_text = doc.text
            snap.content_hash = doc.content_hash
            snap.links = doc.links
            snap.raw_sha256 = hashlib.sha256(r.body).hexdigest()
            prev = db.scalar(select(SourceSnapshot).where(SourceSnapshot.source_id == source.id, SourceSnapshot.outcome == "ok")
                             .order_by(SourceSnapshot.id.desc()).limit(1))
            if prev is not None and prev.content_hash == snap.content_hash:
                # same denoised content: reference the earlier body instead of storing it again
                snap.unchanged_of_id = prev.unchanged_of_id or prev.id
                snap.clean_text, snap.links = None, None
            elif s.store_raw_snapshots and len(r.body) <= s.raw_snapshot_max_bytes:
                snap.raw_gz = gzip.compress(r.body)
    snapshot_ok = snap.outcome == "ok" and doc is not None
    if snapshot_ok:
        source.last_success_at, source.consecutive_failures = now, 0
        source.last_error, source.last_content_hash = None, snap.content_hash
    else:
        source.last_failure_at = now
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        source.last_error = snap.error
        source.last_outcome = snap.outcome
    db.add(snap)
    db.flush()
    return snap, doc


def _charset(r: FetchResult) -> str:
    return "utf-8"


def effective_text(db: Session, snap: SourceSnapshot) -> str | None:
    """Clean text for a snapshot, following unchanged_of_id back to the stored body."""
    if snap.clean_text is not None:
        return snap.clean_text
    if snap.unchanged_of_id:
        base = db.get(SourceSnapshot, snap.unchanged_of_id)
        return base.clean_text if base else None
    return None


def effective_links(db: Session, snap: SourceSnapshot) -> list[dict]:
    if snap.links is not None:
        return snap.links
    if snap.unchanged_of_id:
        base = db.get(SourceSnapshot, snap.unchanged_of_id)
        return (base.links or []) if base else []
    return []
