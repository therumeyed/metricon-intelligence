# Metricon Market Intelligence

A tool for Metricon's marketing and sales teams (open access by default; see Access). It collects real data, keeps the evidence, and explains what changed in buyer demand, competitor offers, competitor activity and the wider home-building market.

**Five views:** Market brief · Demand trends · Offers in market · Competitor activity · Market conditions. A separate Admin area holds configuration, review and job controls. State selector (Australia / VIC / NSW / QLD / SA) sits in the main navigation.

Status, live-check results and open blockers: [`docs/LIVE_CHECKS.md`](docs/LIVE_CHECKS.md). Source coverage and running costs: [`docs/SOURCES_AND_COSTS.md`](docs/SOURCES_AND_COSTS.md).

## What it does

| Area | How |
|---|---|
| Offers | Two routes. (1) Daily fetch of official promotion pages, linked offer pages and terms/PDFs where a site allows it. (2) **Google results via DataForSEO SERP** ("Metricon offers", "Carlisle Homes current promotions", state variants for multi-state brands): title, URL and snippet of the brand's own-site results are stored as a snapshot and read by Claude. Offers found only this way are tagged *From Google result* and are never read as withdrawn when a result stops appearing. Every quote, amount and date is verified against the stored text; changes become versions with field-level diffs. Failed fetches mark an offer *stale*; they never withdraw it. |
| Competitor activity | Google News results per brand (DataForSEO SERP News) plus any RSS/Atom feeds you add in Admin, dated by the publisher. Claude (when connected) judges each headline - is it about the builder, what kind of news, a one-sentence summary that must pass a quote and number check; without it, plain headlines are recorded. Design / display / package pages are also baselined where crawlable. One story = one event across channels. |
| Demand | DataForSEO's **Google Trends** Explore API (not the separate DataForSEO Trends product). Comparable groups of up to five brands per geography; period changes use only complete weeks from the same series. |
| GSC | The Search Console workbook seeds themes and representative keywords. Query rows and page rows stay separate. |
| Macro | ABS Building Approvals (private houses), ABS Building Activity (private houses **commenced**), ABS PPI house-construction **output** index, RBA cash rate target and RBA home loan rates (table F5). Market conditions reads them with transparent rules (shown on screen), compares states, lists release dates and groups news by topic. |
| Site inventory | Every page known to exist on metricon.com.au: the public sitemap plus pages seen in Search Console. Recommendations are tested against it. |
| AI analysis | A separate tab. Computed facts (GSC quick wins, low-CTR queries, state content gaps, rising searches from Google Trends across all themes, trend momentum, offers, competitor events, macro) with IDs go to Claude, which writes what is new in the market and what to do. Code then verifies: cited facts must exist, quoted numbers must be in them, a "create a page" idea that matches an existing page is downgraded to "improve that page", and page URLs must exist in the inventory. The computed opportunity tables work without AI. |
| News | Google News RSS for brand, theme and state queries; deduplicated and filtered; headline-only is labelled. |
| Briefs | Weekly national + state briefs from computed facts with evidence IDs. Anything unsupported is dropped; if the model fails the previous brief stays and nothing is made up. |

Design rules that matter are in the code comments of `backend/app/services/` (offers pipeline, trends metrics, briefs). The ones people ask about:

- A claimed inclusion value is not a cash saving; a deposit arrangement is not a discount; a government grant is not builder funded. "Up to" and "from" survive.
- An offer is never assumed to apply everywhere. Geography is "stated in the text", "from the page it was listed on", or "not stated".
- Trends values are relative indices. A statewide series is labelled statewide, a national one national. Separate comparison groups are never ranked against each other.
- Missing data is never zero. Zero or tiny baselines show "Insufficient search data" instead of a percentage.
- Human corrections are kept in their own table, re-applied after every refresh, and audited.

## Stack

Python 3.13, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 · React 19, TypeScript, Vite, Recharts · Docker · Render (web + cron + Postgres).

## Run locally

```bash
# 1. PostgreSQL 16 running locally, then:
cd backend
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example .env            # edit DATABASE_URL, SESSION_SECRET, SEED_ADMIN_*; leave API keys blank to see "not connected"
python -m app.cli migrate
python -m app.cli seed             # 16 brands, 22 brand/state assignments, themes, source registry
python -m app.cli create-user you@example.com --role admin
python -m app.cli import-gsc /path/to/Metricon_GSC_Data.xlsx --period-start 2026-01-01 --period-end 2026-10-10
python -m app.cli daily            # collect sources -> extract offers -> events -> news -> macro
uvicorn app.main:app --reload --port 8000

# 2. UI (separate terminal)
cd frontend && npm ci && npm run dev    # http://localhost:5173, proxies /api to :8000
```

Tests need a database: `createdb metricon_test` then `pytest` (set `TEST_DATABASE_URL` if yours differs). The suite builds the schema from the Alembic migration and uses synthetic fixtures only; none of that data can reach the app database.

## Commands

| Command | What it does |
|---|---|
| `python -m app.cli migrate` / `seed` | Apply migrations / add missing seed rows (never overwrites admin edits) |
| `create-user EMAIL --role admin\|viewer` | Create an account (there is no public registration) |
| `import-gsc FILE --period-start --period-end` | Idempotent GSC import + theme mapping + representative terms |
| `run JOB [--params JSON]` | Run one job: `collect_sources`, `collect_search`, `extract_offers`, `detect_events`, `analyse_activity`, `collect_news`, `check_macro`, `refresh_site_inventory`, `refresh_trends`, `discover_related`, `generate_briefs`, `generate_analysis`, `trends_capability`, `classify_gsc` |
| `daily` / `weekly` | Run that pipeline now, in order; each step isolated |
| `dispatch` | What Render's cron runs hourly: runs whatever is due in Australia/Sydney time |
| `trends-capability [--cities]` | Real provider test per geography; records supported / unsupported |
| `jobs` | Recent job runs |

The same actions are buttons in Admin -> Jobs, with retry. Jobs are database-locked (no overlap), checkpointed, and bounded by timeouts, per-host delays and spend/call limits.

### Schedules
Render cron is UTC only. The cron fires **every hour** and `dispatch` decides what is due using Australia/Sydney time, so daylight saving never moves a schedule. Defaults (Sydney): sources, Google results, offers, events and news analysis 05:00 daily; news and macro 06:00 daily; page inventory Monday 03:00; Trends refresh and discovery Monday 04:00; briefs and AI analysis Monday 08:00. A job that failed is retried at most 3 times per slot; use Retry after that.

## Deploy to Render

1. Push this repo to GitHub.
2. In Render: **New -> Blueprint**, pick the repo. It reads `render.yaml` and creates the web service, an hourly cron service and Postgres.
3. When prompted, enter the `sync: false` secrets. Leave `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `DATAFORSEO_*` blank to start; those modules show "not connected" until set (on **both** the web and cron services). `CLAUDE_MODEL` must be a model id you have confirmed works with your key; none is assumed.
4. Login is off by default (see Access). Only if you set `REQUIRE_LOGIN=true`: set `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` for the first administrator, sign in, create other users, then clear the password variable.
5. In Admin: upload the GSC workbook (Search data), review Sources, run **Trends -> capability test** once credentials are in, and switch on more Trends plans as budget allows.
6. Optional Playwright rendering for JavaScript-only sites: build with `--build-arg INSTALL_BROWSER=1`, set `ALLOW_BROWSER_FETCH=true` and flag the source `fetch_mode=browser`. It reports bot challenges; it never tries to defeat them.

## If a module says "not connected"

The web service and the hourly cron service are separate processes with **separate environment variables**. Set these four on **both** (Render → service → Environment):

`ANTHROPIC_API_KEY`, `CLAUDE_MODEL` (e.g. `claude-sonnet-5-5`), `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`

Notes: Blueprint variables marked `sync: false` are only prompted for at first creation; if they were blank then, they stay blank until you edit them in the dashboard (saving redeploys the service). Leading/trailing spaces are ignored. Then open **Admin → Jobs → "Test Anthropic and DataForSEO now"**: it makes one tiny real call to each provider and shows the provider's own error, which variables the web service can see, and what the last scheduled (cron) run saw.

After connecting, the schedules wait for their next slot. To populate straight away, run these under **Admin → Jobs** in order: `trends capability` (once), `refresh trends`, `discover related`, `collect search` then `extract offers` (reads the Google results; also re-reads existing offers with the model; the change is treated as a re-baseline, not as news), `analyse activity`, `refresh site inventory`, `check macro`, `generate briefs`, `generate analysis` (re-reads existing offers with the model; the change is treated as a re-baseline, not as news), `generate briefs`. Under **Admin → Trends & discovery**, "Activate all plans" turns on NSW, QLD and SA; only the 8 pilot plans (Australia and Victoria) are active by default.

## Administering sources and the roster

Everything is data, not code (Admin area):

- **Brands & regions:** edit names, domains, state assignments, aliases. New brands need no deploy.
- **Sources:** add or disable pages, trigger a fetch, see last outcome and error. **Add search** creates a standing Google search for a brand (offers or news; about $0.002 per search per day; `site:` operators are refused because they cost several times more). **News feeds** takes any RSS/Atom URL (for example a Google Alerts feed covering every brand); it is test-read before it is saved. Pages that can't be crawled can be supplied with **Paste page**; they become manual snapshots and go through the same extraction.
- **Offer review:** open an offer -> *Administrator: correct a field*, or set its status. Machine extraction and evidence stay; your value overrides what readers see; "Return status to automatic" hands control back.
- **Themes & keywords:** rules, representative keywords (only *tracked* ones go to paid APIs), review of proposed subthemes.
- **Limits & spend:** daily/monthly DataForSEO limits, request and call caps. Reported and estimated cost are shown separately.

## Backups and recovery

All state is in PostgreSQL (records, evidence, corrections, audit, job history); the containers are disposable.

- Use a Render Postgres plan with automated backups, and take a manual dump before upgrades: `pg_dump --format=custom "$DATABASE_URL" > metricon-intel-$(date +%F).dump`.
- Restore to a new database: `pg_restore --no-owner --clean --dbname "$NEW_DATABASE_URL" metricon-intel-YYYY-MM-DD.dump`, point the services at it, redeploy. The start script runs `migrate` and `seed`, both safe on existing data.
- A deploy or restart loses nothing: no local files are required, and an interrupted job resumes from its checkpoint or is retried.
- Snapshots are never edited after they are written; template-only fetches are stored as a reference to the earlier body, which keeps growth small.

## Access

**Login is off by default.** Anyone who can reach the URL sees everything and has full administrator rights: they can change the roster, correct offers, upload data and start jobs (including ones that spend DataForSEO and Anthropic credits). Only the spend limits in Admin → Limits cap that. Keep the URL private, or put the service behind something that restricts who can reach it.

To bring sign-in back, set `REQUIRE_LOGIN=true` on the web service. That turns on admin-created accounts, bcrypt password hashes, signed session cookies and viewer / administrator roles, and the API, exports and admin routes then require a session. Set `SESSION_SECRET` (Render generates one) and create the first admin with `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` or `python -m app.cli create-user`. A CSRF header is required on every state-changing request in both modes. Actions taken in open mode are recorded in the audit log without a user name.

## Repository map

```
backend/app/         FastAPI app, models, services (offers, trends, macro, news, briefs), CLI, jobs
backend/migrations/  Alembic (one initial migration)
backend/seed/        brands, themes, source registry (editable YAML; creates missing rows only)
backend/tests/       behaviour tests against real PostgreSQL
frontend/src/        React app: views/ (five readers + Admin), ui.tsx (charts, tables, panels)
Dockerfile, render.yaml, scripts/start.sh, .env.example, docs/
```

## Assumptions recorded

- The numeric positions that came with the competitor list were **not** imported.
- The GSC export has no date column; the period (2026 year to about 10 October) is stored as import metadata.
- No query in the export contains the word "Metricon"; brand-term flags are present but all false.
- Metricon's brand assets were not supplied, so the UI uses a neutral mark and palette, not a logo.
- Metricon's regional offer URLs (`latest-offers/melbourne`, `sydney`, `queensland`, `south-australia`, `northern-nsw`, `southern-nsw`, `vic-regional`, `default`) come from the page tab of the GSC export.
