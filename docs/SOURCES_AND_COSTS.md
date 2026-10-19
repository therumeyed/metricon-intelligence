# Source coverage and recurring-cost estimate

Based on what the pilot actually did on 12 Oct 2026. Where a number is a list price or an assumption it says so.
Nothing here was measured for DataForSEO or Anthropic spend: neither was connected.

## Coverage today

| Module | Sources | Cadence | Notes |
|---|---|---|---|
| Offers | 31 offer pages (listing + discovered detail) across 9 of 16 brands | daily | Metricon blocked; 6 other brands need admin attention (see LIVE_CHECKS.md) |
| Competitor items | 41 index pages, 477 tracked items, 10 brands | daily | link-based identity; baseline only until history builds |
| Macro | 15 ABS series (house approvals, houses commenced and PPI output index for AU, NSW, VIC, QLD, SA) + the RBA cash rate and two RBA home loan rates | daily check | ABS and RBA are free |
| Google results | per-brand offers and news searches (about 65) | daily | DataForSEO SERP, see below |
| Site inventory | metricon.com.au sitemap + GSC pages | weekly | free; the sitemap is publicly fetchable |
| News | 25 queries | daily | free; headline only |
| Trends | 61 plans (6 brand groups, 55 theme groups); 8 active as the pilot | weekly | not run - no credentials |
| GSC | 1 import, replaced only on upload | on upload | |

## Recurring cost

### DataForSEO (Google Trends Explore, Live)
List price on the public pricing page: **$0.011 per task** (up to 5 keywords). Cost is recorded two ways: the provider's reported `cost` on each response, and our configured estimate (`DATAFORSEO_EST_COST_PER_EXPLORE`). Limits stop requests before they are sent.

| Scenario | Requests / week | Approx. per month (4.33 weeks) |
|---|---|---|
| Pilot (8 plans) | 8 | **$0.38** |
| All 61 plans active | 61 | **$2.90** |
| + weekly related-query discovery (5 seeds) | +5 | +$0.24 |
| One-off capability test (5 geographies + 4 cities) | 9 | $0.10 once |

Re-fetching 24 months weekly is the same price as 12 months, so history depth is free. Stored responses answer every page view; readers never trigger a paid call. Standard (queued) mode is $0.0027 per task and could cut this by about 75% if a 45-minute delay is acceptable; the adapter uses Live today.

### DataForSEO SERP (Google results for offers and news)
List price about **$0.002 per live SERP of 10 results** (stored estimate `DATAFORSEO_EST_COST_PER_SERP`; the provider's reported cost is stored beside it). Standing searches the app creates: per brand "{brand} offers" and "{brand} current promotions", "{brand} offers {state}" for brands that trade in several states, and one Google News search per brand - roughly 60 to 70 searches a day.

| Scenario | Searches / day | Approx. per month |
|---|---|---|
| Standing set (about 65 searches, daily) | 65 | **about $3.90** |
| + 10 extra admin-added searches | 75 | about $4.50 |

`site:` and `intitle:` operators cost several times more and are refused. A per-job cap (`SERP_MAX_REQUESTS_PER_JOB`, 150) and the daily/monthly DataForSEO limits stop collection before a request is sent. Weekly rising-search discovery now covers every theme (about 18 Trends requests a week, about $0.20, about $0.85 a month).

### Anthropic (token volumes only)
No model is configured, so no price is assumed. Volumes from the pilot pages:

| Task | Volume | Frequency |
|---|---|---|
| Offer extraction, first baseline | 31 pages, about 14,000 page tokens + prompts, roughly 45,000 input and 50,000 output tokens in total | once |
| Offer extraction, steady state | only pages whose promotional text changed (unchanged pages cost nothing); assume 10% of pages a day, about 5,000 input tokens | daily |
| Terms / PDF analysis | up to about 7,500 input tokens per terms document, once per change | on change |
| Google-results offer reading | one call per search whose results changed (snippets only, a few hundred tokens each) | daily |
| News judging | one call per brand with new headlines, about 3,000 input tokens | daily |
| AI analysis | 5 calls (national + 4 states), about 40,000 input (facts plus the page inventory) and 4,000 output tokens each | weekly |
| Weekly briefs | 5 calls (national + 4 states), about 10,000 input and 3,000 output tokens each | weekly |
| GSC batch classification | 300 queries in 3 calls, about 2,500 tokens each | optional, once per import |

Hard stops: `ANTHROPIC_MAX_CALLS_PER_JOB` (60) and `ANTHROPIC_DAILY_CALL_LIMIT` (300). Multiply the token volumes by your model's price to get a monthly figure. Expect it to be small next to the hosting.

### Hosting (Render list plans, check current pricing)
Web service 0.5c-512mb, cron 0.5c-512mb and Postgres 0.1c-256mb as written in `render.yaml`. Postgres grows with evidence: the pilot stored 204 snapshots in about 0.34 MB of clean text and 3.5 MB of compressed raw HTML (bodies are stored only when the denoised content changes). Assume up to roughly 100 MB a year at full roster plus PDFs; move up a Postgres plan before 256 MB fills. Cron jobs have no persistent disk, which is why all evidence lives in Postgres.

## Assumptions
- Offer pages change rarely; "changed" means the promotional text changed, after nav/footer/clock noise is removed.
- 12 hour limit per Render cron run is far above the observed run time (a full collection of 83 sources took a few minutes at a 2 s per-host delay).
- Prices move; treat the DataForSEO and Render figures as list prices and re-check before committing budget.
