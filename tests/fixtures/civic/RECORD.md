# Recording the civic fixtures

These fixtures are trimmed copies of real upstream responses. They could not be
recorded from the build sandbox (data.cityofnewyork.us and api.weather.gov are
blocked by policy there), so record them on a machine with network access.
Never hand-edit or invent a fixture; re-run the commands instead.

Run from the repo root. `jq` is required. Every request sends the same
User-Agent the app uses (weather.gov rejects requests without one).

```bash
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
OUT=tests/fixtures/civic
```

## 311 service requests (`erm2-nwe9`) -> `nyc311_page.json`

Nyc311Adapter queries by recency alone -- `$order` + `$limit`, no `$where`
time window -- because erm2-nwe9 publishes in daily batches and has been
observed live lagging the wall clock by 37.6h+ (2026-09-09); a fixed 24h
`$where` cutoff returns zero rows whenever lag exceeds it. See
`NYC_311_STALENESS_CEILING` in `socrata.py`.

```bash
curl -sS -A "$UA" -G "https://data.cityofnewyork.us/resource/erm2-nwe9.json" \
  ${SOCRATA_APP_TOKEN:+-H "X-App-Token: $SOCRATA_APP_TOKEN"} \
  --data-urlencode "\$order=created_date DESC" \
  --data-urlencode "\$limit=20" \
  | jq '.' > "$OUT/nyc311_page.json"
jq 'length' "$OUT/nyc311_page.json"   # expect 20
```

## DOHMH restaurant inspections (`43nn-pn8j`) -> `inspections_page.json`

```bash
SINCE=$(TZ=America/New_York date -d '90 days ago' +%Y-%m-%dT%H:%M:%S)   # GNU date
# macOS: SINCE=$(TZ=America/New_York date -v-90d +%Y-%m-%dT%H:%M:%S)
curl -sS -A "$UA" -G "https://data.cityofnewyork.us/resource/43nn-pn8j.json" \
  ${SOCRATA_APP_TOKEN:+-H "X-App-Token: $SOCRATA_APP_TOKEN"} \
  --data-urlencode "\$where=inspection_date >= '$SINCE' AND latitude IS NOT NULL AND longitude IS NOT NULL" \
  --data-urlencode "\$order=inspection_date DESC" \
  --data-urlencode "\$limit=20" \
  | jq '.' > "$OUT/inspections_page.json"
jq 'length' "$OUT/inspections_page.json"   # expect 20
```

## weather.gov, Central Park chain

Four files, recorded in order because each step's URL comes from the previous
response (the adapter never assumes station ids).

```bash
# 1. points -> weather_points_central_park.json (keep whole; it is small)
curl -sS -A "$UA" -H 'Accept: application/geo+json' \
  "https://api.weather.gov/points/40.7789,-73.9692" \
  | jq '.' > "$OUT/weather_points_central_park.json"

# 2. observation stations -> weather_stations_central_park.json (trim to first 3 features)
STATIONS=$(jq -r '.properties.observationStations' "$OUT/weather_points_central_park.json")
curl -sS -A "$UA" -H 'Accept: application/geo+json' "$STATIONS" \
  | jq '.features |= .[:3] | .observationStations |= .[:3]' \
  > "$OUT/weather_stations_central_park.json"

# 3. latest observation for the first station -> weather_observation_latest.json (keep whole)
STATION=$(jq -r '.features[0].properties.stationIdentifier' "$OUT/weather_stations_central_park.json")
echo "first station: $STATION"   # expected KNYC, but the fixture is whatever /points returns
curl -sS -A "$UA" -H 'Accept: application/geo+json' \
  "https://api.weather.gov/stations/$STATION/observations/latest" \
  | jq '.' > "$OUT/weather_observation_latest.json"

# 4. forecast -> weather_forecast.json (trim to first 4 periods)
FORECAST=$(jq -r '.properties.forecast' "$OUT/weather_points_central_park.json")
curl -sS -A "$UA" -H 'Accept: application/geo+json' "$FORECAST" \
  | jq '.properties.periods |= .[:4]' > "$OUT/weather_forecast.json"
```

## Verify

```bash
uv run pytest tests/feeds/test_civic_311.py tests/feeds/test_civic_inspections.py tests/feeds/test_civic_weather.py -k replay -v
```

Every `test_replay_*` should now pass instead of skipping.
