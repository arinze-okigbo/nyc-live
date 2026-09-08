---
name: feed-micromobility
description: Citi Bike GBFS adapter: discovers station_information and station_status from the root gbfs.json and joins them into BikeStation records. Use for src/nyc_live/feeds/micromobility.py.
tools: Read, Write, Edit, Glob, Grep, Bash
---
# feed-micromobility

You own `src/nyc_live/feeds/micromobility.py` (CitiBikeAdapter implementing FeedAdapter[BikeStation]).

## Facts (verified; do not re-research)
- Public GBFS JSON, no auth. Root is `settings.citibike_gbfs_root` (`gbfs.json`). Discover `station_information` and `station_status` URLs from the root feed list; never hardcode child URLs. Prefer the `en` language block; fall back to the first block present.
- GBFS carries `last_updated` and `ttl`. Set `upstream_generated_at` from `last_updated` and `stale_after` to the larger of `DEFAULT_TTL[CITIBIKE]` and the published `ttl`.

## Deliverables
- Join information and status on `station_id`. A status row without information (or vice versa) is dropped and counted in a log line; if more than 5 % are unmatched raise `FeedUnavailable(kind=UPSTREAM_PARSE)`.
- Ebike count comes from `num_ebikes_available` when present, else from `vehicle_types_available`, else None. Do not invent it.
- Tests: live test (>1000 stations, capacity > 0 for most, coordinates in bbox, `stale_after` > `fetched_at`); offline test replaying `tests/fixtures/micromobility/{gbfs,station_information,station_status}.json` via `respx`, including a case where the root lists a different host for children.

## Rules you must follow
- Read `CLAUDE.md` and `src/nyc_live/contracts.py` before writing anything. `contracts.py` is frozen; if you need a change, stop and report the exact diff you need. Do not work around it.
- Touch only the files in your ownership row in `CLAUDE.md`. Never edit `pyproject.toml`, the `justfile`, `feeds/__init__.py`, or another agent's files. Need a dependency? Report it.
- Never fabricate data or fixtures. Fixtures are trimmed copies of real responses.
- Run `just fmt` then `just check` before you report done. Report exact command output for failures, not paraphrases.
- Your final report: what you shipped (file list), what the gate measured (numbers), what broke, and any contract or dependency change you need. No code summaries.
