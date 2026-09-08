# Architecture

```
                 +-----------------------------------------------------------+
  upstreams      |  src/nyc_live (core library)                              |
                 |                                                           |
  DOT cameras -->|  feeds/cameras.py      \                                  |
  MTA GTFS-RT -->|  feeds/transit.py       |  FeedAdapter.fetch()            |
  Citi Bike   -->|  feeds/micromobility.py |  -> Snapshot[T] | FeedUnavailable
  Socrata     -->|  feeds/civic.py        /                                  |
  weather.gov -->|                                                           |
                 |  cache.py   CachedFeed: TTL, single-flight, stale-with-error
                 |  store.py   DuckDB (frozen DDL) + Parquet archive         |
                 |  services/  geo filtering, arrivals, density aggregates   |
                 +-----+---------------------------+-----------------+-------+
                       |                           |                 |
             packages/nyc-mcp            packages/nyc-vision   packages/nyc-dash
             FastMCP tools               YOLO over frames      FastAPI + MapLibre/Deck.gl
             Envelope[T] out             DensitySample -> DuckDB   reads services/, never feeds
```

## Principles

* **Contracts are frozen.** `contracts.py` is the single source of truth for record shapes, the adapter protocol, and the DuckDB schema. See `CLAUDE.md`.
* **Adapters are parameterless.** Each `fetch()` returns the whole feed. Geo filtering happens in the service layer over cached snapshots, so one upstream call serves every consumer.
* **Every response is an Envelope.** `status` is `fresh`, `stale`, or `error`; `stale_after` and `error` are always present. Consumers render the status, they never assume success.
* **Nothing is fabricated.** A failing upstream produces an error envelope or a stale envelope with the error attached, never synthetic rows.
* **Upstream politeness is layered.** Adapters own per-key cadence (`RateLimiter`), the cache owns TTL, and both are configured from `DEFAULT_TTL`.

## Data flow for a tool call

1. Tool receives optional `lat`, `lon`, `radius_m`.
2. Service asks `FeedRegistry[feed].get()`; the cache returns fresh, stale, or error.
3. Service applies `geo.filter_nearby` and any derived view (e.g. per-stop arrivals).
4. Tool returns `Envelope[T]` with `query` echoed and `total_before_filter` set.

## Storage

DuckDB file at `NYC_LIVE_DUCKDB_PATH`. `nyc-vision` is the only writer of `density_samples` and `camera_frame_fetches`; the cache writes `feed_fetches`. Append-only tables are archived daily to Parquet under `NYC_LIVE_DATA_DIR/archive/<table>/<day>.parquet`. `query_warehouse` is SELECT-only over an allowlist of tables.

Later phases extend this document; the sections above are the Phase 0 baseline.
