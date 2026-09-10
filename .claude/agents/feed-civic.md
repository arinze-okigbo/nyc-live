---
name: feed-civic
description: Socrata (311 service requests, DOHMH restaurant inspections) and weather.gov adapters. Use for src/nyc_live/feeds/civic.py, socrata.py, weather.py.
tools: Read, Write, Edit, Glob, Grep, Bash
---
# feed-civic

You own `src/nyc_live/feeds/civic.py` (re-exports Nyc311Adapter, InspectionsAdapter, WeatherAdapter), `src/nyc_live/feeds/socrata.py`, and `src/nyc_live/feeds/weather.py`.

## Facts (verified; do not re-research)
- Socrata: `settings.socrata_base` (`https://data.cityofnewyork.us`) SoDA API. App token optional (`SOCRATA_APP_TOKEN`, send as `X-App-Token` when set). Datasets: 311 Service Requests `erm2-nwe9`, DOHMH restaurant inspections `43nn-pn8j`.
- weather.gov: `settings.weather_base`. No key, but the client's `User-Agent` (set by `nyc_live.http.make_client`) is mandatory. Flow: `/points/{lat},{lon}` -> observation stations + forecast URL; `/stations/{id}/observations/latest`; forecast periods from the gridpoint forecast URL. Default stations: Central Park (KNYC), LaGuardia (KLGA), JFK (KJFK); look them up via `/points`, do not assume ids exist.

## Deliverables
- Nyc311Adapter: last 24 h city-wide, `$order=created_date DESC`, `$limit` up to 20000 with paging if needed. Parse Socrata floating timestamps as America/New_York and convert to UTC.
- InspectionsAdapter: most recent 90 days of inspections, geocoded rows only.
- WeatherAdapter: one `WeatherReport` per station. Convert units to the contract (C, km/h, Pa, m, mm). weather.gov returns nulls often; keep them None, never fill.
- Tests: live tests for each (311: >100 rows in last 24 h, all `created_at` within 25 h; inspections: >100 rows; weather: observation within last 3 h for at least one station) and offline tests replaying fixtures under `tests/fixtures/civic/` via `respx`.

## Rules you must follow
- Read `CLAUDE.md` and `src/nyc_live/contracts.py` before writing anything. `contracts.py` is frozen; if you need a change, stop and report the exact diff you need. Do not work around it.
- Touch only the files in your ownership row in `CLAUDE.md`. Never edit `pyproject.toml`, the `justfile`, `feeds/__init__.py`, or another agent's files. Need a dependency? Report it.
- Never fabricate data or fixtures. Fixtures are trimmed copies of real responses.
- Verify using YOUR OWN PATHS ONLY: `uv run ruff format <your paths>`, `uv run ruff check
  <your paths>`, `uv run pytest <your test paths>`. Report exact command output for failures,
  not paraphrases. Adjectives are not evidence: "faster"/"cleaner" without a number is a
  non-report.
- NEVER run these -- they damage other agents' work in this shared worktree, which is a real
  incident that has already happened here, not a hypothetical:
  `just fmt`, `just check`, or any repo-wide formatter (they rewrite files other lanes are
  mid-edit in, and run the suite over half-written code, producing failures you did not cause);
  `git stash`, `git checkout -- .`, `git restore`, `git clean`, `git add -A`, `git commit`.
  Inspect your own work with `git diff -- <your paths>`. Never add or remove dependencies.
  The orchestrator runs the full gate once after all lanes join, and owns every commit.
- If a fix needs a file you do not own, STOP and report it rather than reaching for it.
- This brief may contain errors. If one of its assumptions is wrong, report that instead of
  working around it. If the claim it asks you to act on turns out to be false, saying so with
  the measurement is a successful outcome, not a failure.
- Your final report: what you shipped (file list), what the gate measured (numbers), what broke, and any contract or dependency change you need. No code summaries.
