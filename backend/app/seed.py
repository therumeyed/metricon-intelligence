"""Idempotent seed loader. Creates missing rows only; never overwrites admin edits."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AppSetting,
    Brand,
    BrandAlias,
    BrandRegion,
    NewsQuery,
    Source,
    Theme,
    ThemeKeyword,
    ThemeRule,
)

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"
STATES = ["VIC", "NSW", "QLD", "SA"]


def context_key(ctx: dict | None) -> str:
    if not ctx:
        return ""
    key = ctx.get("region") or ctx.get("range") or ctx.get("item_type") or ""
    return str(key)[:120]


def load_yaml(name: str) -> dict:
    return yaml.safe_load((SEED_DIR / name).read_text())


def seed_brands(db: Session) -> dict:
    data = load_yaml("brands.yaml")
    cities = {k: v["main_city"] for k, v in data["states"].items()}
    created = {"brands": 0, "regions": 0, "aliases": 0}
    for b in data["brands"]:
        brand = db.scalar(select(Brand).where(Brand.slug == b["slug"]))
        if not brand:
            brand = Brand(slug=b["slug"], name=b["name"], is_own=b.get("is_own", False),
                          official_domains=b.get("domains", []), notes=b.get("notes"))
            db.add(brand)
            db.flush()
            created["brands"] += 1
        have_regions = {r.state for r in brand.regions}
        for st in b["states"]:
            if st not in have_regions:
                db.add(BrandRegion(brand_id=brand.id, state=st, main_city=cities[st]))
                created["regions"] += 1
        have_alias = {a.alias for a in brand.aliases}
        for a in b.get("aliases", []):
            if a["alias"] not in have_alias:
                db.add(BrandAlias(brand_id=brand.id, alias=a["alias"], kind=a.get("kind", "name_variant"), validated=False))
                created["aliases"] += 1
    db.flush()
    return created


def seed_themes(db: Session) -> dict:
    data = load_yaml("themes.yaml")
    created = {"themes": 0, "rules": 0, "keywords": 0}
    for i, t in enumerate(data["themes"]):
        theme = db.scalar(select(Theme).where(Theme.slug == t["slug"]))
        if not theme:
            theme = Theme(slug=t["slug"], name=t["name"], sort_order=i)
            db.add(theme)
            db.flush()
            created["themes"] += 1
            for pat in t.get("rules", []):
                db.add(ThemeRule(theme_id=theme.id, pattern=pat, match_type="regex", priority=t.get("priority", 100)))
                created["rules"] += 1
        have = {k.keyword for k in theme.keywords}
        for kw in t.get("seed_keywords", []):
            if kw not in have:
                db.add(ThemeKeyword(theme_id=theme.id, keyword=kw, source="seed", tracked=False))
                created["keywords"] += 1
    # exclusion patterns are kept in app_setting so admins can edit them
    if not db.get(AppSetting, "gsc_exclude_patterns"):
        db.add(AppSetting(key="gsc_exclude_patterns", value=data.get("exclude_patterns", [])))
    db.flush()
    return created


def seed_sources(db: Session) -> dict:
    data = load_yaml("sources.yaml")
    brands = {b.slug: b for b in db.scalars(select(Brand))}
    n = 0
    for s in data["sources"]:
        ctx = s.get("context") or {}
        ck = context_key(ctx)
        exists = db.scalar(select(Source).where(Source.url == s["url"], Source.context_key == ck))
        if exists:
            continue
        db.add(Source(brand_id=brands[s["brand"]].id, kind=s["kind"], url=s["url"], context_key=ck,
                      context=ctx, state=s.get("state"), notes=s.get("notes"),
                      fetch_mode=s.get("fetch_mode", "http")))
        n += 1
    db.flush()
    return {"sources": n}


def seed_news_queries(db: Session) -> dict:
    n = 0
    brands = list(db.scalars(select(Brand).where(Brand.active.is_(True))))
    existing = {(q.text, q.state) for q in db.scalars(select(NewsQuery))}
    for b in brands:
        term = '"GJ Gardner Homes"' if b.slug == "gj-gardner" else f'"{b.name}"'
        key = (term, None)
        if key not in existing:
            db.add(NewsQuery(text=key[0], state=None, brand_id=b.id))
            existing.add(key)
            n += 1
    topical = [
        ("new home builders Australia", None, "home-builders"),
        ("house and land packages", None, "house-and-land"),
        ("housing approvals Australia", None, None),
        ("first home buyer grant", None, "first-home-buyers-investors"),
        ("building costs residential Australia", None, "costs-affordability"),
        ("Victoria new homes policy", "VIC", None),
        ("NSW housing construction", "NSW", None),
        ("Queensland housing construction", "QLD", None),
        ("South Australia housing construction", "SA", None),
    ]
    for text, st, theme in topical:
        if (text, st) not in existing:
            db.add(NewsQuery(text=text, state=st, theme_slug=theme))
            existing.add((text, st))
            n += 1
    db.flush()
    return {"news_queries": n}


def seed_all(db: Session) -> dict:
    out = {}
    out.update(seed_brands(db))
    out.update(seed_themes(db))
    out.update(seed_sources(db))
    out.update(seed_news_queries(db))
    return out


if __name__ == "__main__":  # pragma: no cover
    from .db import session_scope

    with session_scope() as s:
        print(json.dumps(seed_all(s)))
