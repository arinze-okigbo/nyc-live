# Orchestrator ledger

Orchestrator-owned. Not `docs/**` (docs-writer owns that). Rewritten each cycle.
The **Constraints** section is pasted verbatim into every worker brief.

---

## Constraints learned (append-only — never prune)

1. **Repo-wide formatters are destructive mid-cycle.** `just fmt` / repo-wide
   `ruff format` rewrites files other lanes are mid-edit in. Observed: a design
   lane's `just fmt` reformatted two Python files belonging to two other lanes.
   Workers format their own paths only.
2. **`just check` in a worker is worse than useless.** It runs the full suite over
   other lanes' half-written files, so the worker reports failures it didn't cause
   and is tempted to "fix" them. **The full gate is orchestrator-only, run once
   after the join.** All nine agent definitions were rewritten to remove this.
3. **`git stash` in a shared worktree destroys peers' uncommitted work.** An agent
   stashing to inspect its own diff briefly reverted two other lanes. Same class:
   `git checkout -- .`, `git restore`, `git clean`, `git add -A`.
4. **Ownership must forbid verbs, not just paths.** The original ownership map
   banned editing files but never banned the commands that ignore paths entirely.
   That gap caused every incident above.
5. **~6 concurrent write agents hits this account's session rate limit** and killed
   six mid-edit at once. Cap write lanes at 3–4, staggered. Read-only lanes 6–8.
6. **Never fan out across an interface that isn't decided yet.** Parallelism is
   safe here only because `contracts.py` is frozen and the frontend was
   pre-modularized — the conflicting decisions were made once, up front, by one
   mind. If a cycle needs two agents to agree on a new shape, that cycle is
   single-threaded: design the interface, freeze it, *then* fan out.
7. **~79% of multi-agent failures are specification/design/verification** (MAST,
   1,600+ traces) — i.e. the orchestrator's, not the workers'. Brief quality is
   the product.
8. **Briefs must invite disagreement.** Agents given "this brief may contain
   errors" caught real bugs in their own briefing: a wrong date format
   (DD/MM vs MM/DD, proven three ways) and a false premise about persisted history.
9. **Give agents permission to find nothing.** Those told a "nothing found" report
   is a success produced more honest work than those implicitly expected to ship.
10. **A design change you have not looked at is a guess.** Discs-at-every-zoom
    passed review in code and buried Manhattan in a solid mass on screen.
11. **Commit at every green gate.** Recovery from the six-agent kill only worked
    because the gate was green and diffs were coherent. Blast radius = one cycle.
12. **Subagents never spawn subagents.** Keep the tree depth 1 so the orchestrator
    always knows who holds which file.

---

## Backlog (each item carries its falsifiable claim, ready to paste)

**Ops-center build** — full spec at `scratchpad/OPS_CENTER_SPEC.md`.
- `/api/history/{table}` — *Claim: no dash route serves history except
  camera_density_history; adding bucketed read endpoints over Store.query_readonly
  unblocks replay. Do NOT expose warehouse.py's raw SQL to the browser — SELECT-only
  but not cost-bounded, so a cross join is a trivial DoS.*
- `nyc-historian` writer — *Claim: contracts.py declares bike_station_status,
  weather_observations, service_requests_311, subway_positions and NOTHING writes
  them; the only INSERT sites are feed_fetches, camera_frame_fetches, cameras.
  Verify by grep before building.* Volume: subway_positions ≈ 2M rows/day.
- Event + correlation engine — *Claim: the frontend has no concept of an event;
  all 4,061 lines of static/js render current state.* Budget <6 alerts/operator/hour
  (ISA-18.2 / EEMUA 191; >30 is formally "seriously deficient").
- Video wall `/wall` — alarm-driven pop-up FIRST; without it a 4×4 grid is
  decoration. Verify hotlink/CORS live before committing to browser-direct `<img>`.
- Time conductor, wall-mode stylesheet, baselines. See spec for ordering.

**Open bugs (fix lanes died mid-edit on the rate limit):**
- SSE client half of per-feed params. Server half landed. *Claim: buses drop
  3000→1000 fifteen seconds after load; assert counts are unchanged across two ticks.*
- detail-panel fabricates "no longer tracked" on errored/truncated envelopes and
  latches forever.
- Subway trip cache never refreshes (miss rate 4.6% at 1min → 6.5% at 6min).
- Search dropdown stays armed after click-away; Enter opens an unseen result.
- `highlightedBusRoute` stranded, no indicator, no way to clear.
- One SSE hiccup downgrades to polling permanently.
- Layer counts present page size as citywide total — `alerts-banner.js:131-135`
  already solved exactly this and calls it "the bug this function exists to prevent".

**Perf** (baselines to beat): SSE re-sends 92% byte-identical data every cycle
(~680MB/hr per tab); data-sync fetches feeds whose layer is toggled OFF (~2.2MB
wasted per load); mta_bus cold fetch blocked 9.7s and 28.5s inline.

---

## Do-not-retry (measured, doesn't work / already correct)

- **Do not "optimize" these — measured fine:** panning/zoom 60fps with every layer
  on; picking + autoHighlight across 6,273 points costs the same as zero pickable
  layers; the 15s refresh runs <10ms with zero long tasks; no memory leak (heap
  plateaus ~25MB through 18 cycles with a live panel open); app JS/CSS size.
- **Vision Zero crash data (`h9gi-nx95`)** — newest row 2026-06-11. Not live.
- **Hourly subway ridership (`wujg-7c2s`)** — newest row 2024-12-31. Dead.
- **Density heatmap non-pickability** is deliberate and documented in code. Two
  separate lanes nearly "fixed" it.
- **511NY is not key-gated** — an invalid key returns a byte-identical 200, so
  gating would switch off a working layer.
- **Discs at every zoom** — tried, buries the basemap at city scale.

---

## Open contract requests from workers

- 511 incidents and elevator outages have **no history table**; persisting them
  needs a `contracts.py` DDL change (orchestrator-only) or they stay live-only.
- **k-suppression floor** (privacy): a `person=1` density sample on a quiet
  residential street at 03:00 is effectively individual observation. Proposal:
  store the row, but have `services/density.py` return counts below k as `<k`.
  Needs a `docs/privacy.md` update. **Accepted in principle — not yet built.**
