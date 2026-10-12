"""News discovery via Google News RSS (brief s10). Headline-only results are labelled as such and never support detailed claims."""
from __future__ import annotations

import html
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import httpx
from lxml import etree
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Brand, BrandAlias, NewsItem, NewsQuery
from ..timeutil import now_utc
from . import fetcher
from .gsc import state_hint

RSS = "https://news.google.com/rss/search?q={q}&hl=en-AU&gl=AU&ceid=AU:en"
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "oc", "ref", "cmpid", "ito"}
RESIDENTIAL = re.compile(r"\b(home|homes|house|houses|housing|dwelling|dwellings|builder|builders|building approvals?|new homes?|first home|house and land|land package|display home|knock ?down|duplex|townhouse|mortgage|cash rate|interest rate|construction costs?|building costs?|land supply|stamp duty|planning|build[- ]to[- ]rent|home buyers?)\b", re.I)
EXCLUDE = re.compile(r"\b(celebrity|mansion|star|actor|actress|singer|footballer|afl star|nrl star|wedding|obituary|skyscraper|office tower|shopping centre|warehouse|data centre|hospital|school|stadium|casino|hotel|airport|commercial (?:property|construction|building)|share price|asx:|stock|crypto|horoscope)\b", re.I)
DECOR = re.compile(r"\b(colour palette|color palette|paint colou?rs?|interior design|decorating|styling tips|decor|furniture|gardening|landscaping tips|diy|renovation tips|declutter|organis(?:e|ing) your|feng shui|real estate agents?)\b", re.I)
AU_SIGNAL = re.compile(r"\b(australia|australian|aussie|victoria|victorian|vic|melbourne|nsw|new south wales|sydney|queensland|qld|brisbane|gold coast|sunshine coast|south australia|adelaide|perth|tasmania|canberra|rba|abs|reserve bank|geelong|newcastle)\b", re.I)
GOV_SUFFIX = (".gov.au",)
STATS = ("abs.gov.au", "rba.gov.au")


def canonical(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q), ""))


def title_key(title: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", html.unescape(title)).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:200]


def classify_source(domain: str | None, brand_domains: set[str]) -> str:
    d = (domain or "").lower()
    if any(d == x or d.endswith("." + x) for x in STATS):
        return "official_stats"
    if d.endswith(GOV_SUFFIX):
        return "government"
    if any(d == x or d.endswith("." + x) for x in brand_domains):
        return "builder_claim"
    return "third_party"


def relevance(title: str, snippet: str | None, brand_hit: bool, publisher_domain: str | None = None) -> tuple[bool, str]:
    text = f"{title} {snippet or ''}"
    if DECOR.search(text):
        return False, "decorating / lifestyle content"
    if not (AU_SIGNAL.search(text) or (publisher_domain or "").endswith(".au")):
        return False, "no Australian context"
    if EXCLUDE.search(text) and not brand_hit:
        return False, "unrelated commercial/celebrity/finance topic"
    if brand_hit or RESIDENTIAL.search(text):
        return True, "residential home-building relevance"
    return False, "no residential home-building signal"


def parse_feed(xml: bytes) -> list[dict]:
    root = etree.fromstring(xml, parser=etree.XMLParser(recover=True, resolve_entities=False))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        pub = it.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except (TypeError, ValueError):
            published = None
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        out.append({"title": html.unescape(title), "link": link, "publisher": (src.text if src is not None else None),
                    "publisher_url": src.get("url") if src is not None else None, "published": published, "snippet": re.sub(r"\s+", " ", desc).strip()[:400]})
    return out


def fetch_query(client: httpx.Client, q: NewsQuery) -> list[dict]:
    text = q.text if "when:" in q.text else f"{q.text} when:30d"
    r = client.get(RSS.format(q=quote_plus(text)))
    r.raise_for_status()
    return parse_feed(r.content)[: get_settings().news_max_items_per_query]


def resolve_article(link: str) -> tuple[str | None, str | None]:
    """Try to reach the publisher's page from the RSS link. Returns (final_url, excerpt) or (None, None) if only the aggregator answers."""
    r = fetcher.fetch_url(link)
    if not r.ok or not r.final_url or "news.google.com" in r.final_url:
        return None, None
    try:
        from .content import html_to_clean

        doc = html_to_clean(r.body.decode("utf-8", errors="replace"), r.final_url)
        return canonical(r.final_url), " ".join(doc.blocks[:6])[:700]
    except Exception:  # noqa: BLE001
        return canonical(r.final_url), None


def collect(db: Session, max_resolve: int = 15) -> dict:
    brands = list(db.scalars(select(Brand)))
    brand_domains = {d for b in brands for d in (b.official_domains or [])}
    alias_map: list[tuple[str, int]] = []
    for a in db.scalars(select(BrandAlias)):
        alias_map.append((a.alias.lower(), a.brand_id))
    out = {"queries": 0, "new": 0, "duplicates": 0, "excluded": 0, "errors": [], "resolved": 0}
    resolved = 0
    with httpx.Client(timeout=25.0, headers={"User-Agent": fetcher.user_agent()}, follow_redirects=True) as client:
        for q in db.scalars(select(NewsQuery).where(NewsQuery.active.is_(True))):
            try:
                items = fetch_query(client, q)
            except Exception as e:  # noqa: BLE001 - one query failing never stops the rest
                out["errors"].append({"query": q.text, "error": f"{type(e).__name__}: {e}"[:160]})
                continue
            out["queries"] += 1
            for it in items:
                tk = title_key(it["title"])
                if not tk:
                    continue
                low = it["title"].lower()
                brand_ids = sorted({bid for alias, bid in alias_map if len(alias) > 5 and alias in low} | ({q.brand_id} if q.brand_id and any(a in low for a, b in alias_map if b == q.brand_id) else set()))
                pub_domain = urlsplit(it["publisher_url"]).hostname if it["publisher_url"] else None
                keep, why = relevance(it["title"], it["snippet"], bool(brand_ids), pub_domain)
                existing_title = db.scalar(select(NewsItem).where(NewsItem.title_key == tk))
                if existing_title is not None:
                    if it["publisher"] and it["publisher"] not in (existing_title.also_reported_by or []) and it["publisher"] != existing_title.publisher:
                        existing_title.also_reported_by = (existing_title.also_reported_by or []) + [it["publisher"]]
                    if q.id not in (existing_title.query_ids or []):
                        existing_title.query_ids = (existing_title.query_ids or []) + [q.id]
                    out["duplicates"] += 1
                    continue
                url = canonical(it["link"])
                if db.scalar(select(NewsItem.id).where(NewsItem.canonical_url == url)):
                    out["duplicates"] += 1
                    continue
                item = NewsItem(canonical_url=url, title=it["title"], title_key=tk, publisher=it["publisher"], publisher_domain=pub_domain,
                                source_class=classify_source(pub_domain, brand_domains), published_at=it["published"], retrieved_at=now_utc(),
                                snippet=it["snippet"], evidence_level="headline_only", state=q.state or state_hint(low), brand_ids=brand_ids,
                                theme_slug=q.theme_slug, relevance="kept" if keep else "excluded", relevance_reason=why, query_ids=[q.id])
                if keep and resolved < max_resolve:
                    final, excerpt = resolve_article(it["link"])
                    resolved += 1
                    if final and excerpt:
                        item.resolved_url, item.body_excerpt, item.evidence_level = final, excerpt, "article"
                        item.canonical_url = final if not db.scalar(select(NewsItem.id).where(NewsItem.canonical_url == final)) else item.canonical_url
                        out["resolved"] += 1
                db.add(item)
                out["new" if keep else "excluded"] += 1
            db.commit()
    return out


def reclassify(db: Session) -> dict:
    """Re-apply the relevance rules to stored items (after tuning the filters). Evidence is untouched."""
    changed = 0
    for n in db.scalars(select(NewsItem)):
        keep, why = relevance(n.title, n.snippet, bool(n.brand_ids), n.publisher_domain)
        new = "kept" if keep else "excluded"
        if n.relevance != new or n.relevance_reason != why:
            n.relevance, n.relevance_reason = new, why
            changed += 1
    return {"changed": changed}
