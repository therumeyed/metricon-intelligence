"""Administrator API: roster, sources, taxonomy, GSC imports, corrections, jobs and limits. All routes require role=admin + CSRF header."""
from __future__ import annotations

import re

import gzip
import os
import tempfile
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..config import get_settings
from ..db import get_db
from ..models import (
    AppSetting,
    AppUser,
    AuditLog,
    Brand,
    BrandAlias,
    BrandRegion,
    GscQueryRow,
    JobRun,
    Offer,
    QueryMappingOverride,
    RelatedQueryCandidate,
    Source,
    SourceSnapshot,
    Theme,
    ThemeKeyword,
    ThemeRule,
    TrendPlan,
)
from ..services import jobs, queries as Q, spend
from ..services import llm as llm_mod
from ..services.offers import pipeline

router = APIRouter(prefix="/api/admin", tags=["admin"])
STATES = {"VIC": "Melbourne", "NSW": "Sydney", "QLD": "Brisbane", "SA": "Adelaide"}


def audit(db: Session, user: AppUser, action: str, entity: str, entity_id: Any = None, before: Any = None, after: Any = None, note: str | None = None):
    db.add(AuditLog(user_id=user.id, action=action, entity=entity, entity_id=str(entity_id) if entity_id is not None else None, before=before, after=after, note=note))


# --------------------------------------------------------------------------- users
class UserIn(BaseModel):
    email: str
    password: str
    role: str = "viewer"
    name: Optional[str] = None


class UserPatch(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None
    name: Optional[str] = None


@router.get("/users")
def users(db: Session = Depends(get_db)):
    return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active, "last_login_at": u.last_login_at} for u in db.scalars(select(AppUser).order_by(AppUser.email))]


@router.post("/users")
def user_create(body: UserIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    try:
        u = auth.create_user(db, body.email, body.password, body.role, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(db, me, "user.create", "user", u.id, after={"email": u.email, "role": u.role})
    db.commit()
    return {"id": u.id}


@router.patch("/users/{uid}")
def user_patch(uid: int, body: UserPatch, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    u = db.get(AppUser, uid)
    if not u:
        raise HTTPException(404)
    if u.id == me.id and (body.active is False or (body.role and body.role != "admin")):
        raise HTTPException(400, "You cannot demote or deactivate yourself")
    if body.role:
        if body.role not in ("viewer", "admin"):
            raise HTTPException(400, "invalid role")
        u.role = body.role
    if body.active is not None:
        u.active = body.active
    if body.name is not None:
        u.name = body.name
    if body.password:
        try:
            u.password_hash = auth.hash_password(body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
    audit(db, me, "user.update", "user", u.id, after=body.model_dump(exclude={"password"}, exclude_none=True))
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- roster
class BrandIn(BaseModel):
    slug: Optional[str] = None
    name: str
    domains: list[str] = []
    states: list[str] = []
    aliases: list[str] = []
    notes: Optional[str] = None


class BrandPatch(BaseModel):
    name: Optional[str] = None
    domains: Optional[list[str]] = None
    active: Optional[bool] = None
    notes: Optional[str] = None
    states: Optional[list[str]] = None  # replaces the state assignments
    aliases: Optional[list[dict]] = None  # [{alias, kind, validated}] replaces aliases


def _brand_dict(b: Brand) -> dict:
    return {"id": b.id, "slug": b.slug, "name": b.name, "is_own": b.is_own, "domains": b.official_domains, "active": b.active, "notes": b.notes,
            "states": sorted(r.state for r in b.regions if r.active), "aliases": [{"alias": a.alias, "kind": a.kind, "validated": a.validated} for a in b.aliases]}


@router.get("/brands")
def brands(db: Session = Depends(get_db)):
    return [_brand_dict(b) for b in db.scalars(select(Brand).order_by(Brand.is_own.desc(), Brand.name))]


@router.post("/brands")
def brand_create(body: BrandIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    import re

    slug = body.slug or re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    if db.scalar(select(Brand).where(Brand.slug == slug)):
        raise HTTPException(400, "brand already exists")
    b = Brand(slug=slug, name=body.name, official_domains=body.domains, notes=body.notes)
    db.add(b)
    db.flush()
    for st in body.states:
        if st not in STATES:
            raise HTTPException(400, f"unknown state {st}")
        db.add(BrandRegion(brand_id=b.id, state=st, main_city=STATES[st]))
    for a in body.aliases or [body.name.lower()]:
        db.add(BrandAlias(brand_id=b.id, alias=a.lower(), kind="search_term"))
    audit(db, me, "brand.create", "brand", slug, after=body.model_dump())
    db.commit()
    return {"id": b.id, "slug": slug}


@router.patch("/brands/{slug}")
def brand_patch(slug: str, body: BrandPatch, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    b = db.scalar(select(Brand).where(Brand.slug == slug))
    if not b:
        raise HTTPException(404)
    before = _brand_dict(b)
    for f, col in (("name", "name"), ("domains", "official_domains"), ("active", "active"), ("notes", "notes")):
        v = getattr(body, f)
        if v is not None:
            setattr(b, col, v)
    if body.states is not None:
        want = set(body.states)
        if not want <= set(STATES):
            raise HTTPException(400, "unknown state")
        for r in list(b.regions):
            if r.state not in want:
                db.delete(r)
        have = {r.state for r in b.regions}
        for st in want - have:
            db.add(BrandRegion(brand_id=b.id, state=st, main_city=STATES[st]))
    if body.aliases is not None:
        for a in list(b.aliases):
            db.delete(a)
        db.flush()
        for a in body.aliases:
            db.add(BrandAlias(brand_id=b.id, alias=a["alias"].lower(), kind=a.get("kind", "name_variant"), validated=bool(a.get("validated"))))
    db.flush()
    db.refresh(b)
    audit(db, me, "brand.update", "brand", slug, before=before, after=_brand_dict(b))
    db.commit()
    return _brand_dict(b)


# --------------------------------------------------------------------------- sources
class SourceIn(BaseModel):
    brand_slug: str
    kind: str
    url: str
    state: Optional[str] = None
    context: dict = {}
    fetch_mode: str = "http"
    notes: Optional[str] = None


class SourcePatch(BaseModel):
    active: Optional[bool] = None
    kind: Optional[str] = None
    state: Optional[str] = None
    fetch_mode: Optional[str] = None
    notes: Optional[str] = None
    context: Optional[dict] = None


@router.get("/sources")
def sources(db: Session = Depends(get_db)):
    return Q.source_health(db)


@router.post("/sources")
def source_create(body: SourceIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    from ..seed import context_key

    brand = db.scalar(select(Brand).where(Brand.slug == body.brand_slug))
    if not brand:
        raise HTTPException(400, "unknown brand")
    if body.kind not in ("offer_listing", "offer_detail", "terms", "pdf", "competitor_page", "discovery_seed"):
        raise HTTPException(400, "invalid kind")
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    ck = context_key(body.context)
    if db.scalar(select(Source).where(Source.url == body.url, Source.context_key == ck)):
        raise HTTPException(400, "source already registered")
    s = Source(brand_id=brand.id, kind=body.kind, url=body.url, context_key=ck, context=body.context, state=body.state, fetch_mode=body.fetch_mode, notes=body.notes)
    db.add(s)
    db.flush()
    audit(db, me, "source.create", "source", s.id, after=body.model_dump())
    db.commit()
    return {"id": s.id}


@router.patch("/sources/{sid}")
def source_patch(sid: int, body: SourcePatch, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(s, k, v)
    audit(db, me, "source.update", "source", sid, after=changes)
    db.commit()
    return {"ok": True}


class ManualSnapshotIn(BaseModel):
    text: str
    note: Optional[str] = None


@router.post("/sources/{sid}/manual-snapshot")
def manual_snapshot(sid: int, body: ManualSnapshotIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    """For sources a crawler cannot reach (e.g. bot-protected pages): an administrator pastes the page text or HTML as evidence.
    It is stored as an immutable snapshot flagged fetch_mode='manual' and goes through the same extraction pipeline."""
    s = db.get(Source, sid)
    if not s:
        raise HTTPException(404)
    from ..services import content
    from ..timeutil import now_utc

    raw = body.text
    doc = content.html_to_clean(raw, s.url) if "<" in raw[:2000] and ">" in raw[:4000] else content.CleanDoc(title=None, blocks=content.dedupe_blocks([b.strip() for b in raw.splitlines() if b.strip()]))
    if not doc.blocks:
        raise HTTPException(400, "no readable text")
    now = now_utc()
    snap = SourceSnapshot(source_id=s.id, requested_url=s.url, final_url=s.url, http_status=None, outcome="ok", fetched_at=now, fetch_mode="manual", context=s.context or {},
                          content_type="text/html", clean_text=doc.text, content_hash=doc.content_hash, title=doc.title, links=doc.links,
                          raw_gz=gzip.compress(raw.encode()) if len(raw) < 3_000_000 else None, extraction_notes=None)
    db.add(snap)
    s.last_attempt_at = s.last_success_at = now
    s.last_outcome, s.last_error, s.consecutive_failures, s.last_content_hash = "ok", None, 0, snap.content_hash
    audit(db, me, "source.manual_snapshot", "source", sid, note=body.note)
    db.commit()
    run_id = jobs.enqueue("extract_offers", {}, trigger="manual", user_id=me.id)
    jobs.execute_in_thread(run_id)
    return {"snapshot_id": snap.id, "extract_job_id": run_id}


class SearchIn(BaseModel):
    brand_slug: str
    query: str
    kind: str = "offers"  # offers | news


@router.post("/searches")
def search_create(body: SearchIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    """Add a standing Google search for a brand. 'offers' reads the brand's own-site results for offers; 'news' reads Google News for activity."""
    from ..services.serp.collect import google_url

    brand = db.scalar(select(Brand).where(Brand.slug == body.brand_slug))
    if not brand:
        raise HTTPException(400, "unknown brand")
    q = " ".join(body.query.split())
    if not q or len(q) > 120:
        raise HTTPException(400, "query must be 1-120 characters")
    if re.search(r"\b(site|intitle|inurl|allintitle|allinurl):", q, re.I):
        raise HTTPException(400, "search operators are billed at a multiple of the normal price and are not supported")
    if body.kind not in ("offers", "news"):
        raise HTTPException(400, "kind must be offers or news")
    kind = "serp_offers" if body.kind == "offers" else "serp_news"
    url = google_url(q, news=body.kind == "news")
    if db.scalar(select(Source.id).where(Source.url == url, Source.context_key == "")):
        raise HTTPException(400, "this search already exists")
    src = Source(brand_id=brand.id, kind=kind, url=url, fetch_mode="serp", notes="Added by an administrator",
                 context={"search": {"query": q, "engine": "google", "result_kind": "news" if kind == "serp_news" else "organic", "location": "Australia", "language_code": "en", "state_in_query": None}})
    db.add(src)
    db.flush()
    audit(db, me, "search.create", "source", src.id, after=body.model_dump())
    db.commit()
    return {"id": src.id}


class FeedIn(BaseModel):
    url: str
    label: Optional[str] = None
    state: Optional[str] = None


@router.get("/feeds")
def feeds(db: Session = Depends(get_db)):
    from ..models import NewsFeed

    return [{"id": f.id, "url": f.url, "label": f.label, "state": f.state, "active": f.active, "last_fetched_at": f.last_fetched_at, "last_error": f.last_error, "items_last_run": f.items_last_run}
            for f in db.scalars(select(NewsFeed).order_by(NewsFeed.id))]


@router.post("/feeds")
def feed_create(body: FeedIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    """Add an RSS/Atom feed (for example a Google Alerts feed covering every brand). It is tested first so a bad URL is refused straight away."""
    import httpx

    from ..models import NewsFeed
    from ..services import fetcher, news

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "feed URL must be http(s)")
    if db.scalar(select(NewsFeed.id).where(NewsFeed.url == url)):
        raise HTTPException(400, "this feed is already added")
    try:
        with httpx.Client(timeout=25.0, headers={"User-Agent": fetcher.user_agent()}, follow_redirects=True) as c:
            items = news.fetch_feed(c, url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not read that feed: {type(e).__name__}: {str(e)[:160]}")
    if not items:
        raise HTTPException(400, "the address answered, but no RSS/Atom items were found in it")
    f = NewsFeed(url=url, label=(body.label or "").strip()[:160] or None, state=(body.state or None))
    db.add(f)
    db.flush()
    audit(db, me, "feed.create", "news_feed", f.id, after={"url": url})
    db.commit()
    return {"id": f.id, "items_found": len(items), "sample": [i["title"] for i in items[:3]]}


@router.patch("/feeds/{fid}")
def feed_patch(fid: int, body: dict, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    from ..models import NewsFeed

    f = db.get(NewsFeed, fid)
    if not f:
        raise HTTPException(404)
    if "active" in body:
        f.active = bool(body["active"])
    if "label" in body:
        f.label = (body["label"] or None)
    audit(db, me, "feed.update", "news_feed", fid, after=body)
    db.commit()
    return {"ok": True}


@router.delete("/feeds/{fid}")
def feed_delete(fid: int, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    from ..models import NewsFeed

    f = db.get(NewsFeed, fid)
    if not f:
        raise HTTPException(404)
    db.delete(f)
    audit(db, me, "feed.delete", "news_feed", fid)
    db.commit()
    return {"ok": True}


@router.post("/sources/{sid}/fetch")
def source_fetch(sid: int, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    src = db.get(Source, sid)
    if not src:
        raise HTTPException(404)
    from ..services.serp import SERP_KINDS

    if src.kind in SERP_KINDS:
        run_id = jobs.enqueue("collect_search", {"source_ids": [sid]}, trigger="manual", user_id=me.id)
        jobs.execute_in_thread(run_id)
        return {"job_id": run_id}
    run_id = jobs.enqueue("collect_sources", {"source_ids": [sid]}, trigger="manual", user_id=me.id)
    jobs.execute_in_thread(run_id)
    return {"job_id": run_id}


# --------------------------------------------------------------------------- themes / keywords / mappings
class ThemePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    active: Optional[bool] = None
    is_discovery: Optional[bool] = None


class RuleIn(BaseModel):
    pattern: str
    match_type: str = "regex"
    priority: int = 100


class KeywordIn(BaseModel):
    keyword: str
    subtheme: Optional[str] = None
    tracked: bool = False


@router.get("/themes")
def themes(db: Session = Depends(get_db)):
    return [{"id": t.id, "slug": t.slug, "name": t.name, "description": t.description, "active": t.active, "is_discovery": t.is_discovery,
             "rules": [{"id": r.id, "pattern": r.pattern, "match_type": r.match_type, "priority": r.priority} for r in t.rules],
             "keywords": [{"id": k.id, "keyword": k.keyword, "subtheme": k.subtheme, "tracked": k.tracked, "source": k.source, "gsc_impressions": k.gsc_impressions}
                          for k in sorted(t.keywords, key=lambda k: -(k.gsc_impressions or 0))]} for t in db.scalars(select(Theme).order_by(Theme.sort_order))]


@router.post("/themes")
def theme_create(body: dict, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    import re

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if db.scalar(select(Theme).where(Theme.slug == slug)):
        raise HTTPException(400, "theme exists")
    t = Theme(slug=slug, name=name, description=body.get("description"), is_discovery=bool(body.get("is_discovery")), sort_order=99)
    db.add(t)
    audit(db, me, "theme.create", "theme", slug)
    db.commit()
    return {"slug": slug}


@router.patch("/themes/{slug}")
def theme_patch(slug: str, body: ThemePatch, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    t = db.scalar(select(Theme).where(Theme.slug == slug))
    if not t:
        raise HTTPException(404)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(t, k, v)
    audit(db, me, "theme.update", "theme", slug, after=body.model_dump(exclude_none=True))
    db.commit()
    return {"ok": True}


@router.post("/themes/{slug}/rules")
def rule_add(slug: str, body: RuleIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    import re

    t = db.scalar(select(Theme).where(Theme.slug == slug))
    if not t:
        raise HTTPException(404)
    if body.match_type == "regex":
        try:
            re.compile(body.pattern)
        except re.error as e:
            raise HTTPException(400, f"invalid regex: {e}")
    r = ThemeRule(theme_id=t.id, pattern=body.pattern, match_type=body.match_type, priority=body.priority)
    db.add(r)
    audit(db, me, "theme.rule_add", "theme", slug, after=body.model_dump())
    db.commit()
    return {"id": r.id}


@router.delete("/rules/{rid}")
def rule_delete(rid: int, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    r = db.get(ThemeRule, rid)
    if r:
        audit(db, me, "theme.rule_delete", "theme_rule", rid, before={"pattern": r.pattern})
        db.delete(r)
        db.commit()
    return {"ok": True}


@router.post("/themes/{slug}/keywords")
def keyword_add(slug: str, body: KeywordIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    t = db.scalar(select(Theme).where(Theme.slug == slug))
    if not t:
        raise HTTPException(404)
    kw = body.keyword.strip().lower()
    if db.scalar(select(ThemeKeyword).where(ThemeKeyword.theme_id == t.id, ThemeKeyword.keyword == kw)):
        raise HTTPException(400, "keyword exists")
    db.add(ThemeKeyword(theme_id=t.id, keyword=kw, subtheme=body.subtheme, tracked=body.tracked, source="admin"))
    audit(db, me, "keyword.add", "theme", slug, after=body.model_dump())
    db.commit()
    return {"ok": True}


@router.patch("/keywords/{kid}")
def keyword_patch(kid: int, body: dict, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    k = db.get(ThemeKeyword, kid)
    if not k:
        raise HTTPException(404)
    if "tracked" in body:
        k.tracked = bool(body["tracked"])
    if "subtheme" in body:
        k.subtheme = body["subtheme"]
    audit(db, me, "keyword.update", "theme_keyword", kid, after=body)
    db.commit()
    return {"ok": True}


@router.delete("/keywords/{kid}")
def keyword_delete(kid: int, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    k = db.get(ThemeKeyword, kid)
    if k:
        audit(db, me, "keyword.delete", "theme_keyword", kid, before={"keyword": k.keyword})
        db.delete(k)
        db.commit()
    return {"ok": True}


@router.post("/gsc/import")
async def gsc_import(file: UploadFile = File(...), period_start: str = Form(""), period_end: str = Form(""), period_note: str = Form(""),
                     db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Upload the .xlsx export")
    data = await file.read()
    if len(data) > 60_000_000:
        raise HTTPException(400, "file too large")
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    run_id = jobs.enqueue("import_gsc", {"path": path, "file_name": file.filename, "user_id": me.id, "period_start": period_start or None, "period_end": period_end or None,
                                         "period_note": period_note or None}, trigger="manual", user_id=me.id)
    audit(db, me, "gsc.import", "gsc_import", run_id, note=file.filename)
    db.commit()
    jobs.execute_in_thread(run_id)
    return {"job_id": run_id}


@router.get("/gsc/queries")
def gsc_queries(q: str = "", unmapped: bool = False, theme: Optional[str] = None, limit: int = 100, db: Session = Depends(get_db)):
    from ..models import GscImport

    imp = db.scalar(select(GscImport).order_by(GscImport.id.desc()).limit(1))
    if not imp:
        return []
    stmt = select(GscQueryRow, Theme.slug).outerjoin(Theme, Theme.id == GscQueryRow.theme_id).where(GscQueryRow.import_id == imp.id)
    if q:
        stmt = stmt.where(GscQueryRow.query_norm.ilike(f"%{q.lower()}%"))
    if unmapped:
        stmt = stmt.where(GscQueryRow.theme_id.is_(None), GscQueryRow.excluded.is_(False))
    if theme:
        stmt = stmt.where(Theme.slug == theme)
    stmt = stmt.order_by(GscQueryRow.impressions.desc()).limit(min(limit, 500))
    return [{"id": r.id, "query": r.query, "impressions": r.impressions, "clicks": r.clicks, "ctr": r.ctr, "position": r.position, "theme": slug, "method": r.mapping_method,
             "excluded": r.excluded} for r, slug in db.execute(stmt)]


class OverrideIn(BaseModel):
    query: str
    theme: Optional[str] = None
    excluded: bool = False


@router.post("/gsc/override")
def gsc_override(body: OverrideIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    from ..services.gsc import normalise_query

    qn = normalise_query(body.query)
    theme = db.scalar(select(Theme).where(Theme.slug == body.theme)) if body.theme else None
    if not body.excluded and theme is None:
        raise HTTPException(400, "choose a theme or mark as excluded")
    ov = db.scalar(select(QueryMappingOverride).where(QueryMappingOverride.query_norm == qn)) or QueryMappingOverride(query_norm=qn)
    ov.theme_id, ov.excluded, ov.user_id = (theme.id if theme else None), body.excluded, me.id
    db.add(ov)
    db.execute(GscQueryRow.__table__.update().where(GscQueryRow.query_norm == qn).values(theme_id=ov.theme_id, excluded=body.excluded, mapping_method="admin"))
    audit(db, me, "gsc.override", "query", qn, after=body.model_dump())
    db.commit()
    return {"ok": True}


@router.post("/gsc/remap")
def gsc_remap(db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    from ..models import GscImport
    from ..services import gsc

    imp = db.scalar(select(GscImport).order_by(GscImport.id.desc()).limit(1))
    if not imp:
        raise HTTPException(400, "no import")
    out = gsc.remap_import(db, imp.id)
    audit(db, me, "gsc.remap", "gsc_import", imp.id)
    db.commit()
    return out


# --------------------------------------------------------------------------- offers: corrections
class CorrectionIn(BaseModel):
    field: str
    value: Any
    reason: Optional[str] = None


@router.post("/offers/{cid}/correct")
def offer_correct(cid: str, body: CorrectionIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    o = db.scalar(select(Offer).where(Offer.campaign_id == cid))
    if not o:
        raise HTTPException(404)
    try:
        pipeline.apply_correction(db, o, body.field, body.value, me.id, body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return Q.offer_dict(db, o, detail=True)


@router.post("/offers/{cid}/release-status")
def offer_release(cid: str, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    o = db.scalar(select(Offer).where(Offer.campaign_id == cid))
    if not o:
        raise HTTPException(404)
    pipeline.release_status(db, o, me.id)
    db.commit()
    return Q.offer_dict(db, o, detail=True)


# --------------------------------------------------------------------------- jobs, limits, trend plans, discovery review
class RunIn(BaseModel):
    name: str
    params: dict = {}


@router.get("/jobs")
def job_list(db: Session = Depends(get_db)):
    runs = db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(80))
    return {"runs": [{"id": r.id, "name": r.job_name, "status": r.status, "trigger": r.trigger, "queued_at": r.queued_at, "started_at": r.started_at, "finished_at": r.finished_at,
                      "result": r.result, "error": (r.error or "")[:600], "params": {k: v for k, v in (r.params or {}).items() if k != "path"}} for r in runs],
            "available": sorted(jobs.REGISTRY), "due": jobs.due_jobs(),
            "schedule": [{"job": s.job, "cadence": s.cadence, "hour_sydney": s.hour, "weekday": s.weekday} for s in jobs.SCHEDULES]}


@router.post("/jobs/run")
def job_run(body: RunIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    if body.name not in jobs.REGISTRY:
        raise HTTPException(400, "unknown job")
    if body.name == "import_gsc":
        raise HTTPException(400, "use the GSC upload")
    rid = jobs.enqueue(body.name, body.params, trigger="manual", user_id=me.id)
    audit(db, me, "job.run", "job", rid, after={"name": body.name})
    db.commit()
    jobs.execute_in_thread(rid)
    return {"job_id": rid}


@router.post("/jobs/{rid}/retry")
def job_retry(rid: int, me: AppUser = Depends(auth.current_user)):
    new = jobs.retry(rid, me.id)
    jobs.execute_in_thread(new)
    return {"job_id": new}


@router.get("/jobs/{rid}")
def job_get(rid: int, db: Session = Depends(get_db)):
    r = db.get(JobRun, rid)
    if not r:
        raise HTTPException(404)
    return {"id": r.id, "name": r.job_name, "status": r.status, "result": r.result, "error": r.error, "started_at": r.started_at, "finished_at": r.finished_at}


class LimitsIn(BaseModel):
    dataforseo_daily_usd_limit: Optional[float] = None
    dataforseo_monthly_usd_limit: Optional[float] = None
    dataforseo_max_requests_per_job: Optional[int] = None
    anthropic_max_calls_per_job: Optional[int] = None
    anthropic_daily_call_limit: Optional[int] = None


@router.get("/limits")
def limits_get(db: Session = Depends(get_db)):
    return {"limits": spend.limits(db), "spent": {"dataforseo": spend.spent(db, "dataforseo"), "anthropic": spend.spent(db, "anthropic")},
            "note": "Reported cost comes from the provider response; estimated cost is our configured per-request estimate. They are tracked separately."}


@router.put("/limits")
def limits_put(body: LimitsIn, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    for k, v in body.model_dump(exclude_none=True).items():
        if v < 0:
            raise HTTPException(400, "limits must be non-negative")
        row = db.get(AppSetting, k) or AppSetting(key=k, value=v)
        row.value = v
        db.add(row)
    audit(db, me, "limits.update", "settings", None, after=body.model_dump(exclude_none=True))
    db.commit()
    return limits_get(db)


@router.get("/trend-plans")
def plans(db: Session = Depends(get_db)):
    return [{"id": p.id, "key": p.plan_key, "purpose": p.purpose, "geo": p.geo_key, "keywords": p.keywords, "theme": p.theme_slug, "active": p.active, "pilot": p.pilot, "auto_managed": p.auto_managed}
            for p in db.scalars(select(TrendPlan).order_by(TrendPlan.pilot.desc(), TrendPlan.id))]


@router.patch("/trend-plans/{pid}")
def plan_patch(pid: int, body: dict, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    p = db.get(TrendPlan, pid)
    if not p:
        raise HTTPException(404)
    if "active" in body:
        p.active = bool(body["active"])
    if "keywords" in body:
        kws = [str(k).strip() for k in body["keywords"] if str(k).strip()]
        if not 1 <= len(kws) <= 5:
            raise HTTPException(400, "a comparison needs 1-5 keywords")
        p.keywords = kws
        p.auto_managed = False  # an administrator chose these: the weekly re-selection leaves them alone
    audit(db, me, "trend_plan.update", "trend_plan", pid, after=body)
    db.commit()
    return {"ok": True}


@router.get("/related")
def related(status: str = "proposed", db: Session = Depends(get_db)):
    return [{"id": r.id, "seed": r.seed, "query": r.query, "kind": r.kind, "value": r.value, "geo": r.geo_key, "suggested_theme": r.suggested_theme_slug, "status": r.review_status}
            for r in db.scalars(select(RelatedQueryCandidate).where(RelatedQueryCandidate.review_status == status, RelatedQueryCandidate.relevant.is_(True)).limit(100))]


@router.patch("/related/{rid}")
def related_review(rid: int, body: dict, db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    r = db.get(RelatedQueryCandidate, rid)
    if not r:
        raise HTTPException(404)
    st = body.get("status")
    if st not in ("accepted", "rejected"):
        raise HTTPException(400, "status must be accepted or rejected")
    r.review_status = st
    if st == "accepted":
        theme = db.scalar(select(Theme).where(Theme.slug == (body.get("theme") or r.suggested_theme_slug)))
        if theme and not db.scalar(select(ThemeKeyword).where(ThemeKeyword.theme_id == theme.id, ThemeKeyword.keyword == r.query)):
            db.add(ThemeKeyword(theme_id=theme.id, keyword=r.query, source="discovery", tracked=False))
    audit(db, me, "related.review", "related_query", rid, after=body)
    db.commit()
    return {"ok": True}


@router.get("/audit")
def audit_list(limit: int = 100, db: Session = Depends(get_db)):
    return [{"at": a.at, "user_id": a.user_id, "action": a.action, "entity": a.entity, "entity_id": a.entity_id, "before": a.before, "after": a.after, "note": a.note}
            for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500)))]


@router.post("/test-connections")
def test_connections():
    """Presence of each setting (names only, never values) plus one real, nearly free call per provider."""
    import os

    from ..services.trends.provider import DataForSEOTrends, ProviderError

    s = get_settings()
    present = {k: bool(os.environ.get(k, "").strip()) for k in ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")}
    out = {"environment_seen_by_web_service": present, "anthropic": llm_mod.test_connection()}
    if not s.dataforseo_ready:
        out["dataforseo"] = {"configured": False, "ok": False, "detail": "DATAFORSEO_LOGIN and/or DATAFORSEO_PASSWORD are empty on the web service"}
    else:
        try:
            rows = DataForSEOTrends().locations("au")  # the locations list is a free call
            au = next((r for r in rows if (r.get("location_name") or "").lower() == "australia"), None)
            out["dataforseo"] = {"configured": True, "ok": True, "detail": f"Login accepted. {len(rows)} Australian locations in the Google Trends catalogue" + ("" if au else " (Australia itself not listed)"),
                                 "locations": len(rows)}
        except ProviderError as e:
            out["dataforseo"] = {"configured": True, "ok": False, "detail": str(e)[:300]}
    out["note"] = "This tests the web service only. The hourly cron service has its own environment: the same four variables must be set there too."
    return out


@router.post("/trend-plans/activate-all")
def plans_activate_all(db: Session = Depends(get_db), me: AppUser = Depends(auth.current_user)):
    n = 0
    for p in db.scalars(select(TrendPlan).where(TrendPlan.active.is_(False))):
        p.active = True
        n += 1
    audit(db, me, "trend_plan.activate_all", "trend_plan", None, after={"activated": n})
    db.commit()
    return {"activated": n}


@router.get("/diagnostics")
def diagnostics(db: Session = Depends(get_db)):
    s = get_settings()
    return {"ai": llm_mod.status(), "dataforseo": {"connected": s.dataforseo_ready}, "trend_capabilities": Q.trend_capabilities(db), "sources": Q.source_health(db),
            "browser_fetch_enabled": s.allow_browser_fetch, "stale_after_hours": s.stale_after_hours,
            "extra_header_allowlist_configured": bool(s.fetch_extra_headers_json)}
