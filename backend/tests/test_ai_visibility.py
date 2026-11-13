"""AI search visibility: import, recalculation from answer rows, cross-checks, slicing and API. The workbook here is a SYNTHETIC fixture in the real file's layout."""
import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.models import AiVisImport, AiVisRow
from app.services import ai_visibility as V

H = {"X-Requested-With": "metricon-ui"}
COLS = ["Row ID", "Keyword", "AI platform", "Query format", "Product / topic", "Region", "Journey stage", "Brand type", "Primary intent", "Volume", "Volume (capped)", "Primary keyword row", "Excluded",
        "Metricon mentioned", "Metricon cited", "GJ Gardner mentioned", "Carlisle mentioned", "Simonds mentioned", "Porter Davis mentioned", "Opportunity type", "Opportunity score",
        "Competitors named", "Metricon URLs cited", "Cited domains", "Updated", "First tracked brand in answer", "Recommendation intent"]
DEF, CIT, RIV, WHITE, NOT = "Defend – mentioned & cited", "Citation gap – named, not cited", "Competitor gap – rival named, Metricon absent", "Whitespace – no builder named", "Brand narrative – manage"


def row(i, kw, topic, region, m, c, gj=0, car=0, sim=0, opp=None, plat="Google AI Overviews", btype="Non-branded", vol=100, score=10.0, excluded=0, journey="Conversion", upd="2026-10-01", first=None, rec=0):
    opp = opp or (DEF if m and c else CIT if m else RIV if (gj or car or sim) else WHITE)
    if first is None:  # default: Metricon first when named, else the first rival named, else nobody
        first = "Metricon" if m else "G.J. Gardner Homes" if gj else "Carlisle Homes" if car else "Simonds Homes" if sim else "None"
    return [i, kw, plat, "Search keyword", topic, region, journey, btype, "Commercial", vol, vol, 1, excluded, m, c, gj, car, sim, 0, opp, score, "G.J. Gardner Homes" if gj else None, None, None, upd, first, rec]


def make_rows():
    r, i = [], 0

    def add(n, *a, **k):
        nonlocal i
        for _ in range(n):
            i += 1
            r.append(row(i, f"kw {i}", *a, **k))
    add(8, "House & land packages", "VIC", 1, 1)               # Metricon wins VIC H&L
    add(4, "House & land packages", "VIC", 1, 0)
    add(2, "House & land packages", "VIC", 0, 0, car=1)
    add(6, "House & land packages", "NSW", 0, 0, gj=1)         # GJ wins NSW
    add(6, "House & land packages", "NSW", 1, 1)
    add(2, "House & land packages", "NSW", 1, 1)
    add(12, "Display homes", "No-state", 0, 0)                 # replaced below
    add(3, "Display homes", "QLD", 1, 1)                       # low sample (3 < 10)
    add(12, "Interiors, inclusions & fit-out", "Not location-specific", 0, 0, score=40.0)  # awareness-tier whitespace
    add(10, "Home designs & floor plans", "Not location-specific", 0, 0, gj=1, score=30.0)
    add(4, "Home designs & floor plans", "WA", 0, 0, gj=1)    # out of market
    add(5, "Home designs & floor plans", "VIC", 1, 1, btype="Metricon branded")
    add(2, "Stadium", "VIC", 0, 0, excluded=1, opp="Exclude / off-topic")
    for x in r:
        if x[5] == "No-state":
            x[5] = "National"
    return r


def make_workbook(path, rows=None, kpi_named=None, with_summary=True):
    rows = rows or make_rows()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(COLS)
    for r in rows:
        ws.append(r)
    ins = wb.create_sheet("Insights_Next_Steps")
    ins["A1"], ins["A2"] = "Insights", "DRAFT for internal review. Source: Ahrefs Brand Radar export (synthetic)."
    on = [r for r in rows if not r[12]]
    nb = [r for r in on if r[7] == "Non-branded"]
    ins.append([])
    ins["A4"] = "Headline KPIs"
    ins["A5"], ins["D5"] = "On-topic AI answers analysed", len(on)
    ins["A6"], ins["D6"] = "Metricon named – non-branded queries", kpi_named if kpi_named is not None else sum(r[13] for r in nb) / len(nb)
    ins["A15"], ins["B15"], ins["C15"], ins["D15"] = "Priority", "Theme", "Segment", "Finding (verify figures)"
    ins["A16"], ins["B16"], ins["C16"], ins["D16"] = "P1", "Citations", "All", "Metricon is named more often than it is cited."
    ass = wb.create_sheet("Assumptions")
    ass["B6"], ass["C6"] = "Minimum rows for a segment \"winner\" to be called", 10
    for n, (t, w) in enumerate([("House & land packages", 1.0), ("Display homes", 1.0), ("Home designs & floor plans", 1.0), ("Interiors, inclusions & fit-out", 0.5)]):
        ass.cell(row=9 + n, column=8, value=t)
        ass.cell(row=9 + n, column=9, value=w)
    top = wb.create_sheet("Top_Opportunities")
    top.append(["Top 200"]); top.append(["note"]); top.append([])
    top.append(["Rank", "Keyword", "Product / topic", "Region", "Journey stage", "Brand type", "Search volume", "Opportunity score", "Dominant gap type", "Metricon mention rate", "Competitors named",
                "AI platforms tracked", "Suggested action"])
    top.append([1, "kw 1", "Home designs & floor plans", "Not location-specific", "Conversion", "Non-branded", 700, 76.9, RIV, 0, "G.J. Gardner Homes", "Google AI Overviews", "Build a page"])
    if with_summary:
        ss = wb.create_sheet("Segment_Summary")
        ss.append(["Segment summary"]); ss.append(["note"]); ss.append([])
        ss.append(["Dimension", "Segment", "Rows", "Vol", "Metricon mention %"])
        for t in ("House & land packages", "Display homes", "Home designs & floor plans", "Interiors, inclusions & fit-out"):
            rs = [r for r in on if r[4] == t]
            if rs:
                ss.append(["Product / topic", t, len(rs), 0, sum(r[13] for r in rs) / len(rs)])
    wb.save(path)


@pytest.fixture
def imp(seeded, tmp_path):
    p = tmp_path / "v.xlsx"
    make_workbook(p)
    r = V.import_workbook(seeded, p, "v.xlsx")
    seeded.commit()
    return seeded, seeded.get(AiVisImport, r["import_id"]), r


def test_import_keeps_every_row_flags_off_topic_and_passes_its_cross_checks(imp):
    db, i, r = imp
    assert r["created"] and i.row_count == 76 - 0 and db.scalar(select(func.count()).select_from(AiVisRow)) == i.row_count
    assert all(c["ok"] for c in i.checks), i.checks
    assert i.source_note.startswith("DRAFT") and i.is_current and str(i.period_start) == "2026-10-01" and i.min_rows == 10
    assert i.insights[0]["Priority"] == "P1" and len(i.opportunities) == 1


def test_importing_the_same_file_again_changes_nothing(seeded, tmp_path):
    p = tmp_path / "v.xlsx"
    make_workbook(p)
    a = V.import_workbook(seeded, p, "v.xlsx")
    seeded.commit()
    b = V.import_workbook(seeded, p, "again.xlsx")
    assert not b["created"] and b["import_id"] == a["import_id"] and seeded.scalar(select(func.count()).select_from(AiVisImport)) == 1


def test_a_workbook_whose_published_figure_disagrees_is_flagged_not_trusted(seeded, tmp_path):
    p = tmp_path / "bad.xlsx"
    make_workbook(p, kpi_named=0.99)
    r = V.import_workbook(seeded, p)
    bad = [c for c in r["checks"] if not c["ok"]]
    assert bad and "Metricon named" in bad[0]["check"]


def test_a_file_that_is_not_the_workbook_is_refused_with_a_reason(seeded, tmp_path):
    p = tmp_path / "x.xlsx"
    wb = openpyxl.Workbook(); wb.active.title = "Other"; wb.save(p)
    with pytest.raises(ValueError, match="no 'Data' sheet"):
        V.import_workbook(seeded, p)
    wb = openpyxl.Workbook(); wb.active.title = "Data"; wb.active.append(["Row ID", "Keyword"]); wb.save(p)
    with pytest.raises(ValueError, match="missing columns"):
        V.import_workbook(seeded, p)


def test_heatmap_cells_are_recalculated_from_rows_with_winner_lead_and_sample_flag(imp):
    db, i, _ = imp
    o = V.overview(db, i)
    hl = next(g for g in o["grid"] if g["topic"] == "House & land packages")
    vic, nsw = hl["cells"]["VIC"], hl["cells"]["NSW"]
    assert vic["n"] == 14 and round(vic["mention"], 3) == round(12 / 14, 3) and round(vic["cited"], 3) == round(8 / 14, 3) and vic["winner"] == "Metricon" and not vic["low_sample"]
    assert nsw["winner"] == "Metricon" and nsw["n"] == 14  # 8 named vs 6 GJ
    assert nsw["lead_pts"] == round((8 / 14 - 6 / 14) * 100, 1)
    disp = next(g for g in o["grid"] if g["topic"] == "Display homes")
    assert disp["cells"]["QLD"]["low_sample"] is True and disp["cells"]["QLD"]["n"] == 3  # shown, but never called a win
    assert o["columns"] == ["VIC", "NSW", "QLD", "SA", "No state named"] and "WA" not in o["columns"]
    des = next(g for g in o["grid"] if g["topic"] == "Home designs & floor plans")
    assert des["outside_markets"] == 4 and o["outside_markets"] == {"WA": 4}
    assert des["cells"]["No state named"]["winner"] == "G.J. Gardner"


def test_non_branded_is_the_default_and_branded_rows_only_appear_when_asked(imp):
    db, i, _ = imp
    assert V.overview(db, i)["kpi"]["answers"] == 76 - 2 - 5  # off-topic and Metricon-branded rows are out
    assert V.overview(db, i, "all")["kpi"]["answers"] == 76 - 2
    assert V.overview(db, i, "metricon_branded")["kpi"]["answers"] == 5


def test_platform_filter_narrows_the_answers(seeded, tmp_path):
    rows = make_rows()
    for r in rows[:10]:
        r[2] = "ChatGPT"
    p = tmp_path / "p.xlsx"
    make_workbook(p, rows)
    i = seeded.get(AiVisImport, V.import_workbook(seeded, p)["import_id"])
    seeded.commit()
    assert V.overview(seeded, i, platform="ChatGPT")["kpi"]["answers"] == 10
    assert {x["platform"] for x in V.overview(seeded, i)["platforms"]} == {"ChatGPT", "Google AI Overviews"}


def test_outcomes_partition_the_answers_and_use_the_workbooks_labels(imp):
    db, i, _ = imp
    k = V.overview(db, i)["kpi"]
    assert sum(k["outcomes"].values()) == k["answers"]
    assert k["outcomes"]["rival_only"] == 2 + 6 + 10 + 4 and k["outcomes"]["nobody"] == 12 + 12  # Carlisle VIC, GJ NSW, GJ designs, GJ WA (outside markets still count in the totals)
    assert k["outcomes"]["won"] > 0 and k["outcomes"]["named_not_cited"] == 4


def test_lift_pockets_rank_by_score_and_keep_awareness_topics_apart(imp):
    db, i, _ = imp
    o = V.overview(db, i)
    assert o["pockets"][0]["topic"] == "Home designs & floor plans" and o["pockets"][0]["region"] == "No state named"
    assert o["pockets"][0]["dominant_gap"] == "rival_only" and o["pockets"][0]["winner"] == "G.J. Gardner"
    assert all(p["tier"] != "awareness" for p in o["pockets"])
    assert [p["topic"] for p in o["awareness_pockets"]] == ["Interiors, inclusions & fit-out"]
    assert all(p["n"] >= i.min_rows for p in o["pockets"])  # thin segments are not ranked


def test_keyword_drill_down_filters_and_orders_by_score(imp):
    db, i, _ = imp
    ks = V.keywords(db, i, "Home designs & floor plans", "No state named", "rival_only")
    assert len(ks) == 10 and ks[0]["score"] == 30.0 and ks[0]["rivals"] == ["G.J. Gardner Homes"]
    assert V.keywords(db, i, "Display homes", "QLD", "rival_only") == []


@pytest.fixture
def open_client(seeded, monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "false")
    get_settings.cache_clear()
    from app.main import create_app

    yield TestClient(create_app())
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    get_settings.cache_clear()


def test_api_says_nothing_imported_then_serves_the_overview(open_client, seeded, tmp_path):
    assert open_client.get("/api/ai-visibility").json() == {"imported": False}
    p = tmp_path / "v.xlsx"
    make_workbook(p)
    V.import_workbook(seeded, p, "v.xlsx")
    seeded.commit()
    j = open_client.get("/api/ai-visibility").json()
    assert j["imported"] and j["overview"]["kpi"]["answers"] == 69 and "Google AI Overviews" in j["platform_options"] and j["insights"][0]["Priority"] == "P1"
    assert open_client.get("/api/ai-visibility?brand_type=nonsense").json()["overview"]["filters"]["brand_type"] == "non_branded"
    ks = open_client.get("/api/ai-visibility/keywords?topic=Home designs %26 floor plans&region=No state named&outcome=rival_only").json()
    assert len(ks) == 10


def test_upload_refuses_anything_but_xlsx(open_client):
    r = open_client.post("/api/admin/ai-visibility/import", files={"file": ("x.csv", b"a,b", "text/csv")}, headers=H)
    assert r.status_code == 400


def test_city_is_read_from_the_search_wording_and_ambiguous_searches_are_left_out():
    assert V.city_of("House designs Melbourne") == "Melbourne" and V.city_of("home builders gold coast") == "Gold Coast"
    assert V.city_of("builders on the Sunshine  Coast") == "Sunshine Coast"
    assert V.city_of("melbourne to sydney builders") is None  # two cities: not counted for either
    assert V.city_of("house designs") is None and V.city_of("melbournes") is None and V.city_of(None) is None


def city_rows():
    r, i = [], 0

    def add(n, kw, topic, region, m, c, **k):
        nonlocal i
        for _ in range(n):
            i += 1
            r.append(row(i, kw, topic, region, m, c, **k))
    add(12, "house designs melbourne", "Home designs & floor plans", "VIC", 1, 1)
    add(4, "display homes melbourne", "Display homes", "VIC", 0, 0, car=1)
    add(11, "house designs sydney", "Home designs & floor plans", "NSW", 0, 0, gj=1)
    add(3, "house designs sydney", "Home designs & floor plans", "NSW", 1, 0)
    add(11, "house and land gold coast", "House & land packages", "QLD", 1, 0)
    add(5, "house designs brisbane", "Home designs & floor plans", "QLD", 0, 0)
    add(6, "house designs victoria", "Home designs & floor plans", "VIC", 1, 1)  # a state but no city
    add(2, "melbourne metricon", "Home designs & floor plans", "VIC", 1, 1, btype="Metricon branded")
    return r


def test_overview_breaks_answers_down_by_city_in_the_order_requested(seeded, tmp_path):
    p = tmp_path / "c.xlsx"
    make_workbook(p, city_rows())
    i = seeded.get(AiVisImport, V.import_workbook(seeded, p)["import_id"])
    seeded.commit()
    o = V.overview(seeded, i)
    assert [c["city"] for c in o["by_city"]] == ["Melbourne", "Sydney", "Adelaide", "Gold Coast", "Brisbane", "Sunshine Coast"]
    by = {c["city"]: c for c in o["by_city"]}
    assert by["Melbourne"]["n"] == 16 and by["Melbourne"]["state"] == "VIC" and round(by["Melbourne"]["mention"], 3) == round(12 / 16, 3) and by["Melbourne"]["winner"] == "Metricon"
    assert by["Sydney"]["n"] == 14 and by["Sydney"]["winner"] == "G.J. Gardner" and by["Sydney"]["outcomes"]["rival_only"] == 11
    assert by["Gold Coast"]["outcomes"]["named_not_cited"] == 11
    assert by["Adelaide"]["n"] == 0 and by["Sunshine Coast"]["n"] == 0  # present, but no answers: shown as such, never zero-filled
    assert by["Brisbane"]["low_sample"] is True  # 5 answers


def test_city_grid_uses_only_searches_that_name_a_city_and_state_grid_is_unchanged(seeded, tmp_path):
    p = tmp_path / "c.xlsx"
    make_workbook(p, city_rows())
    i = seeded.get(AiVisImport, V.import_workbook(seeded, p)["import_id"])
    seeded.commit()
    o = V.overview(seeded, i)
    des = next(g for g in o["city_grid"] if g["topic"] == "Home designs & floor plans")
    assert des["cells"]["Melbourne"]["n"] == 12 and des["cells"]["Sydney"]["n"] == 14 and des["all"]["n"] == 12 + 14 + 5  # 'victoria' (no city) is not in the city view
    sd = next(g for g in o["grid"] if g["topic"] == "Home designs & floor plans")
    assert sd["cells"]["VIC"]["n"] == 12 + 6  # ...but it is in the state view
    assert [c["city"] for c in o["city_columns"]] == [c["city"] for c in o["by_city"]] and "city_note" in o


def test_keywords_can_be_drilled_by_city(seeded, tmp_path):
    p = tmp_path / "c.xlsx"
    make_workbook(p, city_rows())
    i = seeded.get(AiVisImport, V.import_workbook(seeded, p)["import_id"])
    seeded.commit()
    ks = V.keywords(seeded, i, "Home designs & floor plans", None, None, city="Sydney")
    assert [k["keyword"] for k in ks] == ["house designs sydney"] and ks[0]["platforms"] == 14
    assert V.keywords(seeded, i, None, None, None, city="Brisbane")[0]["keyword"] == "house designs brisbane"


def strength_rows():
    r, i = [], 0

    def add(n, topic, region, m, c, first=None, rec=0, gj=0, car=0, sim=0):
        nonlocal i
        for _ in range(n):
            i += 1
            r.append(row(i, f"kw {i}", topic, region, m, c, gj=gj, car=car, sim=sim, first=first, rec=rec))
    # Home designs: Metricon named in 20 answers, first in 15; GJ named in 12 (first in 5 of its own... Metricon first in the shared ones)
    add(15, "Home designs & floor plans", "VIC", 1, 1, first="Metricon", gj=1)
    add(5, "Home designs & floor plans", "VIC", 1, 0, first="G.J. Gardner Homes", gj=1)       # both named, GJ listed first
    add(4, "Home designs & floor plans", "VIC", 0, 0, first="Carlisle Homes", car=1)
    add(12, "Display homes", "QLD", 1, 1, first="Metricon", rec=1)                           # recommendation-style searches
    add(2, "Display homes", "QLD", 0, 0, first="Simonds Homes", sim=1, rec=1)
    return r


def make_strength(seeded, tmp_path):
    p = tmp_path / "s.xlsx"
    make_workbook(p, strength_rows())
    i = seeded.get(AiVisImport, V.import_workbook(seeded, p)["import_id"])
    seeded.commit()
    return i


def test_first_named_brand_is_normalised_to_the_five_tracked_names():
    assert V.canonical_first("G.J. Gardner Homes") == "G.J. Gardner" and V.canonical_first("Carlisle Homes") == "Carlisle" and V.canonical_first("Metricon") == "Metricon"
    assert V.canonical_first("Simonds Homes") == "Simonds" and V.canonical_first("Porter Davis Homes") == "Porter Davis"
    assert V.canonical_first("None") is None and V.canonical_first(None) is None and V.canonical_first("Henley") is None


def test_strength_table_scores_each_brand_per_category_from_position_not_sentiment(seeded, tmp_path):
    i = make_strength(seeded, tmp_path)
    st = V.overview(seeded, i)["strength"]
    assert st["available"] and "no answer text" in st["note"]
    t = st["topic"]
    assert [c["key"] for c in t["columns"]] == ["Home designs & floor plans", "Display homes"]
    me = next(b for b in t["brands"] if b["brand"] == "Metricon")
    d = me["cells"]["Home designs & floor plans"]
    assert d["n"] == 24 and round(d["named"], 3) == round(20 / 24, 3) and round(d["first"], 3) == round(15 / 24, 3) and round(d["first_when_named"], 3) == 0.75
    gj = next(b for b in t["brands"] if b["brand"] == "G.J. Gardner")["cells"]["Home designs & floor plans"]
    assert round(gj["named"], 3) == round(20 / 24, 3) and round(gj["first"], 3) == round(5 / 24, 3)  # named as often as Metricon but listed first far less
    assert t["leaders"]["Home designs & floor plans"] == "Metricon"


def test_who_to_choose_figure_uses_only_recommendation_style_searches_and_hides_thin_samples(seeded, tmp_path):
    i = make_strength(seeded, tmp_path)
    t = V.overview(seeded, i)["strength"]["topic"]
    me = next(b for b in t["brands"] if b["brand"] == "Metricon")
    disp = me["cells"]["Display homes"]
    assert disp["rec_n"] == 14 and round(disp["rec_first"], 3) == round(12 / 14, 3)
    assert me["cells"]["Home designs & floor plans"]["rec_first"] is None and me["cells"]["Home designs & floor plans"]["rec_n"] == 0  # no recommendation-style searches there: no number, not a zero
    assert next(b for b in t["brands"] if b["brand"] == "Porter Davis")["cells"]["Display homes"]["first"] == 0  # tracked, never first: a real zero


def test_strength_by_state_and_city_use_the_same_logic(seeded, tmp_path):
    i = make_strength(seeded, tmp_path)
    st = V.overview(seeded, i)["strength"]
    assert [c["key"] for c in st["state"]["columns"]] == ["VIC", "NSW", "QLD", "SA"] and [c["key"] for c in st["city"]["columns"]][:2] == ["Melbourne", "Sydney"]
    me = next(b for b in st["state"]["brands"] if b["brand"] == "Metricon")
    assert me["cells"]["VIC"]["n"] == 24 and me["cells"]["NSW"] == {"n": 0}


def test_an_older_import_asks_for_a_reupload_and_a_reupload_of_the_same_file_fills_the_new_columns(seeded, tmp_path):
    p = tmp_path / "s.xlsx"
    make_workbook(p, strength_rows())
    a = V.import_workbook(seeded, p)
    imp = seeded.get(AiVisImport, a["import_id"])
    imp.schema_version = 1
    seeded.execute(AiVisRow.__table__.update().values(first_brand=None, recommendation_intent=False))
    seeded.commit()
    seeded.refresh(imp)
    assert V.overview(seeded, imp)["strength"]["available"] is False
    again = V.import_workbook(seeded, p)
    seeded.commit()
    assert again["upgraded"] and again["import_id"] == a["import_id"] and seeded.scalar(select(func.count()).select_from(AiVisImport)) == 1
    seeded.refresh(imp)
    assert V.overview(seeded, imp)["strength"]["available"] is True and seeded.scalar(select(func.count()).select_from(AiVisRow)) == 24 + 14
    assert not V.import_workbook(seeded, p)["created"]  # and now it is a plain no-op
