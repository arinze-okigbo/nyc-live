# Recording the Citi Bike GBFS fixtures

`gbfs.citibikenyc.com` is not reachable from the sandbox the adapter was written
in, so these fixtures must be recorded on a machine with network access. They
are trimmed copies of real responses; never hand-edit values.

Files expected by `tests/feeds/test_micromobility.py::test_replays_recorded_gbfs_fixtures`:

| File | Source | Trim |
|------|--------|------|
| `gbfs.json` | the root, `NYC_LIVE_CITIBIKE_GBFS_ROOT` (default `https://gbfs.citibikenyc.com/gbfs/gbfs.json`) | none |
| `station_information.json` | the `station_information` URL listed in `gbfs.json` | first 20 stations |
| `station_status.json` | the `station_status` URL listed in `gbfs.json` | the same 20 `station_id`s |
| `vehicle_types.json` (optional) | the `vehicle_types` URL listed in `gbfs.json`, if any | none |

The replay test skips with `fixture not recorded yet: <path>` while any of the
three required files is missing. `vehicle_types.json` is only needed if the
recorded `station_status.json` rows lack `num_ebikes_available` (the adapter
then classifies `vehicle_types_available` through `vehicle_types`); record it
anyway, it is tiny.

## Commands

Run from the repo root. Requires `curl` and `jq`. Download both station files
back-to-back so the two 20-station subsets come from the same moment and the
join has no unmatched rows.

```bash
cd tests/fixtures/micromobility
UA="nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)"
ROOT="${NYC_LIVE_CITIBIKE_GBFS_ROOT:-https://gbfs.citibikenyc.com/gbfs/gbfs.json}"

# 1. Root (kept whole; it is small). Handles GBFS 2.x (data.en.feeds) and 3.x (data.feeds).
curl -sSf -H "User-Agent: $UA" "$ROOT" -o gbfs.json
FEEDS='(.data.en // (.data | to_entries | map(select(.value | type == "object" and has("feeds"))) | .[0].value) // .data).feeds'
INFO_URL=$(jq -r "$FEEDS[] | select(.name == \"station_information\") | .url" gbfs.json)
STATUS_URL=$(jq -r "$FEEDS[] | select(.name == \"station_status\") | .url" gbfs.json)
VT_URL=$(jq -r "$FEEDS[] | select(.name == \"vehicle_types\") | .url" gbfs.json)
echo "info=$INFO_URL status=$STATUS_URL vehicle_types=$VT_URL"

# 2. Full station files, fetched together.
curl -sSf -H "User-Agent: $UA" "$INFO_URL"   -o station_information.full.json
curl -sSf -H "User-Agent: $UA" "$STATUS_URL" -o station_status.full.json

# 3. Trim information to its first 20 stations, then keep only those ids in status.
jq '.data.stations |= .[:20]' station_information.full.json > station_information.json
jq --slurpfile info station_information.json '
    ([$info[0].data.stations[].station_id]) as $ids
    | .data.stations |= map(select(.station_id as $id | $ids | index($id)))
' station_status.full.json > station_status.json

# 4. Optional vehicle_types (whole file).
if [ -n "$VT_URL" ]; then curl -sSf -H "User-Agent: $UA" "$VT_URL" -o vehicle_types.json; fi

# 5. Sanity: both trimmed files must carry the same 20 ids, then drop the full downloads.
diff <(jq -r '.data.stations[].station_id' station_information.json | sort) \
     <(jq -r '.data.stations[].station_id' station_status.json | sort) && echo "ids match"
rm station_information.full.json station_status.full.json
```

Then run:

```bash
uv run pytest tests/feeds/test_micromobility.py -k replays -v
```

Do not edit the recorded bodies beyond the `jq` trims above. If the two id sets
differ (a station appeared or vanished between the two downloads), re-run step 2
onwards; the adapter tolerates at most 5 % unmatched rows.
