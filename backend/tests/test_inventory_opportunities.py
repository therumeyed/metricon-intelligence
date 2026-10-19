"""Page inventory + computed search opportunities. Sitemaps, GSC rows and trend candidates here are SYNTHETIC fixtures."""
from sqlalchemy import select

from app.models import RelatedQueryCandidate, SitePage
from app.services import fetcher, gsc, inventory, opportunities as O
from tests.helpers import ok_result
from tests.test_gsc import make_workbook

INDEX = """<?xml version="1.0"?><sitemapindex><sitemap><loc>https://www.metricon.com.au/sitemap.xml?site=default&amp;s=2</loc></sitemap></sitemapindex>"""
SUB = """<urlset><url><loc>https://www.metricon.com.au/knockdown-rebuild/melbourne/</loc></url>
<url><loc>https://www.metricon.com.au/house-designs/melbourne/lumiere?utm=x</loc></url>
<url><loc>https://files.metricon.com.au/brochure.pdf</loc></url><url><loc>https://other.example/x</loc></url>
<url><loc>https://www.metricon.com.au/house-and-land/packages/melbourne</loc></url></urlset>"""


def test_canonical_keeps_only_own_site_html_pages_without_query():
    d = ["metricon.com.au"]
    assert inventory.canonical("https://www.metricon.com.au/a/b/?x=1#f", d) == "https://www.metricon.com.au/a/b"
    assert inventory.canonical("http://metricon.com.au/", d) == "https://metricon.com.au/"
    assert inventory.canonical("https://files.metricon.com.au/x.pdf", d) is None
    assert inventory.canonical("https://other.example/x", d) is None


def test_sitemap_index_and_pages_are_read_and_classified(seeded, monkeypatch):
    def fake(url, **k):
        return ok_result(url, INDEX if url.endswith("/sitemap.xml") else SUB)

    monkeypatch.setattr(fetcher, "fetch_url", fake)
    out = inventory.refresh_from_sitemap(seeded)
    seeded.commit()
    assert out["sitemap_pages"] == 3 and out["sitemaps_read"] == 2
    kd = seeded.scalar(select(SitePage).where(SitePage.path == "/knockdown-rebuild/melbourne"))
    assert kd.page_kind == "knockdown_rebuild" and kd.state_hint == "VIC" and kd.sources == ["sitemap"]


def test_gsc_pages_merge_with_sitemap_pages_and_fold_query_strings(seeded, tmp_path):
    p = tmp_path / "g.xlsx"
    make_workbook(p, pages=[("https://www.metricon.com.au/house-and-land/packages/melbourne", 5, 500, 0.01, 10.0),
                            ("https://www.metricon.com.au/house-and-land/packages/melbourne?page=2", 1, 500, 0.002, 20.0)])
    gsc.import_workbook(seeded, p)
    seeded.add(SitePage(url="https://www.metricon.com.au/house-and-land/packages/melbourne", path="/house-and-land/packages/melbourne", sources=["sitemap"]))
    seeded.flush()
    out = inventory.refresh_from_gsc(seeded)
    row = seeded.scalar(select(SitePage).where(SitePage.path == "/house-and-land/packages/melbourne"))
    assert out["gsc_pages"] == 1 and sorted(row.sources) == ["gsc", "sitemap"] and row.impressions == 1000 and row.clicks == 6 and round(row.position, 1) == 15.0


def pages(*paths):
    return [SitePage(url=f"https://www.metricon.com.au{p}", path=p, sources=["sitemap"], page_kind=gsc.classify_page(f"https://www.metricon.com.au{p}")["page_kind"],
                     state_hint=gsc.classify_page(f"https://www.metricon.com.au{p}")["state_hint"]) for p in paths]


def test_topic_matching_folds_plurals_and_place_names_and_does_not_overreach(seeded):
    pg = pages("/knockdown-rebuild/melbourne", "/house-and-land/packages/sydney", "/display-homes/qld/brisbane", "/house-designs/melbourne/lumiere")
    cov = lambda t, **k: [x["path"] for x in inventory.page_coverage(seeded, t, pages=pg, **k)]  # noqa: E731
    assert cov("knock down rebuild melbourne") == ["/knockdown-rebuild/melbourne"]  # split spelling is folded
    assert cov("knockdown rebuilds melbourne") == ["/knockdown-rebuild/melbourne"]
    assert cov("house and land packages sydney") == ["/house-and-land/packages/sydney"]
    assert cov("display homes victoria") == []
    assert cov("display homes queensland") == ["/display-homes/qld/brisbane"]
    assert cov("granny flats") == []


def seeded_gsc(seeded, tmp_path):
    q = [("house and land packages melbourne", 40, 3000, 0.0133, 6.5), ("metricon house and land", 300, 900, 0.33, 1.2), ("display homes brisbane", 5, 800, 0.006, 9.0),
         ("granny flat builders melbourne", 2, 900, 0.002, 14.0), ("knockdown rebuild sydney", 1, 700, 0.001, 18.0), ("single storey home designs", 10, 5000, 0.002, 2.1),
         ("top builder", 90, 3000, 0.03, 2.0), ("home builder", 100, 3000, 0.05, 3.0), ("house builders", 130, 3000, 0.04, 4.0), ("new home", 250, 3000, 0.08, 1.0)]
    p = tmp_path / "g.xlsx"
    make_workbook(p, queries=q, pages=[("https://www.metricon.com.au/house-and-land/packages/melbourne", 5, 500, 0.01, 10.0)])
    r = gsc.import_workbook(seeded, p)
    seeded.commit()
    inventory.refresh_from_gsc(seeded)
    return r["import_id"]


def test_quick_wins_exclude_branded_and_already_good_queries(seeded, tmp_path):
    iid = seeded_gsc(seeded, tmp_path)
    qw = O.quick_wins(seeded, iid)
    qs = [x["query"] for x in qw]
    assert "granny flat builders melbourne" in qs and "display homes brisbane" in qs
    assert "metricon house and land" not in qs and "new home" not in qs  # branded / already top
    assert all(4 <= x["position"] < 20 for x in qw)


def test_low_ctr_flags_only_when_benchmark_exists_and_ctr_is_well_below_it(seeded, tmp_path):
    iid = seeded_gsc(seeded, tmp_path)
    low = O.ctr_underperformers(seeded, iid)
    assert [x["query"] for x in low] == ["single storey home designs"]  # position 2.1 with 0.2% CTR vs ~4% benchmark at position 2


def test_topic_visibility_reports_whether_a_page_already_exists(seeded, tmp_path):
    iid = seeded_gsc(seeded, tmp_path)
    tv = {x["query"]: x for x in O.topic_visibility(seeded, iid)}
    assert "house and land packages melbourne" not in tv  # ranks on page one: not a visibility problem
    assert tv["granny flat builders melbourne"]["topic_has_page"] is False


def test_content_gap_requires_demand_in_a_state_and_no_page_for_that_theme_and_state(seeded, tmp_path):
    iid = seeded_gsc(seeded, tmp_path)
    gaps = O.content_gaps(seeded, iid)
    ids = {g["id"] for g in gaps}
    assert any(i.endswith(":QLD") for i in ids)  # display homes brisbane, no QLD display page in the inventory
    mel = [g for g in gaps if g["state"] == "VIC" and g["theme_slug"] == "house-and-land"]
    assert not mel or mel[0]["page_count"] > 0  # a Melbourne packages page exists, so it is not a gap


def test_new_searches_carry_visibility_and_coverage_and_skip_rejected(seeded, tmp_path):
    iid = seeded_gsc(seeded, tmp_path)
    for q, st in [("granny flat builders", "proposed"), ("house and land packages melbourne", "proposed"), ("rent home", "rejected")]:
        seeded.add(RelatedQueryCandidate(seed="home builders", geo_key="AU", query=q, kind="rising", value=300, relevant=st != "rejected", review_status=st))
    seeded.commit()
    ns = {x["query"]: x for x in O.new_searches(seeded, iid)}
    assert set(ns) == {"granny flat builders", "house and land packages melbourne"}
    assert ns["granny flat builders"]["topic_has_page"] is False and ns["granny flat builders"]["metricon_visibility"]["matching_queries"] == 1
    assert ns["house and land packages melbourne"]["topic_has_page"] is True


def test_compute_without_gsc_says_so_instead_of_inventing(seeded):
    out = O.compute(seeded)
    assert out["gsc_available"] is False and out["quick_wins"] == [] and any("Search Console" in g for g in out["gaps"])
