# Recording the DOT camera fixtures

`webcams.nyctmc.org` is blocked from the sandbox these tests were written in, so
the fixtures below are **not** checked in yet. The offline replay tests in
`tests/feeds/test_cameras.py` and `tests/feeds/test_cameras_frames.py` skip with
a reason naming the missing file until you record them. Never hand-write these
files: they must be trimmed copies of real responses.

Run from the repo root on a machine with network access.

## 1. `cameras.json` (camera list, trimmed to 20 real entries)

```sh
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'

curl -sS -H "User-Agent: $UA" "https://webcams.nyctmc.org/api/cameras/" \
  | jq 'if type == "array" then .[:20] else . end' \
  > tests/fixtures/cameras/cameras.json
```

If the response is a dict wrapping the array (e.g. `{"cameras": [...]}`), trim the
inner array instead, e.g. `jq '.cameras |= .[:20]'`, and tell the orchestrator so
the wrapper key can be confirmed against `cameras.py`.

Sanity check: `jq 'length' tests/fixtures/cameras/cameras.json` prints `20` and
`jq '.[0] | keys' tests/fixtures/cameras/cameras.json` lists the real field names
(expected: `id`, `name`, `latitude`, `longitude`, `area`, `isOnline`, `imageUrl`, ...).
If the names differ from the assumptions in the docstring of
`src/nyc_live/feeds/cameras.py`, report them.

## 2. `frame.jpg` (one JPEG frame from an online camera in the trimmed list)

```sh
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
ID=$(jq -r '[.[] | select(.isOnline == true or .isOnline == "true")][0].id' \
       tests/fixtures/cameras/cameras.json)

curl -sS -H "User-Agent: $UA" "https://webcams.nyctmc.org/api/cameras/$ID/image" \
  -o tests/fixtures/cameras/frame.jpg

file tests/fixtures/cameras/frame.jpg       # must say "JPEG image data"
stat -c %s tests/fixtures/cameras/frame.jpg # must be > 1024 bytes
```

If `$ID` is empty the `isOnline` key name differs from the assumption; inspect
`jq '.[0]'` and pick any camera id by hand for the `curl` (do not edit the JSON).

Note on privacy: `frame.jpg` is a single static test fixture of a public traffic
camera. The runtime pipeline (`CameraFrameSource`) never writes frames to disk.

## 3. `ny511_events.json` (511NY statewide events, trimmed to 20 real entries)

Recorded 2026-09-09 from the keyless endpoint (no `key` parameter; `format=json`
is mandatory, a bare `getevents` 404s). The full response was 2,420 events /
2.9 MB, so it is trimmed to 20 by a deterministic selection rather than a blind
head: up to 3 events of **each** `EventType` inside the NYC bbox, then the first
3 outside it, then the single event whose `Severity` was outside 511NY's usual
Minor/Moderate/Major/Unknown set (`"None"`). That is what gives the replay test
in `tests/feeds/test_ny511_events.py` coverage of all six event types, both
sides of the bbox filter, and the unknown-severity fallback -- from real rows
only. Rows are copied verbatim, never edited.

```sh
UA='nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)'
RAW=$(mktemp)

curl -sS -H "User-Agent: $UA" 'https://511ny.org/api/getevents?format=json' > "$RAW"
jq 'length' "$RAW"   # was 2420

# nyc_live.geo.in_nyc_bbox: NYC_BBOX padded by 0.05 deg
INB='(.Latitude >= 40.4274 and .Latitude <= 40.9676
      and .Longitude >= -74.3091 and .Longitude <= -73.6504)'

jq "[ (map(select($INB)) | group_by(.EventType) | map(.[0:3]) | add),
      (map(select($INB | not)) | .[0:3]),
      (map(select(.Severity == \"None\")) | .[0:1]) ] | add" "$RAW" \
  > tests/fixtures/cameras/ny511_events.json
```

Sanity checks (the replay test asserts the same things):

```sh
F=tests/fixtures/cameras/ny511_events.json
jq 'length' "$F"                                  # expect 20
jq '[.[] | select(.CountyName != "Monroe")] | length' "$F"   # 16 inside the bbox
jq '[.[].EventType] | unique | length' "$F"       # 6
jq '[.[].LastUpdated | split("/")[1] | tonumber] | max' "$F" # <= 12: dates are DD/MM/YYYY
jq '[.[].LastUpdated | split("/")[0] | tonumber] | max' "$F" # > 12: proves day-first
```

Timestamps in this feed are `DD/MM/YYYY HH:MM:SS` in **New York local time**, not
`MM/DD` and not UTC -- see `NY511_TIMESTAMP_FORMAT` in `src/nyc_live/feeds/ny511.py`
for the three independent checks that establish that. If you re-record, keep a row
with a day greater than 12 in the selection or the test that guards this fails.
