# Live checks and remaining source limitations

Run on 12 October 2026 from the build environment (a cloud container; datacentre IP, no Anthropic or DataForSEO credentials).
Everything below was run against the real sources unless marked **not performed**.

## Summary

| Area | Result |
|---|---|
| GSC workbook import | **Done.** 60,911 query rows and 7,310 page rows imported in about 30 s; a repeat import was recognised as identical and changed nothing. |
| Theme mapping | **Done.** Rules map about 83% of query impressions to a theme and exclude about 0.2% as off-topic (rentals, decorating, jobs). The rest is generic head terms ("home", "house") and estate/suburb names; Claude batch classification is built but **not performed** (no key). |
| Offer collection, one real collector per state | **Done** for VIC (Carlisle, Arden, Senka, Mimosa), QLD (Brighton, Plantation), SA (Hickinbotham, Fairmont) and NSW (McDonald Jones). Extraction ran in **rules mode** because no Anthropic key was available, so conditions are mostly "not extracted" and every offer is flagged *needs review*. The AI path is covered by tests with a scripted model only; it has **not** been run against real pages. |
| Metricon's own offers | **Blocked.** All `latest-offers/*` and `terms-and-conditions` URLs return an AWS WAF challenge (HTTP 202, `x-amzn-waf-action: challenge`) to a plain client and a hard CloudFront 403 to a normal headless Chromium. The tool records this as `blocked` and does not try to defeat it. See "Needs a decision" below. |
| Competitor activity | **Done.** 41 design / display / package / estate index pages discovered from homepages and baselined (477 tracked items). A second pass raised no events, so baselines are stable. No genuine change events yet: that needs days of history. |
| Google Trends (DataForSEO) | **Not performed.** No credentials. The adapter was built from the provider's documentation (Explore Live, Locations) and tested against a mocked transport using the documented response shape. National/state/city support is therefore **unverified**; `trends-capability` is ready to run. |
| Macro indicators | **Done, live.** ABS Building Approvals (private houses, new work, original), ABS PPI output index for 3011 House construction (AU, NSW, VIC, QLD, SA) and the RBA cash-rate table (A2). Latest: approvals 2026-08, PPI 2026-Q2, cash rate change 2026-09-30. |
| News | **Done, live.** 25 queries, about 120 relevant new items after filtering. All are headline-only: Google News RSS links cannot be followed to the publisher page from here. |
| Weekly briefs | **Not performed live** (no key). Generation, evidence validation, failure handling and export are covered by tests with a scripted model. |
| Deployment | `Dockerfile` and `render.yaml` are written but **not built or deployed**: there is no Docker daemon here and no Render account. The same steps were checked by installing `requirements.txt` into a clean virtualenv, building the UI, and running the migrate/seed/serve sequence locally. |

## Source-by-source outcome

| Brand | Source | Outcome |
|---|---|---|
| Metricon | latest-offers (8 regional URLs) + terms | **Blocked** (WAF challenge). |
| Arden Homes | /offers + linked offer pages | OK |
| Carlisle Homes | /offers/ , /disclaimer/ | OK |
| Senka Homes | promotions page | OK |
| Mimosa Homes | M and V collection offer pages | OK |
| Boutique Homes | homepage and discovered pages | **Empty**: the site returns a 212-byte JavaScript shell. Needs `fetch_mode=browser` (Playwright) - implemented, not validated. |
| G.J. Gardner Homes | homepage | Fetched; **no offer pages found** (/offers 404). Franchise/area pages must be registered by an administrator. |
| McDonald Jones Homes | /offers + offer pages | OK |
| Clarendon Homes | homepage | Fetched; **no offer links found**. Register the right URL in Admin -> Sources. |
| Eden Brae Homes | homepage | **Failed** from here (connection reset / TLS handshake timeout). Retry from the deployed service. |
| Brighton Homes | /offers + terms | OK. The regional selector includes Tweed Coast (NSW), so state is never assumed from the page. |
| Plantation Homes | /promotions | OK |
| Fairmont Homes | current-offers | OK |
| Hickinbotham | promotions page | Fetched OK; the page carries almost no offer text (promotions appear to be images), so 1 low-confidence offer. |
| Weeks Homes | weekshomes.com.au | **Failed**: certificate not valid for that hostname. The correct domain needs confirming. |
| Rossdale Homes | homepage | **HTTP 403** from this environment. |

## Needs a decision (one blocker)

**Metricon's own offers are the only thing blocking the core comparison.** Pick one:

1. Ask Metricon IT to allowlist the crawler (a header agreed with them, set as `FETCH_EXTRA_HEADERS_JSON`, or the Render outbound IPs). Nothing else changes.
2. Have an administrator paste each regional offers page into Admin -> Sources -> "Paste page". It is stored as an immutable manual snapshot and runs through the same extraction and review.
3. Metricon supplies the offers as a feed or export.

Until then Metricon rows are absent and the Offers screen says so.

## Other limitations to know about

- **Rules-mode extraction is a stopgap.** It finds headlines, dollar figures with "up to"/"from", printed dates and obvious finance wording, and sends everything to review. Set `ANTHROPIC_API_KEY` and `CLAUDE_MODEL` for real condition extraction and terms/PDF analysis. Expect to review the first AI run.
- **Google News RSS** is licensed by Google for personal feed-reader use. Before wide rollout, replace it with a licensed news source (the collector is one module, `services/news.py`).
- **ABS seasonally adjusted/trend** slices for this series key returned no records, so approvals are original data (labelled, never mixed with adjusted data). Publication dates come from the ABS release page, not the data API.
- **Trends alias/topic IDs** are not used; aliases are plain search terms and start unvalidated.
- **City-level Trends** are untested; the UI only ever labels a series with the geography actually requested.
